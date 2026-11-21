"""Recognize visible text near the pointer when accessibility exposes no text."""

from dataclasses import dataclass, replace
import csv
import io
from itertools import groupby, takewhile
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

from .model import Piece, Rect, passages
from .speech import Cancelled, reap

LOG = logging.getLogger(__name__)
SCREENSHOT = "org.kde.KWin.ScreenShot2"


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
            pieces.append(Piece(word.text, word, -1))
            previous = word.paragraph
        return passages(pieces)

    def rectangles(self, utterance, scroll=False):
        words = (piece.node for piece in utterance.pieces)
        boxes = []
        for _, line in groupby(words, key=lambda word: word.line):
            bounds = [word.box for word in line]
            x = min(box.x for box in bounds)
            y = min(box.y for box in bounds)
            right = max(box.x + box.w for box in bounds)
            bottom = max(box.y + box.h for box in bounds)
            boxes.append(Rect(x, y, right - x, bottom - y))
        return boxes


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


def paragraph_at(words, point):
    matches = at_point(words, point)
    if not matches:
        return []
    paragraph = matches[0].paragraph
    paragraph_words = takewhile(lambda word: word.paragraph == paragraph, matches)
    result = []
    previous_row = None
    for key, group in groupby(paragraph_words, key=lambda word: word.line):
        line = list(group)
        first = line[0]
        reference = max(line, key=lambda word: word.box.w).box
        if previous_row is not None:
            if reference.y < previous_row.y + previous_row.h / 2:
                continue
            gap = reference.y - (previous_row.y + previous_row.h)
            if gap > max(12, max(reference.h, previous_row.h) * 1.4):
                break
        previous_row = reference
        # A change of font, such as a bold shortcut, can split one visible
        # line into multiple OCR blocks. Join only close words on that row.
        candidates = []
        for word in words:
            box = word.box
            overlap = min(box.y + box.h, reference.y + reference.h) - max(box.y, reference.y)
            if box.x > first.box.x and overlap >= min(box.h, reference.h) / 2:
                candidates.append(word)
        result.append(first)
        for word in sorted(candidates, key=lambda word: word.box.x):
            previous = result[-1].box
            if word.box.x - (previous.x + previous.w) > max(14, min(36, reference.h * 2)):
                break
            result.append(replace(word, paragraph=paragraph, line=key))
    return result


def command(argv, cancel, seconds, payload=None, env=None):
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env) as process:
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise Cancelled
                try:
                    output, _ = process.communicate(payload, timeout=0.08)
                    return output if process.returncode == 0 else None
                except subprocess.TimeoutExpired:
                    payload = None
            return None
        finally:
            reap(process)


def capture_region(request):
    active, hovered = request.get("active"), request.get("hovered")
    if request.get("screen"):
        screen = Rect(*request["screen"])
        bounds = screen.intersect(Rect(*hovered["frame"])) if hovered else screen
        if request.get("desktop"):
            return ["--fullscreen", "--scaled"], Rect(*request["desktop"]), bounds
        return ["--current"], screen, bounds
    if active and hovered == active:
        bounds = Rect(*active["client"])
        return ["--activewindow", "--no-decoration", "--no-shadow"], bounds, bounds
    return [], None, None


def crop(image, source, bounds):
    width, height = image.get_width(), image.get_height()
    if abs(width * source.h - height * source.w) > max(source.w, source.h):
        return None
    if not bounds.valid or bounds.intersect(source) != bounds:
        return None
    if bounds == source:
        return image
    x = round((bounds.x - source.x) * width / source.w)
    y = round((bounds.y - source.y) * height / source.h)
    right = round((bounds.x + bounds.w - source.x) * width / source.w)
    bottom = round((bounds.y + bounds.h - source.y) * height / source.h)
    if right <= x or bottom <= y:
        return None
    return image.new_subpixbuf(x, y, right - x, bottom - y)


def decode_frame(pixels, width, height, stride, format):
    # QImage's RGB32 formats use native word order. RGBX8888 and RGBA8888
    # use byte order and can be passed to GdkPixbuf directly.
    if format in (4, 5, 6):
        rgba = bytearray(pixels)
        if sys.byteorder == "little":
            rgba[0::4], rgba[2::4] = pixels[2::4], pixels[0::4]
            rgba[3::4] = b"\xff" * (len(pixels) // 4)
        else:
            rgba[0::4], rgba[1::4], rgba[2::4] = pixels[1::4], pixels[2::4], pixels[3::4]
            rgba[3::4] = b"\xff" * (len(pixels) // 4)
        pixels = bytes(rgba)
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(pixels), GdkPixbuf.Colorspace.RGB,
                                           True, 8, width, height, stride)


def capture_area(bounds, cancel):
    with os.fdopen(os.memfd_create("select-to-speak-ocr"), "r+b") as image:
        fd = image.fileno()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            file_list = Gio.UnixFDList.new()
            args = GLib.Variant("(iiuua{sv}h)", (bounds.x, bounds.y, bounds.w, bounds.h, {}, file_list.append(fd)))
            reply, _ = bus.call_with_unix_fd_list_sync(
                SCREENSHOT, "/org/kde/KWin/ScreenShot2", SCREENSHOT, "CaptureArea",
                args, None, Gio.DBusCallFlags.NO_AUTO_START, 1200, file_list, None,
            )
        except GLib.Error as exc:
            LOG.debug("Direct KWin capture unavailable: %s", exc.message)
            return None
        meta, = reply.unpack()
        width, height = meta.get("width"), meta.get("height")
        stride, format = meta.get("stride"), meta.get("format")
        if ((width, height) != (bounds.w, bounds.h) or meta.get("type") != "raw"
                or format not in (4, 5, 6, 16, 17, 18) or not isinstance(stride, int)
                or stride < width * 4 or stride % 4):
            LOG.debug("KWin returned an unsupported screenshot layout")
            return None
        size = stride * height
        deadline = time.monotonic() + 0.5
        while os.fstat(fd).st_size < size:
            if cancel.wait(0.01):
                raise Cancelled
            if time.monotonic() >= deadline:
                return None
        return decode_frame(os.pread(fd, size, 0), width, height, stride, format)


def capture_spectacle(options, source, bounds, cancel):
    spectacle = shutil.which("spectacle")
    if not spectacle:
        return None
    env = os.environ.copy()
    for key in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID"):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="select-to-speak-", dir=os.environ.get("XDG_RUNTIME_DIR")) as folder:
        path = Path(folder) / "capture.png"
        result = command([spectacle, "--new-instance", "-b", "-n", *options, "-o", str(path)],
                         cancel, 3, env=env)
        if result is None or not path.is_file():
            return None
        try:
            return crop(GdkPixbuf.Pixbuf.new_from_file(str(path)), source, bounds)
        except GLib.Error:
            return None


def recognize(image, tesseract, language, layout, bounds, point, cancel):
    _, png = image.save_to_bufferv("png", [], [])
    output = command([tesseract, "stdin", "stdout", "-l", language, "--psm", layout, "tsv"],
                     cancel, 3, payload=png)
    if not output:
        return []
    size = image.get_width(), image.get_height()
    return paragraph_at(parse(output.decode("utf-8", errors="replace"), size, bounds), point)


def capture(request, base, cancel):
    if os.environ.get("SELECT_TO_SPEAK_OCR", "1") == "0":
        return None
    options, source, bounds = capture_region(request)
    point = request["point"]
    if bounds is None or not bounds.contains(*point):
        return None
    tesseract = shutil.which("tesseract")
    local = base / "runtime/ocr/usr/bin/tesseract"
    if tesseract is None and local.is_file():
        tesseract = str(local)
    if not tesseract:
        return None
    _, y = point
    bounds = bounds.intersect(Rect(bounds.x, y - 120, bounds.w, 480))
    image = capture_area(bounds, cancel)
    if image is None:
        image = capture_spectacle(options, source, bounds, cancel)
    if image is None:
        LOG.info("OCR could not capture the requested area")
        return None
    language = os.environ.get("SELECT_TO_SPEAK_OCR_LANGUAGE", "eng")
    words = recognize(image, tesseract, language, "3", bounds, point, cancel)
    if words:
        return ImageReading(words)

    # Keep the window's line width so the retry can read past the cursor.
    tile = bounds.intersect(Rect(bounds.x, y - 64, bounds.w, 256))
    detail = crop(image, bounds, tile)
    if detail is None:
        return None
    enlarged = detail.scale_simple(detail.get_width() * 2, detail.get_height() * 2, GdkPixbuf.InterpType.BILINEAR)
    for layout in ("6", "11"):
        words = recognize(enlarged, tesseract, language, layout, tile, point, cancel)
        if words:
            return ImageReading(words)
    LOG.info("OCR found no text near the pointer")
    return None
