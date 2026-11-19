"""Reading offsets and geometry shared by accessibility and playback."""

from dataclasses import dataclass
import re

SENTENCE_END = re.compile(r"""(?:[.!?。！？]+[’”"'»)}\]]*(?=\s|$)|\n+)""")
WORD = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)
ABBREVIATION = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|[A-Za-z])\.$", re.I)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def valid(self):
        return self.w > 0 and self.h > 0

    def contains(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def move(self, x, y):
        return Rect(self.x + x, self.y + y, self.w, self.h)

    def intersect(self, other):
        x, y = max(self.x, other.x), max(self.y, other.y)
        right = min(self.x + self.w, other.x + other.w)
        bottom = min(self.y + self.h, other.y + other.h)
        return Rect(x, y, right - x, bottom - y)


@dataclass(frozen=True)
class Piece:
    text: str
    node: object = None
    offset: int = 0

    def slice(self, start, end):
        offset = self.offset + start if self.offset >= 0 else -1
        return Piece(self.text[start:end], self.node, offset)


@dataclass(frozen=True)
class Utterance:
    text: str
    pieces: tuple[Piece, ...]


def from_word(text, offset):
    for word in WORD.finditer(text, max(0, text.rfind("\n", 0, offset) + 1)):
        if word.end() > offset:
            return word.start()
    return offset


def passages(pieces):
    """Split on prose boundaries while keeping the underlying text ranges."""
    text = "".join(piece.text for piece in pieces)
    cursor = 0
    ranges = []
    for end in SENTENCE_END.finditer(text):
        if end.group() == "." and ABBREVIATION.search(text[cursor:end.end()]):
            continue
        ranges.append((cursor, end.end()))
        cursor = end.end()
    ranges.append((cursor, len(text)))

    piece_index = piece_start = 0

    def bounded(start, end):
        nonlocal piece_index, piece_start
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if not text[start:end].strip(" \t\n.,!?:;"):
            return
        spans = []
        while piece_index < len(pieces) and piece_start < end:
            piece = pieces[piece_index]
            piece_end = piece_start + len(piece.text)
            if piece.node is not None and piece_end > start:
                spans.append(piece.slice(max(start - piece_start, 0), min(end, piece_end) - piece_start))
            if piece_end > end:
                break
            piece_index += 1
            piece_start = piece_end
        yield Utterance(re.sub(r"\s+", " ", text[start:end]), tuple(spans))

    for start, end in ranges:
        while end - start > 360:
            split = text.rfind(" ", start + 180, start + 360)
            split = start + 360 if split == -1 else split
            yield from bounded(start, split)
            start = split
        yield from bounded(start, end)
