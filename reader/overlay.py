"""A click-through highlight drawn on native Wayland layer surfaces."""

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, Gtk, Gtk4LayerShell as Layer

from .model import Rect


class ScreenHighlight:
    def __init__(self, monitor):
        self.monitor = monitor
        self.rectangles = []
        window = self.window = Gtk.Window(decorated=False, focusable=False)
        window.add_css_class("reading-highlight")
        Layer.init_for_window(window)
        Layer.set_namespace(window, "select-to-speak-highlight")
        Layer.set_layer(window, Layer.Layer.OVERLAY)
        Layer.set_keyboard_mode(window, Layer.KeyboardMode.NONE)
        Layer.set_exclusive_zone(window, -1)
        Layer.set_monitor(window, monitor)
        for edge in (Layer.Edge.TOP, Layer.Edge.BOTTOM, Layer.Edge.LEFT, Layer.Edge.RIGHT):
            Layer.set_anchor(window, edge, True)
        window.connect("realize", self._realize)
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self._draw)
        window.set_child(self.area)

    @staticmethod
    def _realize(window):
        window.get_surface().set_input_region(cairo.Region())

    def update(self, boxes):
        g = self.monitor.get_geometry()
        output = Rect(g.x, g.y, g.width, g.height)
        self.rectangles = [
            region.move(-g.x, -g.y) for box in boxes
            if (region := box.intersect(output)).valid
        ]
        self.window.set_visible(bool(self.rectangles))
        if self.rectangles:
            self.area.queue_draw()

    def _draw(self, area, cr, width, height):
        cr.set_line_width(1.5)
        for rect in self.rectangles:
            x, y, w, h = rect.x + 0.75, rect.y + 0.75, rect.w - 1.5, rect.h - 1.5
            if w <= 0 or h <= 0:
                continue
            radius = min(4, w / 2, h / 2)
            cr.new_sub_path()
            cr.arc(x + w - radius, y + radius, radius, -1.571, 0)
            cr.arc(x + w - radius, y + h - radius, radius, 0, 1.571)
            cr.arc(x + radius, y + h - radius, radius, 1.571, 3.142)
            cr.arc(x + radius, y + radius, radius, 3.142, 4.713)
            cr.close_path()
            cr.set_source_rgba(1, 0.76, 0.16, 0.23)
            cr.fill_preserve()
            cr.set_source_rgba(1, 0.67, 0.08, 0.9)
            cr.stroke()


class Highlight:
    def __init__(self):
        Gtk.init()
        display = Gdk.Display.get_default()
        if display is None or not Layer.is_supported():
            raise RuntimeError("The reader needs a Wayland session with layer-shell support.")
        css = Gtk.CssProvider()
        css.load_from_string("window.reading-highlight { background: transparent; box-shadow: none; }")
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.monitors = display.get_monitors()
        self.windows = {}
        self.boxes = []
        self.monitors.connect("items-changed", lambda *_: self.update(self.boxes))

    def update(self, boxes):
        self.boxes = boxes
        current = {self.monitors.get_item(i) for i in range(self.monitors.get_n_items())}
        for monitor in self.windows.keys() - current:
            self.windows.pop(monitor).window.destroy()
        for monitor in current:
            if boxes and monitor not in self.windows:
                self.windows[monitor] = ScreenHighlight(monitor)
            if monitor in self.windows:
                self.windows[monitor].update(boxes)
