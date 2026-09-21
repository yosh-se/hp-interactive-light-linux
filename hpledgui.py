#!/usr/bin/env python3
"""hpledgui - small window for the HP Interactive Light, modelled on HP's Windows app.

Tabs: Help, Test, Quiz, Group, About. It only talks to the hpledd daemon over its socket,
so it needs no root. Requires PyGObject with GTK 3 (python3-gi, standard on Ubuntu desktop).
"""
import os
import socket
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

SOCKET = os.environ.get("HPLEDD_SOCKET", "/run/hpledd/hpledd.sock")
DOT_COLOURS = {"white": "#f2f2f2", "green": "#2bc255", "red": "#ff5257"}
TABS = [("help", "Help"), ("test", "Test"), ("quiz", "Quiz"), ("group", "Group"), ("about", "About")]
REFRESH_SECONDS = 2
# Tabs where white is a real choice; hpledd keeps the light off there until one is picked.
COLOUR_PAGES = ("quiz", "group")

CSS = b"""
window.hpilwin, .hpil { background-color: #2b2b2b; color: #e8e8e8; }
.hpil scrolledwindow, .hpil viewport { background-color: #2b2b2b; }
.hpil label.status { color: #9a9a9a; padding: 6px 12px; font-size: 9pt; }
.hpil label.about { color: #cfcfcf; padding: 8px 4px; }
button.tab { background-image: none; background-color: transparent; border: none;
             box-shadow: none; color: #8a8a8a; padding: 10px 4px; border-radius: 0;
             text-shadow: none; }
button.tab.selected { color: #ffffff; font-weight: bold; }
button.big { background-image: none; background-color: #4a4a4a; color: #ffffff;
             font-weight: bold; padding: 8px 12px; border: 3px solid transparent;
             border-radius: 2px; text-shadow: none; box-shadow: none; }
button.big:hover { background-color: #5a5a5a; }
button.big.white { background-color: #f2f2f2; color: #222222; }
button.big.green { background-color: #1e9e45; color: #ffffff; }
button.big.red { background-color: #d13438; color: #ffffff; }
button.big.selected { border-color: #5aa9ff; }
"""


def send(command):
    """Send one command to hpledd; returns (ok, text). Raises OSError if it is unreachable."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(SOCKET)
        s.sendall(command.encode() + b"\n")
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            reply += chunk
    finally:
        s.close()
    text = reply.decode(errors="replace").strip()
    return text.startswith("ok"), (text[3:] if text.startswith("ok ") else text)


def parse_status(text):
    """'led=white help=off mode=quiz:b ...' -> {'led': 'white', 'help': 'off', 'mode': 'quiz:b'}"""
    return dict(tok.split("=", 1) for tok in text.split() if "=" in tok)


def set_class(widget, name, on):
    ctx = widget.get_style_context()
    (ctx.add_class if on else ctx.remove_class)(name)


class Panel(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("hpil")
        self.tab_buttons = {}
        self.tab_dots = {}
        self.quiz_buttons = {}
        self.group_buttons = {}
        self.current_tab = None
        self.page_sent = "none"

        tab_bar = Gtk.Box(homogeneous=True)
        self.stack = Gtk.Stack()
        self.stack.set_margin_start(16)
        self.stack.set_margin_end(16)
        self.stack.set_margin_top(12)
        self.stack.set_margin_bottom(12)
        self.stack.set_vexpand(True)

        for name, title in TABS:
            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.get_style_context().add_class("tab")
            row = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER)
            row.pack_start(Gtk.Label(label=title), False, False, 0)
            dot = Gtk.Label()
            dot.set_no_show_all(True)
            row.pack_start(dot, False, False, 0)
            button.add(row)
            button.connect("clicked", self.on_tab, name)
            tab_bar.pack_start(button, True, True, 0)
            self.tab_buttons[name] = button
            self.tab_dots[name] = dot

        self.help_btn = self.big_button("REQUEST HELP", self.on_help)
        self.test_btn = self.big_button("TURN ON TEST MODE", self.on_test)
        self.stack.add_named(self.help_btn, "help")
        self.stack.add_named(self.test_btn, "test")
        self.stack.add_named(self.choice_row("quiz", (("a", "A", "white"), ("b", "B", "green"),
                                                      ("c", "C", "red")), self.quiz_buttons), "quiz")
        self.stack.add_named(self.choice_row("group", (("white", "WHITE", "white"),
                                                       ("green", "GREEN", "green"),
                                                       ("red", "RED", "red")), self.group_buttons), "group")
        about = Gtk.Label(label=(
            "Drives the status light on this computer through the hpledd service.\n\n"
            "Help: blinking red\nTest: solid green\n"
            "Quiz: A white, B green, C red\nGroup: pick your group's colour\n\n"
            "With nothing selected the light shows internet (white) and low battery (blinking red)."))
        about.set_line_wrap(True)
        about.set_xalign(0)
        about.get_style_context().add_class("about")
        about_scroll = Gtk.ScrolledWindow()
        about_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        about_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        about_scroll.set_min_content_height(24)  # lets the window shrink; text scrolls
        about_scroll.add(about)
        self.stack.add_named(about_scroll, "about")

        self.status = Gtk.Label(xalign=0)
        self.status.set_no_show_all(True)  # only shown when there is something to tell
        self.status.set_line_wrap(True)  # a long message must not widen the window
        self.status.get_style_context().add_class("status")

        self.pack_start(tab_bar, False, False, 0)
        self.pack_start(self.stack, True, True, 0)
        self.pack_start(self.status, False, False, 0)
        self.show_tab("help")

    @staticmethod
    def big_button(label, handler=None, *args):
        button = Gtk.Button(label=label)
        button.get_style_context().add_class("big")
        if handler:
            button.connect("clicked", handler, *args)
        return button

    def choice_row(self, kind, choices, registry):
        row = Gtk.Box(spacing=10, homogeneous=True)
        for key, label, colour in choices:
            button = self.big_button(label, self.on_choice, kind, key)
            button.get_style_context().add_class(colour)
            button.set_tooltip_text(colour.capitalize())
            row.pack_start(button, True, True, 0)
            registry[key] = button
        return row

    def show_tab(self, name):
        changed = name != self.current_tab
        self.current_tab = name
        self.stack.set_visible_child_name(name)
        for tab, button in self.tab_buttons.items():
            set_class(button, "selected", tab == name)
        if changed:
            self.sync_page()

    def sync_page(self):
        """Tell hpledd which colour page is showing so it doesn't show internet-white there."""
        page = self.current_tab if self.current_tab in COLOUR_PAGES else "none"
        if page == "none" and self.page_sent == "none":
            return
        self.page_sent = page
        self.command(f"page {page}")

    def close(self):
        if self.page_sent != "none":
            try:
                send("page none")
            except OSError:
                pass

    def on_tab(self, _button, name):
        self.show_tab(name)

    def on_help(self, _button):
        self.command("help off" if self.state.get("help") == "on" else "help on")

    def on_test(self, _button):
        self.command("test off" if self.state.get("mode") == "test" else "test on")

    def on_choice(self, _button, kind, key):
        # clicking the selected answer/group again clears it
        self.command(f"{kind} off" if self.state.get("mode") == f"{kind}:{key}" else f"{kind} {key}")

    state = {}

    def command(self, line):
        try:
            ok, text = send(line)
        except OSError:
            self.set_offline()
            return
        if ok:
            self.apply_state(parse_status(text))
        else:
            self.set_status(f"hpledd: {text}")

    def refresh(self):
        # while a colour page is showing, the status poll doubles as hpledd's "page is open" heartbeat
        line = f"page {self.current_tab}" if self.current_tab in COLOUR_PAGES else "status"
        try:
            _ok, text = send(line)
        except OSError:
            self.set_offline()
            return True
        self.apply_state(parse_status(text))
        return True  # keep the GLib timer running

    def set_status(self, text):
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def set_offline(self):
        self.stack.set_sensitive(False)
        self.set_status("hpledd is not running (sudo systemctl start hpledd)")

    def apply_state(self, st):
        self.state = st
        self.stack.set_sensitive(True)
        help_on = st.get("help") == "on"
        mode = st.get("mode", "none")
        kind, _, arg = mode.partition(":")

        self.help_btn.set_label("CANCEL HELP" if help_on else "REQUEST HELP")
        set_class(self.help_btn, "red", help_on)
        self.test_btn.set_label("TURN OFF TEST MODE" if kind == "test" else "TURN ON TEST MODE")
        set_class(self.test_btn, "green", kind == "test")
        for key, button in self.quiz_buttons.items():
            set_class(button, "selected", mode == f"quiz:{key}")
        for key, button in self.group_buttons.items():
            set_class(button, "selected", mode == f"group:{key}")

        quiz_colour = {"a": "white", "b": "green", "c": "red"}.get(arg)
        dots = {"help": "red" if help_on else None,
                "test": "green" if kind == "test" else None,
                "quiz": quiz_colour if kind == "quiz" else None,
                "group": arg if kind == "group" else None}
        for tab, colour in dots.items():
            dot = self.tab_dots[tab]
            if colour:
                dot.set_markup(f'<span foreground="{DOT_COLOURS[colour]}" size="small">●</span>')
                dot.show()
            else:
                dot.hide()

        # only worth a line when the light does something the user didn't ask for
        low = st.get("reason") == "low-battery"
        self.set_status("Battery low: the light blinks red until the charger is plugged in" if low else "")


class Window(Gtk.Window):
    def __init__(self):
        super().__init__(title="HP Interactive Light")
        self.get_style_context().add_class("hpilwin")
        self.set_default_size(380, 250)
        self.panel = Panel()
        self.add(self.panel)
        self.connect("destroy", self.on_destroy)

    def on_destroy(self, _window):
        self.panel.close()
        Gtk.main_quit()


def load_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main():
    # must match StartupWMClass in hpledgui.desktop so the window groups with its launcher
    GLib.set_prgname("hpledgui")
    GLib.set_application_name("HP Interactive Light")
    Gtk.Window.set_default_icon_name("hpledgui")
    load_css()
    window = Window()
    window.show_all()
    window.panel.refresh()
    GLib.timeout_add_seconds(REFRESH_SECONDS, window.panel.refresh)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
