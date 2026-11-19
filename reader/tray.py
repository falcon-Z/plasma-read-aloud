"""Plasma system tray entry and speech rate controls."""

import logging

from gi.repository import Gio, GLib, Gtk

from .settings import MIN_RATE, MAX_RATE

LOG = logging.getLogger(__name__)
ITEM = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
DISPLAY_NAME = "Select to Speak"
ICON = "audio-headphones-symbolic"
XML = f"""
<node>
  <interface name="{ITEM}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""


class Tray:
    def __init__(self, bus, change_rate, stop):
        self.bus = bus
        self.change_rate = change_rate
        self.stop = stop
        self.window = None
        self.rate = MIN_RATE
        self.active = False
        self.activation_token = ""
        self.info = Gio.DBusNodeInfo.new_for_xml(XML)
        bus.register_object(ITEM_PATH, self.info.interfaces[0], self.handle, self.get_property, None)
        self.watcher = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self.register, None,
        )

    def register(self, bus, name, owner):
        def finished(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error as exc:
                LOG.warning("Could not register the tray icon: %s", exc.message)

        bus.call(
            name, WATCHER_PATH, WATCHER, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (ITEM_PATH,)), None, Gio.DBusCallFlags.NONE,
            1500, None, finished,
        )

    def get_property(self, bus, sender, path, interface, name):
        if name == "ToolTip":
            phase = "Reading aloud" if self.active else "Alt+Esc to read"
            return GLib.Variant("(sa(iiay)ss)", (ICON, [], DISPLAY_NAME, f"{phase} · {self.rate:.2f}×"))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        return GLib.Variant("s", {
            "Category": "ApplicationStatus",
            "Id": "select-to-speak",
            "Title": DISPLAY_NAME,
            "Status": "Active",
            "IconName": ICON,
        }[name])

    def handle(self, bus, sender, path, iface, method, parameters, reply):
        if method == "ProvideXdgActivationToken":
            self.activation_token, = parameters.unpack()
        elif method == "SecondaryActivate":
            self.stop()
        else:
            self.show()
        reply.return_value(None)

    def show(self):
        if self.window is None:
            self.build_window()
        self.window.realize()
        if self.activation_token:
            self.window.get_surface().set_startup_id(self.activation_token)
            self.activation_token = ""
        self.window.present()

    def build_window(self):
        window = self.window = Gtk.Window(title=DISPLAY_NAME, default_width=345, resizable=False,
                                          hide_on_close=True)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16,
                        margin_bottom=16, margin_start=18, margin_end=18)
        window.set_child(panel)
        row = Gtk.Box(spacing=8)
        panel.append(row)
        label = Gtk.Label(label="_Speech rate", use_underline=True, xalign=0, hexpand=True)
        label.add_css_class("heading")
        row.append(label)
        self.value = Gtk.Label(label=f"{self.rate:.2f}×", xalign=1, width_chars=6)
        self.value.add_css_class("numeric")
        row.append(self.value)

        self.rate_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, MIN_RATE, MAX_RATE, 0.05)
        self.rate_slider.set_draw_value(False)
        self.rate_slider.set_digits(2)
        self.rate_slider.set_value(self.rate)
        self.rate_slider.set_hexpand(True)
        panel.append(self.rate_slider)
        label.set_mnemonic_widget(self.rate_slider)
        self.slider_handler = self.rate_slider.connect("value-changed", self.adjust_rate)

        panel.append(Gtk.Separator())
        footer = Gtk.Box(spacing=12)
        panel.append(footer)
        hint = Gtk.Label(label="Alt+Esc to read\nTap Ctrl to stop", xalign=0, hexpand=True)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        footer.append(hint)
        self.stop_button = Gtk.Button(label="Stop reading", sensitive=self.active)
        self.stop_button.connect("clicked", lambda *_: self.stop())
        footer.append(self.stop_button)
        self.error = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.error.add_css_class("error")
        panel.append(self.error)

    def adjust_rate(self, slider):
        try:
            self.change_rate(slider.get_value())
            self.error.set_visible(False)
        except OSError:
            self.error.set_label("The speech rate could not be saved.")
            self.error.set_visible(True)
        self.sync_window()

    def sync_window(self):
        if self.window is None:
            return
        self.value.set_label(f"{self.rate:.2f}×")
        if self.rate_slider.get_value() != self.rate:
            self.rate_slider.handler_block(self.slider_handler)
            self.rate_slider.set_value(self.rate)
            self.rate_slider.handler_unblock(self.slider_handler)
        self.stop_button.set_sensitive(self.active)

    def update(self, active, rate):
        if (self.active, self.rate) == (active, rate):
            return
        self.active, self.rate = active, rate
        self.sync_window()
        self.bus.emit_signal(None, ITEM_PATH, ITEM, "NewToolTip", None)
