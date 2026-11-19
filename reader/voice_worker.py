"""Synthesize WAV data in the Piper Python environment."""

import io
import json
from pathlib import Path
import struct
import sys
import wave

from piper import PiperVoice, SynthesisConfig


def main():
    voice = PiperVoice.load(Path(sys.argv[1]))
    out = sys.stdout.buffer
    for request in sys.stdin.buffer:
        text, rate = json.loads(request)
        config = SynthesisConfig(length_scale=1 / rate)
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wav:
                voice.synthesize_wav(text, wav, syn_config=config)
            payload = buffer.getvalue()
        out.write(struct.pack("!I", len(payload)))
        out.write(payload)
        out.flush()


if __name__ == "__main__":
    main()
