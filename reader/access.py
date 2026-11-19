"""Translate KWin window coordinates and AT-SPI text to a reading."""

from dataclasses import dataclass
from functools import lru_cache
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi as A, GLib

from .model import Piece, Rect, from_word, passages

ROLE = A.Role
STATE = A.StateType
SCREEN = A.CoordType.SCREEN
WINDOW = A.CoordType.WINDOW
DOCUMENTS = {ROLE.DOCUMENT_WEB, ROLE.DOCUMENT_TEXT, ROLE.DOCUMENT_FRAME,
             ROLE.DOCUMENT_EMAIL, ROLE.DOCUMENT_PRESENTATION, ROLE.DOCUMENT_SPREADSHEET}
BLOCKS = DOCUMENTS | {ROLE.ARTICLE, ROLE.PARAGRAPH, ROLE.HEADING, ROLE.LIST_ITEM,
                      ROLE.BLOCK_QUOTE, ROLE.TABLE_CELL, ROLE.LANDMARK, ROLE.FORM}
LABELS = {ROLE.LINK, ROLE.LABEL, ROLE.STATIC, ROLE.IMAGE, ROLE.ICON, ROLE.PUSH_BUTTON,
          ROLE.CHECK_BOX, ROLE.RADIO_BUTTON, ROLE.MENU_ITEM, ROLE.PAGE_TAB, ROLE.LIST_ITEM,
          ROLE.TOGGLE_BUTTON, ROLE.TABLE_CELL, ROLE.COMBO_BOX, ROLE.HEADING, ROLE.TEXT}
MARK = "\ufffc"


class ProtectedText(Exception):
    pass


def attempt(method, *args, default=None):
    try:
        result = method(*args)
        return default if result is None else result
    except GLib.Error:
        return default


def role(node):
    return attempt(node.get_role, default=ROLE.INVALID)


def has(node, state):
    flags = node.get_state_set()
    return flags is not None and flags.contains(state)


def ancestry(node):
    while node is not None:
        yield node
        node = attempt(node.get_parent)


def children(node):
    for i in range(attempt(node.get_child_count, default=0)):
        child = attempt(node.get_child_at_index, i)
        if child is not None and not has(child, STATE.DEFUNCT):
            yield child


def embedded_child(node, offset):
    link_index = attempt(A.Hypertext.get_link_index, node, offset, default=-1)
    if link_index < 0:
        return None
    link = attempt(A.Hypertext.get_link, node, link_index)
    return link and attempt(link.get_object, 0)


def text_of(node):
    if role(node) != ROLE.PASSWORD_TEXT and attempt(node.get_text_iface) is not None:
        return attempt(A.Text.get_text, node, 0, -1, default="")
    return ""


def rect(raw):
    return Rect(raw.x, raw.y, raw.width, raw.height) if raw else Rect(0, 0, 0, 0)


def extents(node, mode):
    if attempt(node.get_component_iface) is not None:
        return rect(attempt(A.Component.get_extents, node, mode))
    return Rect(0, 0, 0, 0)


@dataclass
class Surface:
    node: object
    bounds: Rect
    mode: object
    dx: int
    dy: int

    @classmethod
    def create(cls, node, window):
        frame, client = Rect(*window["frame"]), Rect(*window["client"])
        screen = extents(node, SCREEN)
        mode = SCREEN if screen.valid else WINDOW
        reported = screen if screen.valid else extents(node, WINDOW)
        origins = [client, frame]
        if "buffer" in window:
            origins.append(Rect(*window["buffer"]))
        origin = min(origins, key=lambda r: abs(r.w - reported.w) + abs(r.h - reported.h))
        return cls(node, client, mode, origin.x - reported.x, origin.y - reported.y)

    def box(self, node):
        return extents(node, self.mode).move(self.dx, self.dy)

    def hit(self, point):
        x, y = point
        if not self.bounds.contains(x, y):
            return None
        return self._hit(self.node, x - self.dx, y - self.dy, set())

    def _hit(self, node, x, y, visited):
        visited.add(node)
        if attempt(node.get_component_iface) is not None:
            target = attempt(A.Component.get_accessible_at_point, node, x, y, self.mode)
            if target is not None and target not in visited and has(target, STATE.SHOWING):
                bounds = extents(target, self.mode)
                if not bounds.valid or bounds.contains(x, y):
                    return self._hit(target, x, y, visited)
        for child in reversed(list(children(node))):
            if child not in visited and has(child, STATE.SHOWING) and extents(child, self.mode).contains(x, y):
                return self._hit(child, x, y, visited)
        return node

    def offset(self, node, point, size):
        x, y = point
        if self.zero_height_text(node):
            return self.offset_from_geometry(node, x - self.dx, y - self.dy, size)
        return attempt(A.Text.get_offset_at_point, node, x - self.dx, y - self.dy, self.mode, default=-1)

    def character(self, node, offset):
        return rect(attempt(A.Text.get_character_extents, node, offset, self.mode))

    def zero_height_text(self, node):
        char = self.character(node, 0) if editable(node) else None
        return char is not None and char.w > 0 and char.h == 0

    def offset_from_geometry(self, node, x, y, size):
        # KTextEditor reports a zero-height glyph with its x origin applied
        # twice. Its offset-at-point implementation can also return a
        # different line from the one the pointer is over.
        origin = extents(node, self.mode)

        @lru_cache(maxsize=64)
        def glyph(index):
            return self.character(node, index)

        x += origin.x
        low, high = 0, size
        while low < high:
            middle = (low + high) // 2
            if glyph(middle).y <= y:
                low = middle + 1
            else:
                high = middle
        end = low
        if end == 0:
            return 0
        row = glyph(end - 1).y
        low, high = 0, end
        while low < high:
            middle = (low + high) // 2
            char = glyph(middle)
            if char.y < row or (char.y == row and char.x + char.w <= x):
                low = middle + 1
            else:
                high = middle
        return min(low, end - 1)

    def boxes(self, piece, clip):
        node = piece.node
        if not has(node, STATE.SHOWING):
            return
        start = piece.offset
        end = start + len(piece.text)
        if self.zero_height_text(node):
            regions = self.editor_boxes(node, start, end)
            for region in regions:
                visible = region.move(self.dx, self.dy).intersect(clip)
                if visible.valid:
                    yield visible
            return
        while start < end:
            line = attempt(A.Text.get_string_at_offset, node, start, A.TextGranularity.LINE)
            stop = min(line.end_offset, end) if line and line.end_offset > start else end
            # Some providers include a whole embedded link when it is the
            # end boundary. Use the two glyphs when they occupy the same line.
            first, last = self.character(node, start), self.character(node, stop - 1)
            if first.valid and last.valid and abs(first.y - last.y) < min(first.h, last.h):
                region = enclosing(first, last)
            else:
                region = rect(attempt(A.Text.get_range_extents, node, start, stop, self.mode))
            if region.valid:
                region = region.move(self.dx, self.dy).intersect(clip)
                if region.valid:
                    yield region
            start = stop

    def editor_boxes(self, node, start, end):
        origin = extents(node, self.mode)
        rows = {}
        for offset in range(start, end):
            glyph = self.character(node, offset)
            if glyph.w > 0:
                box = Rect(glyph.x - origin.x, glyph.y, glyph.w, max(12, round(glyph.w * 1.7)))
                rows[glyph.y] = enclosing(rows[glyph.y], box) if glyph.y in rows else box
        return rows.values()


def enclosing(first, last):
    x, y = min(first.x, last.x), min(first.y, last.y)
    return Rect(x, y, max(first.x + first.w, last.x + last.w) - x,
                max(first.y + first.h, last.y + last.h) - y)

def surface_for(window):
    if not window or window["pid"] <= 0:
        return None
    candidates = []
    is_chromium = False
    for app in children(A.get_desktop(0)):
        if attempt(app.get_process_id) != window["pid"]:
            continue
        is_chromium = attempt(app.get_toolkit_name, default="").lower() in {"chromium", "chrome"}
        for node in children(app):
            if has(node, STATE.SHOWING) or has(node, STATE.ACTIVE):
                bounds = extents(node, SCREEN)
                if bounds.valid or role(node) in {ROLE.FRAME, ROLE.DIALOG, ROLE.WINDOW}:
                    score = (attempt(node.get_name) == window["title"],
                             has(node, STATE.ACTIVE), bounds.valid)
                    candidates.append((score, node))
    if candidates:
        _, node = max(candidates, key=lambda pair: pair[0])
        if is_chromium:
            # Chromium enables the renderer accessibility tree on an ATK
            # attribute query. Give the renderer a cycle to publish its tree.
            attempt(node.get_attributes)
            time.sleep(0.08)
        return Surface.create(node, window)
    return None


def editable(node):
    return has(node, STATE.EDITABLE) or role(node) in {ROLE.ENTRY, ROLE.TERMINAL}


def visible_nodes(node, check):
    check()
    if not has(node, STATE.SHOWING) or role(node) == ROLE.PASSWORD_TEXT:
        return
    yield node
    for child in children(node):
        yield from visible_nodes(child, check)


def focus_in(surface, check):
    if surface is None:
        return None
    return next((node for node in visible_nodes(surface.node, check)
                 if has(node, STATE.FOCUSED) and (editable(node) or text_of(node))), None)


def scope_for(node, link=False):
    branch = list(ancestry(node))
    if editable(node):
        return node
    if link:
        container = None
        for parent in branch:
            kind = role(parent)
            if kind in {ROLE.PARAGRAPH, ROLE.LIST_ITEM, ROLE.TABLE_CELL, ROLE.HEADING}:
                return parent
            if kind in DOCUMENTS or kind == ROLE.ARTICLE:
                break
            if container is None and kind in {ROLE.SECTION, ROLE.PANEL, ROLE.GROUPING}:
                attributes = attempt(parent.get_attributes, default={})
                if (attributes.get("display") in {"block", "flex", "grid"}
                        and attempt(parent.get_child_count, default=0) > 1):
                    container = parent
        if container is not None:
            return container
    for parent in branch:
        if role(parent) == ROLE.ARTICLE:
            return parent
    for parent in branch:
        if role(parent) == ROLE.LANDMARK:
            attributes = attempt(parent.get_attributes, default={})
            if attributes.get("xml-roles") in {"main", "region", "navigation", "complementary",
                                                "contentinfo", "banner"}:
                return parent
        if role(parent) in DOCUMENTS:
            return parent
    for parent in branch:
        if role(parent) in {ROLE.PARAGRAPH, ROLE.LIST, ROLE.DIALOG, ROLE.SCROLL_PANE}:
            return parent
    return node


class Flow:
    def __init__(self, check):
        self.check = check
        self.pieces = []
        self.seen = set()

    def append(self, node, start=0, end=None, boundary=True, selected=False):
        self.check()
        if node in self.seen or role(node) == ROLE.PASSWORD_TEXT:
            return
        self.seen.add(node)
        kind = role(node)
        value = text_of(node)
        limit = len(value) if end is None else min(end, len(value))
        if boundary and kind in BLOCKS:
            self.pieces.append(Piece("\n"))
        if value:
            self.embedded(node, value, start, limit, selected)
        elif end is None:
            descendants = list(children(node))
            if descendants:
                for child in descendants:
                    self.append(child)
            elif kind in LABELS:
                name = attempt(node.get_name, default="")
                if name:
                    self.pieces.append(Piece(name, node, -1))
        if boundary and kind in BLOCKS:
            self.pieces.append(Piece("\n"))

    def embedded(self, node, value, start, end, selected):
        cursor = start
        for index in range(start, end):
            if value[index] != MARK:
                continue
            if cursor < index:
                self.pieces.append(Piece(value[cursor:index], node, cursor))
            child = embedded_child(node, index)
            if child is not None:
                ranges = list(selections(child)) if selected and role(child) != ROLE.PASSWORD_TEXT else []
                if ranges:
                    for low, high in ranges:
                        self.seen.discard(child)
                        self.append(child, low, high, selected=True)
                else:
                    self.append(child)
            cursor = index + 1
        if cursor < end:
            self.pieces.append(Piece(value[cursor:end], node, cursor))

    def from_node(self, node, offset, scope):
        self.append(node, offset)
        while node != scope:
            parent = attempt(node.get_parent)
            if parent is None:
                return
            value = text_of(parent)
            count = attempt(A.Hypertext.get_n_links, parent, default=0) if MARK in value else 0
            for i in range(count):
                link = attempt(A.Hypertext.get_link, parent, i)
                if link is not None and attempt(link.get_object, 0) == node:
                    self.append(parent, attempt(link.get_end_index, default=len(value)), boundary=False)
                    break
            else:
                self.following(parent, node)
            if role(parent) in BLOCKS:
                self.pieces.append(Piece("\n"))
            node = parent

    def following(self, parent, node):
        found = False
        for sibling in children(parent):
            if found:
                self.append(sibling)
            elif sibling == node:
                found = True


def selections(node):
    if attempt(node.get_text_iface) is not None:
        count = attempt(A.Text.get_n_selections, node, default=0)
        for i in range(count):
            selected = attempt(A.Text.get_selection, node, i)
            if selected is not None and selected.end_offset > selected.start_offset:
                yield selected.start_offset, selected.end_offset


def selected_flow(surface, focus, check):
    flow = Flow(check)
    previous_block = None

    def add(node, ranges):
        nonlocal previous_block
        block = next((item for item in ancestry(node) if role(item) in BLOCKS), node)
        for start, end in ranges:
            if flow.pieces:
                flow.pieces.append(Piece(" " if block == previous_block else "\n"))
            flow.seen.discard(node)
            flow.append(node, start, end, selected=True)
            previous_block = block

    if focus is not None and editable(focus):
        add(focus, selections(focus))
        if flow.pieces:
            return flow

    def visit(node):
        check()
        if not has(node, STATE.SHOWING) or role(node) == ROLE.PASSWORD_TEXT:
            return
        ranges = list(selections(node))
        if ranges:
            add(node, ranges)
        else:
            for child in children(node):
                visit(child)

    if surface is not None:
        visit(surface.node)
    return flow


def pointer_start(surface, hit, point):
    branch = list(ancestry(hit))
    if any(role(node) == ROLE.PASSWORD_TEXT for node in branch):
        raise ProtectedText
    for node in branch:
        if role(node) == ROLE.LINK:
            return node, 0, True
        if role(node) in DOCUMENTS:
            break
    block = next((node for node in branch if role(node) in
                  {ROLE.PARAGRAPH, ROLE.HEADING, ROLE.LIST_ITEM, ROLE.TABLE_CELL}), None)
    candidates = branch if block is None or hit == block else [block, *branch]
    for node in candidates:
        value = text_of(node)
        if not value.strip():
            continue
        offset = surface.offset(node, point, len(value))
        if 0 <= offset < len(value):
            if value[offset] == MARK:
                child = embedded_child(node, offset)
                if child is not None and role(child) == ROLE.LINK:
                    return child, 0, True
                return node, offset, False
            return node, from_word(value, offset), False
    if role(hit) in LABELS | BLOCKS and (text_of(hit) or attempt(hit.get_name)):
        return hit, 0, False
    return None


@dataclass
class Reading:
    surface: Surface
    pieces: list
    mode: str

    def utterances(self):
        return passages(self.pieces)

    def rectangles(self, utterance, scroll=False):
        def collect():
            result = []
            for piece in utterance.pieces:
                clip = self.surface.bounds
                for node in ancestry(piece.node):
                    if role(node) in DOCUMENTS | {ROLE.SCROLL_PANE}:
                        region = self.surface.box(node)
                        if region.valid:
                            clip = clip.intersect(region)
                if piece.offset < 0:
                    region = self.surface.box(piece.node).intersect(clip)
                    if region.valid:
                        result.append(region)
                else:
                    result.extend(self.surface.boxes(piece, clip))
            return result

        boxes = collect()
        if scroll and not boxes and utterance.pieces:
            piece = utterance.pieces[0]
            if piece.offset >= 0:
                attempt(A.Text.scroll_substring_to, piece.node, piece.offset,
                        piece.offset + len(piece.text), A.ScrollType.ANYWHERE)
                boxes = collect()
        return boxes


def resolve(request, check):
    point = request["point"]
    active = None if request.get("pointerOnly") else surface_for(request.get("active"))
    focus = focus_in(active, check)
    if active:
        selected = selected_flow(active, focus, check)
        if any(piece.text.strip() for piece in selected.pieces):
            return Reading(active, selected.pieces, "selection")

    hovered = active if active and request.get("active") == request.get("hovered") else surface_for(request.get("hovered"))
    hit = hovered and hovered.hit(point)
    caret = None
    if focus is not None:
        value = text_of(focus)
        offset = attempt(A.Text.get_caret_offset, focus, default=-1) if value else -1
        if 0 <= offset < len(value):
            caret = focus, offset if editable(focus) else from_word(value, offset)
    if focus is not None and editable(focus) and (hovered is None or (hit and focus in ancestry(hit))):
        if caret:
            return reading_from(active, "caret", check, *caret)
    start = hit and pointer_start(hovered, hit, point)
    if start:
        return reading_from(hovered, "pointer", check, *start)
    if caret:
        return reading_from(active, "caret", check, *caret)
    return None


def reading_from(surface, mode, check, node, offset, link=False):
    flow = Flow(check)
    flow.from_node(node, offset, scope_for(node, link))
    return Reading(surface, flow.pieces, mode)
