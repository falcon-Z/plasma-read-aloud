"""Session D-Bus endpoint and one-at-a-time reading lifecycle."""

import concurrent.futures
import json
import logging
import os
from pathlib import Path
import signal
import subprocess

from gi.repository import Gio, GLib, GLibUnix

from . import access, ocr
from .playback import ReadingJob
from .settings import load_rate, load_settings, normalize_rate, save_rate, save_settings
from .speech import Cancelled, Voice, discover_voices

NAME = "org.kde.SelectToSpeak"
PATH = "/SelectToSpeak"
LOG = logging.getLogger("select-to-speak")
EMPTY = ("No readable text found at the pointer", "Point at text or a control, or select some text and try again.")
EMPTY_FOCUS = ("No readable text at keyboard focus", "Focus a control or text field, or use Read from pointer.")
PROTECTED = ("This field is protected", "Protected text can't be read aloud.")
FAILED = ("Couldn't read aloud", "Use the read-aloud shortcut to try again.")
PREVIEW_TEXT = "Hello! This is how I sound when I read aloud. The quick brown fox jumps over the lazy dog."
INTERFACE = f"""
<node>
  <interface name="{NAME}">
    <method name="Read"><arg direction="in" type="s"/></method>
    <method name="ReadOrStop"><arg direction="in" type="s"/></method>
    <method name="Stop"/>
    <method name="TogglePause"/>
    <method name="PreviousSentence"/>
    <method name="NextSentence"/>
    <method name="GetRate"><arg name="rate" direction="out" type="d"/></method>
    <method name="SetRate"><arg name="rate" direction="in" type="d"/></method>
    <method name="SetVoice"><arg name="voice" direction="in" type="s"/></method>
    <method name="PreviewVoice"><arg name="voice" direction="in" type="s"/></method>
    <method name="Status">
      <arg name="phase" direction="out" type="s"/>
      <arg name="voice" direction="out" type="s"/>
    </method>
    <property name="Phase" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Voices" type="s" access="read"/>
    <property name="VoiceId" type="s" access="read"/>
    <property name="PreviewError" type="s" access="read"/>
    <property name="ReadingError" type="s" access="read"/>
    <property name="CanGoBack" type="b" access="read"/>
    <property name="CanGoForward" type="b" access="read"/>
  </interface>
</node>
"""


class Reader:
    def __init__(self, base, settings, bus):
        from .overlay import Highlight

        self.base = base
        self.settings = settings
        self.bus = bus
        self.rate = load_rate(settings)
        self.highlight = Highlight()
        self.voices = discover_voices(base)
        if not self.voices:
            raise RuntimeError("Install Piper with a voice or eSpeak NG.")
        saved_voice = load_settings(settings).get("voice")
        self.voice_id = (saved_voice if isinstance(saved_voice, str) and saved_voice in self.voices
                         else next(iter(self.voices)))
        self.voice = Voice(base, self.voices[self.voice_id].model)
        self.voice_options = json.dumps([{"id": name, "label": option.label}
                                         for name, option in self.voices.items()])
        self.preview_error = ""
        self.reading_error = ""
        self.navigation = (False, False)
        self.access_context = GLib.MainContext.new()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=self.init_access)
        self.cues = (concurrent.futures.ThreadPoolExecutor(max_workers=1)
                     if os.environ.get("SELECT_TO_SPEAK_AUDIO_CUES", "1") != "0" else None)
        self.job = None
        self.task = None
        self.feedback_job = None
        self.phase = "idle"
        self.pool.submit(self.voice.warm)
        self.locked = False

    def init_access(self):
        # Keep AT-SPI's bus watches on the same worker as its synchronous
        # calls so the GTK main loop cannot dispatch their replies in parallel.
        access.A.init()
        access.A.set_main_context(self.access_context)
        access.A.set_timeout(800, 1000)

    def get_property(self, bus, sender, path, interface, name):
        if name == "Rate":
            return GLib.Variant("d", self.rate)
        if name in ("CanGoBack", "CanGoForward"):
            back, forward = self.navigation
            return GLib.Variant("b", back if name == "CanGoBack" else forward)
        return GLib.Variant("s", {
            "Phase": self.phase,
            "Voices": self.voice_options,
            "VoiceId": self.voice_id,
            "PreviewError": self.preview_error,
            "ReadingError": self.reading_error,
        }[name])

    def publish(self, **changes):
        self.bus.emit_signal(
            None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (NAME, changes, [])),
        )

    def change_rate(self, rate):
        rate = normalize_rate(rate)
        if rate != self.rate or not self.settings.exists():
            save_rate(self.settings, rate)
        self.rate = rate
        self.publish(Rate=GLib.Variant("d", rate))

    def change_voice(self, voice_id):
        if voice_id not in self.voices:
            raise ValueError("This voice is unavailable.")
        if voice_id != self.voice_id:
            save_settings(self.settings, voice=voice_id)
            self.voice_id = voice_id
            self.publish(VoiceId=GLib.Variant("s", voice_id))
        self.set_preview_error("")

    def set_preview_error(self, message):
        if self.preview_error != message:
            self.preview_error = message
            self.publish(PreviewError=GLib.Variant("s", message))

    def set_reading_error(self, message):
        if self.reading_error != message:
            self.reading_error = message
            self.publish(ReadingError=GLib.Variant("s", message))

    def start(self, request):
        if self.locked:
            return
        self.set_reading_error("")
        cancel = self.begin("finding text")
        self.cue("accepted", cancel)
        self.task = self.pool.submit(self.run, request, cancel)

    def start_preview(self, voice_id):
        if voice_id not in self.voices:
            raise ValueError("This voice is unavailable.")
        if self.locked or self.job is not None:
            raise ValueError("Voice preview is only available while the reader is idle.")
        self.set_preview_error("")
        cancel = self.begin("preparing preview")
        self.task = self.pool.submit(self.run_preview, self.voices[voice_id].model, cancel)

    def begin(self, phase):
        self.cancel_current()
        cancel = self.job = ReadingJob()
        self.update(cancel, phase, [])
        return cancel

    def cancel_current(self):
        if self.feedback_job is not None:
            self.feedback_job.set()
            self.feedback_job = None
        if self.job is not None:
            self.job.stop()
            self.task.cancel()

    def stop(self):
        self.cancel_current()
        if self.job is not None:
            self.update(self.job, "idle", [])

    def toggle_pause(self):
        job = self.job
        if job is not None and job.toggle_pause():
            self.update(job, "paused" if job.paused else "preparing audio")

    def seek(self, delta):
        job = self.job
        if job is not None and job.seek(delta):
            self.update(job, "paused" if job.paused else "preparing audio", [])

    def show_sentence(self, job, interrupted, boxes=None):
        if not interrupted.is_set():
            self.update(job, "speaking", boxes)

    def update(self, cancel, phase, boxes=None):
        if self.job is not cancel:
            return
        changes = {}
        if self.phase != phase:
            self.phase = phase
            changes["Phase"] = GLib.Variant("s", phase)
        if phase == "idle":
            self.job = None
            self.task = None
        navigation = cancel.navigation() if phase in ("speaking", "paused", "preparing audio") else (False, False)
        if self.navigation != navigation:
            self.navigation = navigation
            changes["CanGoBack"] = GLib.Variant("b", navigation[0])
            changes["CanGoForward"] = GLib.Variant("b", navigation[1])
        if changes:
            self.publish(**changes)
        if boxes is not None:
            self.highlight.update(boxes)

    def run_preview(self, model, cancel):
        error = ""
        try:
            if cancel.is_set():
                return
            voice = Voice(self.base, model, allow_fallback=False)
            try:
                rate = self.rate
                voice.queue(PREVIEW_TEXT, rate)
                wav = voice.receive(PREVIEW_TEXT, rate, cancel)
                if not wav:
                    raise OSError("The voice returned no audio")
                GLib.idle_add(self.update, cancel, "previewing")
                voice.play(wav, cancel, lambda: None)
            finally:
                voice.close()
        except Cancelled:
            pass
        except (OSError, RuntimeError, subprocess.SubprocessError):
            LOG.exception("Voice preview failed")
            error = "Could not play this voice preview."
        finally:
            GLib.idle_add(self.finish_preview, cancel, error)

    def finish_preview(self, cancel, error):
        if self.job is cancel:
            self.set_preview_error(error)
            self.update(cancel, "idle", [])

    def cue(self, kind, cancel):
        if self.cues is not None:
            self.feedback_job = cancel
            self.cues.submit(self.play_cue, kind, cancel)

    def play_cue(self, kind, cancel):
        if not cancel.is_set():
            try:
                self.voice.play_cue(kind)
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.warning("Could not play shortcut feedback: %s", exc)

    def finish(self, cancel, outcome, spoken=False):
        if self.job is not cancel:
            return
        self.update(cancel, "idle", [])
        if outcome is not None:
            self.set_reading_error(outcome[0])
            if not spoken:
                self.cue("unavailable", cancel)
            self.notify(*outcome)

    def announce(self, message, cancel):
        if cancel.is_set():
            return False
        rate = self.rate
        try:
            GLib.idle_add(self.update, cancel, "announcing status", [])
            self.voice.queue(message, rate)
            wav = self.voice.receive(message, rate, cancel)
            if not wav:
                return False
            self.voice.play(wav, cancel, lambda: None)
            return True
        except Cancelled:
            self.voice.close()
        except Exception:
            self.voice.close()
            LOG.exception("Could not speak the reading status")
        return False

    def lock_changed(self, bus, sender, path, interface, member, data):
        self.locked, = data.unpack()
        if self.locked:
            self.stop()

    def notify(self, summary, body):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            bus.call(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    "Select to Speak", 0, "select-to-speak-symbolic", summary, body, [],
                    {"transient": GLib.Variant("b", True)}, 2500,
                )), None, Gio.DBusCallFlags.NONE, 750, None, None, None,
            )
        except GLib.Error:
            LOG.debug("Desktop notification was unavailable")

    def run(self, request, cancel):
        def check():
            if cancel.is_set():
                raise Cancelled
            self.access_context.iteration(False)

        outcome = None
        try:
            check()
            self.voice.select(self.voices[self.voice_id].model)
            target = access.request_target(request)
            reading = access.resolve(request, check)
            check()
            sentences = list(reading.utterances()) if reading else []
            if not sentences and target != "focus":
                GLib.idle_add(self.update, cancel, "recognizing text")
                reading = ocr.capture(request, self.base, cancel)
                sentences = list(reading.utterances()) if reading else []
            check()
            if not sentences:
                LOG.info("No readable text at the requested target")
                outcome = EMPTY_FOCUS if target == "focus" else EMPTY
                return
            LOG.info("Reading from %s using %s", reading.mode, self.voice.name)
            cancel.prepare(len(sentences))
            GLib.idle_add(self.prepare_audio, cancel)
            scroll = (
                os.environ.get("SELECT_TO_SPEAK_AUTO_SCROLL") == "1"
                and request.get("active") is not None
                and request["active"] == request.get("hovered")
                and reading.mode == "pointer"
            )
            if not self.play_sentences(reading, sentences, cancel, check, scroll):
                outcome = FAILED
        except access.ProtectedText:
            outcome = PROTECTED
        except Cancelled:
            LOG.info("Read request cancelled")
        except Exception:
            self.voice.close()
            LOG.exception("Reading failed")
            outcome = FAILED
        finally:
            spoken = outcome is not None and self.announce(outcome[0] + ".", cancel)
            GLib.idle_add(self.finish, cancel, outcome, spoken)

    def prepare_audio(self, job):
        if not job.paused:
            self.update(job, "preparing audio")

    def play_sentences(self, reading, sentences, job, check, scroll):
        queued = None
        cached = None
        highlight = True
        played = False
        try:
            while (sentence := job.sentence()) is not None:
                index, interrupted = sentence
                rate = self.rate
                current = sentences[index]

                if queued is not None:
                    queued_index, queued_rate = queued
                    wav = self.voice.receive(sentences[queued_index].text, queued_rate, job)
                    if queued == (index, rate):
                        cached = (index, rate, wav)
                    queued = None
                if interrupted.is_set():
                    continue
                if cached is None or cached[:2] != (index, rate):
                    self.voice.queue(current.text, rate)
                    queued = (index, rate)
                    wav = self.voice.receive(current.text, rate, job)
                    queued = None
                    cached = (index, rate, wav)
                if interrupted.is_set() or rate != self.rate:
                    continue

                if index + 1 < len(sentences):
                    self.voice.queue(sentences[index + 1].text, rate)
                    queued = (index + 1, rate)
                if cached[2]:
                    previous_boxes = None

                    def refresh():
                        nonlocal highlight, previous_boxes
                        check()
                        if not highlight or interrupted.is_set():
                            return
                        try:
                            boxes = reading.rectangles(current, scroll=scroll and previous_boxes is None)
                        except Exception:
                            LOG.exception("Could not update the reading highlight")
                            highlight = False
                            boxes = []
                        if previous_boxes != boxes:
                            GLib.idle_add(self.show_sentence, job, interrupted, boxes)
                        previous_boxes = boxes

                    GLib.idle_add(self.show_sentence, job, interrupted)
                    try:
                        self.voice.play(cached[2], interrupted, refresh)
                        played = True
                    except Cancelled:
                        check()
                job.complete(interrupted)
            check()
            return played
        finally:
            if queued is not None:
                self.voice.close()

    def close(self, *_):
        self.stop()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.cues is not None:
            self.cues.shutdown(wait=True, cancel_futures=True)
        self.voice.close()
        self.loop.quit()
        return False

    def handle(self, bus, sender, path, iface, method, parameters, reply):
        if method == "Status":
            reply.return_value(GLib.Variant("(ss)", (self.phase, self.voice.name)))
            return
        if method == "GetRate":
            reply.return_value(GLib.Variant("(d)", (self.rate,)))
            return
        if method == "Stop":
            self.stop()
        elif method == "TogglePause":
            self.toggle_pause()
        elif method in ("PreviousSentence", "NextSentence"):
            self.seek(-1 if method == "PreviousSentence" else 1)
        elif method == "SetRate":
            try:
                self.change_rate(parameters.unpack()[0])
            except (ValueError, OSError) as exc:
                reply.return_dbus_error(NAME + ".RateError", str(exc))
                return
        elif method in ("SetVoice", "PreviewVoice"):
            change = self.change_voice if method == "SetVoice" else self.start_preview
            try:
                change(parameters.unpack()[0])
            except (ValueError, OSError) as exc:
                reply.return_dbus_error(NAME + ".VoiceError", str(exc))
                return
        else:
            try:
                request = json.loads(parameters.unpack()[0])
                validate(request)
            except (TypeError, KeyError, ValueError):
                reply.return_dbus_error(NAME + ".InvalidRequest", "Expected a reading target and KWin window data.")
                return
            if method == "ReadOrStop" and self.job is not None:
                self.stop()
            else:
                self.start(request)
        reply.return_value(None)


def validate(request):
    def integers(values, length):
        return isinstance(values, list) and len(values) == length and all(type(v) is int for v in values)

    if not isinstance(request, dict):
        raise ValueError
    target = access.request_target(request)
    if target not in ("auto", "pointer", "focus"):
        raise ValueError
    if (target != "focus" or "point" in request) and not integers(request.get("point"), 2):
        raise ValueError
    for key in ("screen", "desktop"):
        geometry = request.get(key)
        if geometry is not None and (not integers(geometry, 4) or min(geometry[2:]) <= 0):
            raise ValueError
    if "pointerOnly" in request and type(request["pointerOnly"]) is not bool:
        raise ValueError
    for key in ("active", "hovered"):
        item = request.get(key)
        if item is not None:
            if not isinstance(item, dict) or type(item.get("pid")) is not int or not isinstance(item.get("title"), str):
                raise ValueError
            if not all(integers(item.get(rect), 4) for rect in ("client", "frame")):
                raise ValueError
            if "buffer" in item and not integers(item["buffer"], 4):
                raise ValueError
            if "popup" in item and type(item["popup"]) is not bool:
                raise ValueError


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "select-to-speak"
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "select-to-speak"
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    # The accessibility bus starts with assistive technology disabled in some
    # Plasma sessions, even when the desktop setting was saved at install time.
    try:
        bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
    except GLib.Error as exc:
        LOG.warning("Could not enable desktop accessibility: %s", exc.message)
    loop = GLib.MainLoop()
    node = Gio.DBusNodeInfo.new_for_xml(INTERFACE)
    reader = Reader(base, config / "settings.json", bus)
    reader.loop = loop
    bus.register_object(PATH, node.interfaces[0], reader.handle, reader.get_property, None)
    reply = bus.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "RequestName", GLib.Variant("(su)", (NAME, 4)), None, Gio.DBusCallFlags.NONE, 1000, None,
    )
    if reply.unpack()[0] != 1:
        reader.close()
        raise RuntimeError(f"{NAME} is already running")
    bus.signal_subscribe("org.freedesktop.ScreenSaver", "org.freedesktop.ScreenSaver",
                         "ActiveChanged", None, None, Gio.DBusSignalFlags.NONE, reader.lock_changed)
    try:
        result = bus.call_sync(
            "org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver",
            "GetActive", None, None, Gio.DBusCallFlags.NONE, 500, None,
        )
        reader.locked, = result.unpack()
    except GLib.Error:
        pass
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, reader.close)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, reader.close)
    LOG.info("Ready with %s", reader.voice.name)
    loop.run()


if __name__ == "__main__":
    main()
