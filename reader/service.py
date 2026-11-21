"""Session D-Bus endpoint and one-at-a-time reading lifecycle."""

import concurrent.futures
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading

from gi.repository import Gio, GLib, GLibUnix

from . import access, ocr
from .settings import load_rate, normalize_rate, save_rate
from .speech import Cancelled, Voice

NAME = "org.kde.SelectToSpeak"
PATH = "/SelectToSpeak"
LOG = logging.getLogger("select-to-speak")
STOP_ACTION = ("kwin", "Select to Speak: Stop reading", "KWin", "Select to Speak: Stop reading")
QT_ESCAPE = 0x01000000
EMPTY = ("No readable text found at the pointer", "Point at text or a control, or select some text and try again.")
PROTECTED = ("This field is protected", "Protected text can't be read aloud.")
FAILED = ("Couldn't read aloud", "Use the read-aloud shortcut to try again.")
INTERFACE = f"""
<node>
  <interface name="{NAME}">
    <method name="Read"><arg direction="in" type="s"/></method>
    <method name="ReadOrStop"><arg direction="in" type="s"/></method>
    <method name="Stop"/>
    <method name="GetRate"><arg name="rate" direction="out" type="d"/></method>
    <method name="SetRate"><arg name="rate" direction="in" type="d"/></method>
    <method name="Status">
      <arg name="phase" direction="out" type="s"/>
      <arg name="voice" direction="out" type="s"/>
    </method>
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
        self.voice = Voice(base)
        self.access_context = GLib.MainContext.new()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=self.init_access)
        self.cues = (concurrent.futures.ThreadPoolExecutor(max_workers=1)
                     if os.environ.get("SELECT_TO_SPEAK_AUDIO_CUES", "1") != "0" else None)
        self.job = None
        self.task = None
        self.feedback_job = None
        self.phase = "idle"
        self.tray = None
        self.pool.submit(self.voice.warm)
        self.locked = False

    def init_access(self):
        # Keep AT-SPI's bus watches on the same worker as its synchronous
        # calls so the GTK main loop cannot dispatch their replies in parallel.
        access.A.init()
        access.A.set_main_context(self.access_context)
        access.A.set_timeout(800, 1000)

    def attach_tray(self, bus):
        from .tray import Tray

        self.tray = Tray(bus, self.change_rate, self.stop)
        self.tray.update(self.phase, self.rate)

    def change_rate(self, rate):
        rate = normalize_rate(rate)
        if rate != self.rate or not self.settings.exists():
            save_rate(self.settings, rate)
        self.rate = rate
        self.tray.update(self.phase, self.rate)

    def set_escape(self, enabled):
        keys = [([QT_ESCAPE],)] if enabled else []
        try:
            self.bus.call_sync(
                "org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                "setForeignShortcutKeys", GLib.Variant("(asa(ai))", (STOP_ACTION, keys)),
                None, Gio.DBusCallFlags.NO_AUTO_START, 500, None,
            )
        except GLib.Error as exc:
            LOG.warning("Could not %s the Escape shortcut: %s", "bind" if enabled else "release", exc.message)

    def start(self, request):
        if self.locked:
            return
        idle = self.job is None
        self.cancel_current()
        cancel = self.job = threading.Event()
        if idle:
            self.set_escape(True)
        self.update(cancel, "finding text", [])
        self.cue("accepted", cancel)
        self.task = self.pool.submit(self.run, request, cancel)

    def cancel_current(self):
        if self.feedback_job is not None:
            self.feedback_job.set()
            self.feedback_job = None
        if self.job is not None:
            self.job.set()
            self.task.cancel()

    def stop(self):
        self.cancel_current()
        if self.job is not None:
            self.update(self.job, "idle", [])

    def update(self, cancel, phase, boxes=None):
        if self.job is not cancel:
            return
        self.phase = phase
        if phase == "idle":
            self.job = None
            self.task = None
            self.set_escape(False)
        if boxes is not None:
            self.highlight.update(boxes)
        self.tray.update(phase, self.rate)

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
            if not spoken:
                self.cue("unavailable", cancel)
            self.notify(*outcome)

    def announce(self, message, cancel):
        if cancel.is_set():
            return False
        rate = self.rate
        try:
            GLib.idle_add(self.update, cancel, "speaking", [])
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
                    "Select to Speak", 0, "text-to-speech", summary, body, [],
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
        audio_pending = False
        try:
            check()
            reading = access.resolve(request, check)
            check()
            remaining = iter(reading.utterances()) if reading else iter(())
            current = next(remaining, None)
            if current is None:
                check()
                GLib.idle_add(self.update, cancel, "recognizing text")
                reading = ocr.capture(request, self.base, cancel)
                remaining = iter(reading.utterances()) if reading else iter(())
                current = next(remaining, None)
            check()
            if current is None:
                LOG.info("No readable text at the requested target")
                outcome = EMPTY
                return
            LOG.info("Reading from %s using %s", reading.mode, self.voice.name)
            GLib.idle_add(self.update, cancel, "preparing audio")
            scroll = request.get("active") == request.get("hovered") and reading.mode == "pointer"
            highlight = True
            queued_rate = self.rate
            self.voice.queue(current.text, queued_rate)
            audio_pending = True
            while current is not None:
                wav = self.voice.receive(current.text, queued_rate, cancel)
                audio_pending = False
                check()
                requested_rate = self.rate
                if requested_rate != queued_rate:
                    queued_rate = requested_rate
                    self.voice.queue(current.text, queued_rate)
                    audio_pending = True
                    continue
                following = next(remaining, None)
                check()
                if following is not None:
                    self.voice.queue(following.text, queued_rate)
                    audio_pending = True
                check()
                if not wav:
                    current = following
                    continue
                previous_boxes = None

                def refresh():
                    nonlocal highlight, previous_boxes
                    check()
                    if not highlight:
                        return
                    try:
                        boxes = reading.rectangles(current, scroll=scroll and previous_boxes is None)
                    except Exception:
                        LOG.exception("Could not update the reading highlight")
                        highlight = False
                        boxes = []
                    if previous_boxes != boxes:
                        GLib.idle_add(self.update, cancel, "speaking", boxes)
                    previous_boxes = boxes

                GLib.idle_add(self.update, cancel, "speaking")
                self.voice.play(wav, cancel, refresh)
                current = following
        except access.ProtectedText:
            outcome = PROTECTED
        except Cancelled:
            LOG.info("Read request cancelled")
            if audio_pending:
                self.voice.close()
        except Exception:
            self.voice.close()
            LOG.exception("Reading failed")
            outcome = FAILED
        finally:
            spoken = outcome is not None and self.announce(outcome[0] + ".", cancel)
            GLib.idle_add(self.finish, cancel, outcome, spoken)

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
        elif method == "SetRate":
            try:
                self.change_rate(parameters.unpack()[0])
            except (ValueError, OSError) as exc:
                reply.return_dbus_error(NAME + ".RateError", str(exc))
                return
        else:
            try:
                request = json.loads(parameters.unpack()[0])
                validate(request)
            except (TypeError, KeyError, ValueError):
                reply.return_dbus_error(NAME + ".InvalidRequest", "Expected KWin point and window data.")
                return
            if method == "ReadOrStop" and self.job is not None:
                self.stop()
            else:
                self.start(request)
        reply.return_value(None)


def validate(request):
    def integers(values, length):
        return isinstance(values, list) and len(values) == length and all(type(v) is int for v in values)

    if not isinstance(request, dict) or not integers(request.get("point"), 2):
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
    bus.register_object(PATH, node.interfaces[0], reader.handle, None, None)
    reply = bus.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "RequestName", GLib.Variant("(su)", (NAME, 4)), None, Gio.DBusCallFlags.NONE, 1000, None,
    )
    if reply.unpack()[0] != 1:
        reader.close()
        raise RuntimeError(f"{NAME} is already running")
    reader.set_escape(False)
    reader.attach_tray(bus)
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
