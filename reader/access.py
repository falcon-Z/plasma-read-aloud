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
INTERACTIVE = {ROLE.PUSH_BUTTON, ROLE.PUSH_BUTTON_MENU, ROLE.TOGGLE_BUTTON, ROLE.CHECK_BOX,
               ROLE.RADIO_BUTTON, ROLE.MENU, ROLE.MENU_ITEM, ROLE.CHECK_MENU_ITEM,
               ROLE.RADIO_MENU_ITEM, ROLE.PAGE_TAB, ROLE.COMBO_BOX, ROLE.SLIDER, ROLE.SPIN_BUTTON,
               ROLE.DIAL, ROLE.SCROLL_BAR, ROLE.PROGRESS_BAR, ROLE.TREE_ITEM, ROLE.DESKTOP_ICON}
if hasattr(ROLE, "SWITCH"):
    INTERACTIVE.add(ROLE.SWITCH)
CONTROLS = INTERACTIVE | {ROLE.TABLE_ROW, ROLE.COLUMN_HEADER, ROLE.ROW_HEADER,
                          ROLE.TABLE_COLUMN_HEADER, ROLE.TABLE_ROW_HEADER}
LABELS = CONTROLS | {ROLE.LINK, ROLE.LABEL, ROLE.STATIC, ROLE.IMAGE, ROLE.ICON, ROLE.LIST_ITEM,
                     ROLE.TABLE_CELL, ROLE.HEADING, ROLE.TEXT, ROLE.CAPTION, ROLE.STATUS_BAR}
POPUPS = {ROLE.ALERT, ROLE.DIALOG, ROLE.NOTIFICATION, ROLE.POPUP_MENU, ROLE.TOOL_TIP}
WINDOW_ROLES = {ROLE.FRAME, ROLE.DIALOG, ROLE.WINDOW}
TEXT_TARGETS = LABELS | {ROLE.PARAGRAPH, ROLE.BLOCK_QUOTE, ROLE.ENTRY, ROLE.TERMINAL}
PROSE_EXCLUDED = INTERACTIVE | {ROLE.ENTRY, ROLE.TERMINAL, ROLE.TOOL_BAR, ROLE.MENU_BAR, ROLE.STATUS_BAR}
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


def ancestry(node, root=None):
    while node is not None:
        yield node
        if node == root:
            break
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
    def create(cls, node, window, toolkit):
        client = Rect(*window["client"])
        screen = extents(node, SCREEN)
        mode, reported = SCREEN, screen
        # GTK on Wayland can return a valid size with a zero screen origin
        # for every widget. Window coordinates retain the widgets' offsets.
        if not screen.valid or (toolkit == "gtk" and (screen.x, screen.y) == (0, 0)):
            local = extents(node, WINDOW)
            if local.valid or not screen.valid:
                mode, reported = WINDOW, local
        origins = window_rects(window)
        origin = min(origins, key=lambda r: abs(r.w - reported.w) + abs(r.h - reported.h))
        return cls(node, client if client.valid else origin, mode,
                   origin.x - reported.x, origin.y - reported.y)

    def box(self, node):
        return extents(node, self.mode).move(self.dx, self.dy)

    def hits(self, point, check):
        x, y = point
        if self.bounds.contains(x, y):
            yield from self._hits(self.node, x - self.dx, y - self.dy, set(), check)

    def _hits(self, node, x, y, visited, check, direct=False):
        check()
        visited.add(node)
        bounds = extents(node, self.mode)
        if role(node) == ROLE.PASSWORD_TEXT:
            if direct or bounds.contains(x, y):
                yield node
            return
        if attempt(node.get_component_iface) is not None:
            target = attempt(A.Component.get_accessible_at_point, node, x, y, self.mode)
            if target is not None and target not in visited and has(target, STATE.SHOWING):
                target_bounds = extents(target, self.mode)
                if not target_bounds.valid or target_bounds.contains(x, y):
                    yield from self._hits(target, x, y, visited, check, direct=True)
        for child in reversed(list(children(node))):
            check()
            if child in visited or not has(child, STATE.SHOWING):
                continue
            child_bounds = extents(child, self.mode)
            if child_bounds.contains(x, y) or (not child_bounds.valid and attempt(child.get_child_count, default=0)):
                yield from self._hits(child, x, y, visited, check)
        if bounds.contains(x, y):
            yield node

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


def window_rects(window):
    return [Rect(*window[key]) for key in ("client", "frame", "buffer")
            if key in window and min(window[key][2:]) > 0]


def surface_for(window, check):
    if not window or window["pid"] <= 0:
        return None
    origins = window_rects(window)
    if not origins:
        return None
    desktop = attempt(A.get_desktop, 0)
    if desktop is None:
        return None
    popup = window.get("popup", False)
    roots = []
    for app in children(desktop):
        check()
        if attempt(app.get_process_id) != window["pid"]:
            continue
        toolkit = attempt(app.get_toolkit_name, default="").lower()
        for node in children(app):
            check()
            if has(node, STATE.SHOWING) or has(node, STATE.ACTIVE):
                roots.append((node, toolkit))

    def best(nodes):
        candidates = []
        for node, toolkit in nodes:
            check()
            score = surface_score(node, window["title"], origins, popup)
            if score is not None:
                candidates.append((score, node, toolkit))
        return max(candidates, key=lambda item: item[0], default=None)

    match = best(roots)
    if popup and match is None:
        # Some toolkits report a separate popup surface as part of the owner window's tree.
        match = best((node, toolkit) for root, toolkit in roots
                     for node in visible_nodes(root, check) if role(node) in POPUPS | {ROLE.MENU})
    if match is None:
        return None
    _, node, toolkit = match
    if toolkit in {"chromium", "chrome", "electron"}:
        # Chromium enables the renderer accessibility tree on an ATK
        # attribute query. Give the renderer a cycle to publish its tree.
        attempt(node.get_attributes)
        time.sleep(0.08)
        check()
    return Surface.create(node, window, toolkit)


def surface_score(node, title, origins, popup):
    screen = extents(node, SCREEN)
    bounds = screen if screen.valid else extents(node, WINDOW)
    if not bounds.valid and role(node) not in WINDOW_ROLES:
        return None
    size_error = min(abs(r.w - bounds.w) + abs(r.h - bounds.h) for r in origins)
    if popup and (not bounds.valid or size_error > 32):
        return None
    position_error = min(abs(r.x - bounds.x) + abs(r.y - bounds.y) for r in origins)
    title_matches = bool(title) and attempt(node.get_name) == title
    return screen in origins, title_matches, -size_error, -position_error, has(node, STATE.ACTIVE)


def editable(node):
    return has(node, STATE.EDITABLE) or role(node) in {ROLE.ENTRY, ROLE.TERMINAL}


def visible_nodes(node, check):
    check()
    if not has(node, STATE.SHOWING):
        return
    yield node
    if role(node) == ROLE.PASSWORD_TEXT:
        return
    for child in children(node):
        yield from visible_nodes(child, check)


def focus_in(surface, check, editable_only=False):
    if surface is None:
        return None
    focused = None
    for node in visible_nodes(surface.node, check):
        if not has(node, STATE.FOCUSED):
            continue
        kind = role(node)
        if kind == ROLE.PASSWORD_TEXT or editable(node):
            return node
        if not editable_only and kind not in WINDOW_ROLES:
            if kind in TEXT_TARGETS:
                return node
            focused = node
    return focused


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
    def __init__(self, check, visible_only=False, prose=False):
        self.check = check
        self.visible_only = visible_only
        self.prose = prose
        self.pieces = []
        self.seen = set()

    def append(self, node, start=0, end=None, boundary=True, selected=False):
        self.check()
        kind = role(node)
        if (node in self.seen or kind == ROLE.PASSWORD_TEXT
                or (self.prose and (kind in PROSE_EXCLUDED or has(node, STATE.EDITABLE)))
                or (self.visible_only and not has(node, STATE.SHOWING))):
            return
        self.seen.add(node)
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
                    if self.visible_only and self.pieces:
                        self.pieces.append(Piece("\n"))
                    self.descend(child, selected)
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
                self.descend(child, selected)
            cursor = index + 1
        if cursor < end:
            self.pieces.append(Piece(value[cursor:end], node, cursor))

    def descend(self, child, selected):
        # Chromium includes embedded fields in the document's selection
        # offsets, even though their values are not part of the selected page.
        if selected and (role(child) == ROLE.PASSWORD_TEXT or editable(child)):
            return
        ranges = list(selections(child)) if selected else []
        if ranges:
            for low, high in ranges:
                self.seen.discard(child)
                self.append(child, low, high, selected=True)
        else:
            self.append(child, selected=selected)

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


def selected_flow(surface, focus, check, *, focus_only=False):
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

    if focus is not None and role(focus) != ROLE.PASSWORD_TEXT and editable(focus):
        add(focus, selections(focus))
        if flow.pieces or focus_only:
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

    if focus_only:
        visit(focus)
    elif surface is not None:
        visit(surface.node)
    return flow


def text_start(surface, branch, point):
    block = next((node for node in branch if role(node) in
                  {ROLE.PARAGRAPH, ROLE.HEADING, ROLE.LIST_ITEM, ROLE.TABLE_CELL}), None)
    candidates = branch if block is None or branch[0] == block else [block, *branch]
    for node in candidates:
        if role(node) not in TEXT_TARGETS or not surface.box(node).contains(*point):
            continue
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


def request_target(request):
    return request.get("target", "pointer" if request.get("pointerOnly") else "auto")


def resolve(request, check):
    target = request_target(request)
    active_window, hovered_window = request.get("active"), request.get("hovered")
    same_window = active_window == hovered_window
    active = surface_for(active_window, check) if target != "pointer" and active_window else None
    if target == "focus":
        focus = focus_in(active, check)
        return focused_reading(active, focus, check) if focus is not None else None

    focus = focus_in(active, check, editable_only=True)
    if active:
        selected = selected_flow(active, focus, check)
        if readable(selected.pieces):
            return Reading(active, selected.pieces, "selection")

    hovered = active if active and same_window else surface_for(hovered_window, check)
    if hovered:
        return pointer_reading(hovered, request["point"], check, focus if same_window else None)
    return None


def focused_reading(surface, node, check):
    kind = role(node)
    if kind == ROLE.PASSWORD_TEXT:
        raise ProtectedText
    if kind in CONTROLS or kind == ROLE.LINK:
        return element_reading(surface, node, check)

    selected = selected_flow(surface, node, check, focus_only=True)
    if readable(selected.pieces):
        return Reading(surface, selected.pieces, "selection")
    if editable(node):
        value = text_of(node)
        offset = attempt(A.Text.get_caret_offset, node, default=-1) if value else -1
        if value and 0 <= offset <= len(value):
            if offset == len(value):
                offset = value.rfind("\n", 0, len(value.rstrip())) + 1
            flow = Flow(check)
            flow.append(node, offset)
            if readable(flow.pieces):
                return Reading(surface, flow.pieces, "caret")
        return element_reading(surface, node, check)
    if kind in DOCUMENTS:
        flow = Flow(check, visible_only=True, prose=True)
        flow.append(node)
        return Reading(surface, flow.pieces, "focus")
    return element_reading(surface, node, check)


def pointer_reading(surface, point, check, focus=None):
    for hit in surface.hits(point, check):
        branch = list(ancestry(hit, surface.node))
        kinds = [role(node) for node in branch]
        if ROLE.PASSWORD_TEXT in kinds:
            raise ProtectedText
        if focus is not None and focus in branch and editable(focus):
            value = text_of(focus)
            offset = attempt(A.Text.get_caret_offset, focus, default=-1) if value else -1
            if 0 <= offset < len(value):
                reading = reading_from(surface, "caret", check, focus, offset)
                if readable(reading.pieces):
                    return reading

        in_document = any(kind in DOCUMENTS for kind in kinds)
        for node, kind in zip(branch, kinds):
            if kind in DOCUMENTS:
                break
            if not surface.box(node).contains(*point):
                continue
            if kind == ROLE.LINK:
                reading = reading_from(surface, "pointer", check, node, 0, True)
                if readable(reading.pieces):
                    return reading
            if kind in CONTROLS or (kind == ROLE.LIST_ITEM and not in_document):
                reading = element_reading(surface, node, check)
                if readable(reading.pieces):
                    return reading

        if not in_document and kinds[0] in {ROLE.LABEL, ROLE.STATIC}:
            reading = element_reading(surface, hit, check)
            if readable(reading.pieces):
                return reading

        start = text_start(surface, branch, point)
        if start:
            reading = reading_from(surface, "pointer", check, *start)
            if readable(reading.pieces):
                return reading

        if in_document and kinds[0] in {ROLE.LABEL, ROLE.STATIC, ROLE.HEADING} and text_of(hit).strip():
            reading = reading_from(surface, "pointer", check, hit, 0)
            if readable(reading.pieces):
                return reading
        if kinds[0] in TEXT_TARGETS - BLOCKS:
            reading = element_reading(surface, hit, check)
            if readable(reading.pieces):
                return reading
        if not in_document and kinds[0] in POPUPS:
            name = attempt(hit.get_name, default="")
            if name.strip():
                return Reading(surface, [Piece(name, hit, -1)], "element")
    return None


def readable(pieces):
    return any(piece.text.strip(" \t\r\n.,!?:;") for piece in pieces)


def element_reading(surface, node, check):
    name = attempt(node.get_name, default="")
    if name.strip():
        return Reading(surface, [Piece(name, node, -1)], "element")
    flow = Flow(check, visible_only=True)
    flow.append(node)
    if not readable(flow.pieces):
        description = attempt(node.get_description, default="")
        if description.strip():
            flow.pieces.append(Piece(description, node, -1))
    return Reading(surface, flow.pieces, "element")


def reading_from(surface, mode, check, node, offset, link=False):
    flow = Flow(check, prose=mode == "pointer" and not editable(node))
    flow.from_node(node, offset, scope_for(node, link))
    return Reading(surface, flow.pieces, mode)
