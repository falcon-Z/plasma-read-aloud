"""Persist speech settings in the user's configuration directory."""

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


def load_settings(path):
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(values, dict):
            return values
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Could not load the saved speech settings")
    return {}


def load_rate(path):
    if "rate" in (saved := load_settings(path)):
        try:
            return normalize_rate(saved["rate"])
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning("Could not load the saved speech rate")
    try:
        return normalize_rate(os.environ.get("SELECT_TO_SPEAK_RATE", DEFAULT_RATE))
    except ValueError:
        return DEFAULT_RATE


def save_rate(path, rate):
    save_settings(path, rate=rate)


def save_settings(path, **changes):
    values = load_settings(path)
    values.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as file:
        temporary = Path(file.name)
        try:
            json.dump(values, file)
            file.write("\n")
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
