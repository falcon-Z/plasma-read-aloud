"""Persist the speech rate in the user's configuration directory."""

import json
import logging
import os
from pathlib import Path
import tempfile

MIN_RATE = 0.5
MAX_RATE = 2.0
DEFAULT_RATE = 1.20


def normalize_rate(value):
    rate = float(value)
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError("Speech rate must be between 0.50 and 2.00.")
    return round(rate, 2)


def load_rate(path):
    try:
        return normalize_rate(json.loads(path.read_text(encoding="utf-8"))["rate"])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError):
        logging.getLogger(__name__).warning("Could not load the saved speech rate")
    try:
        return normalize_rate(os.environ.get("SELECT_TO_SPEAK_RATE", DEFAULT_RATE))
    except ValueError:
        return DEFAULT_RATE


def save_rate(path, rate):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as file:
        temporary = Path(file.name)
        try:
            json.dump({"rate": rate}, file)
            file.write("\n")
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
