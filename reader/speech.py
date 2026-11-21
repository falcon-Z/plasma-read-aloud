"""Offline speech synthesis and cancellable playback."""

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import select
import shutil
import struct
import subprocess
import time

LOG = logging.getLogger(__name__)
DEFAULT_VOICE = "en_US-lessac-medium"
VOICE_LABELS = {
    DEFAULT_VOICE: "Lessac · US English",
    "en_US-ryan-medium": "Ryan · US English",
    "en_GB-alba-medium": "Alba · UK English",
}
CUE_RATE = 24000


def cue_samples(notes):
    samples = []
    for frequency, duration in notes:
        frames = round(CUE_RATE * duration)
        fade = round(CUE_RATE * 0.008)
        for i in range(frames):
            envelope = min(i / fade, (frames - 1 - i) / fade, 1)
            samples.append(round(5000 * envelope * math.sin(math.tau * frequency * i / CUE_RATE)))
    return struct.pack(f"<{len(samples)}h", *samples)


CUES = {
    "accepted": cue_samples(((660, 0.045), (880, 0.060))),
    "unavailable": cue_samples(((440, 0.070), (330, 0.100))),
}


class Cancelled(Exception):
    pass


def reap(process):
    if process.poll() is None:
        process.kill()
    process.wait()


@dataclass(frozen=True)
class VoiceOption:
    label: str
    model: Path | None


def piper_python(base):
    return Path(os.environ.get("SELECT_TO_SPEAK_PIPER", base / "piper/bin/python"))


def discover_voices(base):
    options = {}
    if piper_python(base).is_file():
        directory = base / "voices"
        models = [directory / f"{name}.onnx" for name in VOICE_LABELS]
        models.extend(path for path in sorted(directory.glob("*.onnx")) if path.stem not in VOICE_LABELS)
        for path in models:
            if path.is_file() and path.with_suffix(".onnx.json").is_file():
                options[path.stem] = VoiceOption(VOICE_LABELS.get(path.stem, path.stem), path.absolute())
        if configured := os.environ.get("SELECT_TO_SPEAK_VOICE"):
            path = Path(configured).absolute()
            if path.is_file() and path.with_name(path.name + ".json").is_file():
                name = next((name for name, option in options.items() if option.model == path), "configured")
                option = options.pop(name, VoiceOption(f"{path.stem} · Piper", path))
                options = {name: option, **options}
    if shutil.which("espeak-ng"):
        options["espeak-ng"] = VoiceOption("eSpeak NG · System voice", None)
    return options


class Voice:
    def __init__(self, base, model, *, allow_fallback=True):
        self.python = piper_python(base)
        self.model = model
        self.process = None
        self.use_piper = model is not None
        self.allow_fallback = allow_fallback
        self.espeak = shutil.which("espeak-ng")
        self.player = shutil.which("paplay")
        if not self.player or not (self.use_piper or self.espeak):
            raise RuntimeError("Install paplay and either Piper with a voice or eSpeak NG.")

    @property
    def name(self):
        return "Piper" if self.use_piper else "eSpeak NG"

    def select(self, model):
        if model != self.model:
            self.close()
            self.model = model
            self.use_piper = model is not None

    def play_cue(self, cue):
        subprocess.run(
            [self.player, "--client-name=Select to Speak", "--stream-name=Shortcut feedback",
             "--latency-msec=60", "--raw", "--format=s16le", f"--rate={CUE_RATE}", "--channels=1"],
            input=CUES[cue], timeout=2, check=True,
        )

    def warm(self):
        if self.use_piper and self.process is None:
            env = os.environ.copy()
            for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "GI_TYPELIB_PATH"):
                env.pop(key, None)
            self.process = subprocess.Popen(
                [str(self.python), str(Path(__file__).with_name("voice_worker.py")), str(self.model)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
            )

    def close(self):
        if self.process is not None:
            reap(self.process)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def queue(self, text, rate):
        if self.use_piper:
            try:
                self.warm()
                self.process.stdin.write((json.dumps([text, rate]) + "\n").encode())
                self.process.stdin.flush()
            except OSError as exc:
                self.fallback(exc)

    def fallback(self, error):
        self.close()
        if not self.espeak or not self.allow_fallback:
            raise error
        self.use_piper = False
        LOG.warning("Piper unavailable, switching to eSpeak NG (%s)", error)

    def _read(self, size, cancel):
        fd = self.process.stdout.fileno()
        result = bytearray()
        deadline = time.monotonic() + 20
        while len(result) < size:
            if cancel.is_set():
                raise Cancelled
            if time.monotonic() > deadline:
                raise TimeoutError("Piper synthesis timed out")
            if select.select([fd], [], [], 0.05)[0]:
                block = os.read(fd, min(65536, size - len(result)))
                if not block:
                    raise OSError("Piper exited during synthesis")
                result.extend(block)
        return result

    def receive(self, text, rate, cancel):
        if self.use_piper:
            try:
                size, = struct.unpack("!I", self._read(4, cancel))
                if size == 0:
                    return b""
                if not 44 < size <= 20_000_000:
                    raise OSError("Invalid audio from Piper")
                return self._read(size, cancel)
            except OSError as exc:
                self.fallback(exc)
        process = subprocess.Popen(
            [self.espeak, "--stdin", "--stdout", "-s", str(round(165 * rate))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        try:
            payload = text.encode()
            while not cancel.is_set():
                try:
                    audio, _ = process.communicate(payload, timeout=0.05)
                    if process.returncode:
                        raise OSError(f"eSpeak NG exited with code {process.returncode}")
                    return audio
                except subprocess.TimeoutExpired:
                    payload = None
            raise Cancelled
        finally:
            reap(process)

    def play(self, wav, cancel, refresh):
        if cancel.is_set():
            raise Cancelled
        with os.fdopen(os.memfd_create("select-to-speak"), "w+b") as audio:
            audio.write(wav)
            audio.seek(0)
            fd = audio.fileno()
            process = subprocess.Popen(
                [self.player, "--client-name=Select to Speak", "--stream-name=Reading aloud",
                 "--latency-msec=60", f"/proc/self/fd/{fd}"], pass_fds=(fd,),
            )
            try:
                refresh()
                while process.poll() is None:
                    if cancel.wait(0.22):
                        raise Cancelled
                    if process.poll() is None:
                        refresh()
                if process.returncode:
                    raise OSError(f"Audio player exited with code {process.returncode}")
            finally:
                reap(process)
