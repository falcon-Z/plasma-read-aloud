"""Recognize visible text near the pointer when accessibility exposes no text."""

from dataclasses import dataclass
import csv
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

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
    nearest = []
    for i, word in enumerate(words):
        box = word.box
        dx = max(box.x - x, x - (box.x + box.w - 1), 0)
        dy = max(box.y - y, y - (box.y + box.h - 1), 0)
        if dx <= 12 and dy <= 8:
            nearest.append((dx ** 2 + dy ** 2, i))
    return words[min(nearest)[1]:] if nearest else []


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


def capture_region(request):
    active, hovered = request.get("active"), request.get("hovered")
    if request.get("screen"):
        screen = Rect(*request["screen"])
        bounds = screen.intersect(Rect(*hovered["frame"])) if hovered else screen
        return ["--current"], screen, bounds
    if active and hovered == active:
        bounds = Rect(*active["client"])
        return ["--activewindow", "--no-decoration", "--no-shadow"], bounds, bounds
    return [], None, None


def crop(png, source, bounds):
    image = GdkPixbuf.Pixbuf.new_from_file(str(png))
    width, height = image.get_width(), image.get_height()
    if abs((width / height) / (source.w / source.h) - 1) > 0.06:
        return None
    if bounds == source:
        return width, height
    x = round((bounds.x - source.x) * width / source.w)
    y = round((bounds.y - source.y) * height / source.h)
    right = round((bounds.x + bounds.w - source.x) * width / source.w)
    bottom = round((bounds.y + bounds.h - source.y) * height / source.h)
    if right <= x or bottom <= y:
        return None
    cropped = image.new_subpixbuf(x, y, right - x, bottom - y)
    cropped.savev(str(png), "png", [], [])
    return cropped.get_width(), cropped.get_height()


def capture(request, base, cancel):
    if os.environ.get("SELECT_TO_SPEAK_OCR", "1") == "0":
        return None
    options, source, bounds = capture_region(request)
    point = request["point"]
    if bounds is None or not bounds.contains(*point):
        return None
    spectrum = shutil.which("spectacle")
    tesseract = shutil.which("tesseract")
    local = base / "runtime/ocr/usr/bin/tesseract"
    if tesseract is None and local.is_file():
        tesseract = str(local)
    if not spectrum or not tesseract:
        return None
    with tempfile.TemporaryDirectory(prefix="select-to-speak-", dir=os.environ.get("XDG_RUNTIME_DIR")) as folder:
        png = Path(folder) / "window.png"
        command([spectrum, "-b", "-n", *options, "-o", str(png)], cancel, 3)
        deadline = time.monotonic() + 2
        while not png.is_file():
            if cancel.wait(0.06):
                raise Cancelled
            if time.monotonic() >= deadline:
                return None
        try:
            size = crop(png, source, bounds)
        except GLib.Error:
            return None
        if size is None:
            return None
        language = os.environ.get("SELECT_TO_SPEAK_OCR_LANGUAGE", "eng")
        for layout in ("3", "11"):
            output = command([tesseract, str(png), "stdout", "-l", language,
                              "--psm", layout, "tsv"], cancel, 10)
            if output:
                words = at_point(parse(output.decode("utf-8", errors="replace"), size, bounds), point)
                if words:
                    return ImageReading(words)
        return None
