"""Read visible text in the active window when accessibility exposes no text."""

from dataclasses import dataclass
import csv
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

from .model import Piece, Rect, passages
from .speech import Cancelled, reap


@dataclass(frozen=True)
class Word:
    text: str
    box: Rect
    paragraph: tuple
    line: tuple


@dataclass
class ImageReading:
    words: list[Word]
    mode = "visible image"

    def utterances(self):
        pieces = []
        previous = None
        for index, word in enumerate(self.words):
            if index:
                pieces.append(Piece(" " if word.paragraph == previous else "\n"))
            pieces.append(Piece(word.text, word.box, -1))
            previous = word.paragraph
        return passages(pieces)

    def rectangles(self, utterance, scroll=False):
        return [piece.node for piece in utterance.pieces]


def parse(tsv, image_size, bounds):
    width, height = image_size
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        value = row.get("text", "").strip()
        if row.get("level") != "5" or not value or float(row["conf"]) < 35:
            continue
        left, top, w, h = (int(row[field]) for field in ("left", "top", "width", "height"))
        x, y = round(left * bounds.w / width), round(top * bounds.h / height)
        right, bottom = round((left + w) * bounds.w / width), round((top + h) * bounds.h / height)
        paragraph = tuple(row[field] for field in ("page_num", "block_num", "par_num"))
        line = (*paragraph, row["line_num"])
        words.append(Word(value, Rect(x + bounds.x, y + bounds.y, right - x, bottom - y),
                          paragraph, line))
    return words


def at_point(words, point):
    x, y = point
    for i, word in enumerate(words):
        if word.box.contains(x, y):
            return words[i:]
    # Allow the spaces between words in a line, without matching another
    # column when the pointer is in the surrounding whitespace.
    for i, (word, following) in enumerate(zip(words, words[1:])):
        box = word.box
        if word.line == following.line and box.y - 3 <= y < box.y + box.h + 3:
            if box.x + box.w <= x < following.box.x and following.box.x - x < 15:
                return words[i + 1:]
    return []


def command(argv, cancel, seconds):
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise Cancelled
                try:
                    output, _ = process.communicate(timeout=0.08)
                    return output if process.returncode == 0 else None
                except subprocess.TimeoutExpired:
                    pass
            return None
        finally:
            reap(process)


def capture(request, base, cancel):
    if os.environ.get("SELECT_TO_SPEAK_OCR", "1") == "0":
        return None
    active, hovered = request.get("active"), request.get("hovered")
    if not active or hovered != active:
        return None
    spectrum = shutil.which("spectacle")
    tesseract = shutil.which("tesseract")
    local = base / "runtime/ocr/usr/bin/tesseract"
    if tesseract is None and local.is_file():
        tesseract = str(local)
    if not spectrum or not tesseract:
        return None
    bounds = Rect(*active["client"])
    if not bounds.contains(*request["point"]):
        return None

    with tempfile.TemporaryDirectory(prefix="select-to-speak-", dir=os.environ.get("XDG_RUNTIME_DIR")) as folder:
        png = Path(folder) / "window.png"
        command([spectrum, "-b", "-n", "-a", "-e", "-S", "-o", str(png)], cancel, 3)
        deadline = time.monotonic() + 2
        while not png.is_file():
            if cancel.wait(0.06):
                raise Cancelled
            if time.monotonic() >= deadline:
                return None
        with png.open("rb") as file:
            header = file.read(24)
        if len(header) != 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        width, height = struct.unpack("!II", header[16:24])
        if not width or not height or abs((width / height) / (bounds.w / bounds.h) - 1) > 0.06:
            return None
        language = os.environ.get("SELECT_TO_SPEAK_OCR_LANGUAGE", "eng")
        output = command([tesseract, str(png), "stdout", "-l", language, "tsv"], cancel, 10)
        if not output:
            return None
        words = at_point(parse(output.decode("utf-8", errors="replace"), (width, height), bounds),
                         request["point"])
        return ImageReading(words) if words else None
