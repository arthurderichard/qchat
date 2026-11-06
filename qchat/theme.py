"""Thèmes clair et sombre de QChat (style « Bulles QChat ») et suivi du réglage du bureau.

Mode « system » (par défaut) : suit la préférence clair/sombre du bureau, lue via le portail
XDG (org.freedesktop.appearance color-scheme, compris par GNOME, KDE...) avec repli sur
org.gnome.desktop.interface, et mise à jour en direct quand elle change.
"""

import logging
import os
from string import Template

from gi.repository import Gdk, Gio, GLib, Gtk

log = logging.getLogger("qchat")

MODES = {"system": "Système", "light": "Clair", "dark": "Sombre"}
ACCENT = "#e8412d"

PALETTES = {
    "light": {
        "accent": ACCENT, "accent_hover": "#d13722",
        "bg": "#ffffff", "fg": "#1b1b1b", "muted": "#7a7f87", "border": "#ebebeb", "hover": "#f4f4f5",
        "sel_bg": ACCENT, "sel_fg": "#ffffff",
        "chat_bg": "#f6efe9", "in_bg": "#ffffff", "in_fg": "#1b1b1b",
        "out_bg": "#fbd9d3", "out_fg": "#1b1b1b", "out_meta": "#b4594b",
        "avatar_bg": "#fde3df", "avatar_fg": "#b8321f", "online": "#2ec27e",
        "chip_bg": "alpha(#000000, 0.07)", "chip_fg": "#5b5b5b",
        "pill_bg": "#fde3df", "pill_fg": "#b8321f",
        "input_bg": "#ffffff", "shadow": "alpha(#000000, 0.08)",
    },
    "dark": {
        "accent": ACCENT, "accent_hover": "#ff5a43",
        "bg": "#17212b", "fg": "#e9eef2", "muted": "#7d8b99", "border": "#0e1621", "hover": "#202b36",
        "sel_bg": "#c23a27", "sel_fg": "#ffffff",
        "chat_bg": "#0e1621", "in_bg": "#182533", "in_fg": "#e9eef2",
        "out_bg": "#7a2b20", "out_fg": "#ffffff", "out_meta": "#f3b1a6",
        "avatar_bg": "#3a2a2a", "avatar_fg": "#ffb3a7", "online": "#4cd38a",
        "chip_bg": "alpha(#ffffff, 0.08)", "chip_fg": "#b6c2cd",
        "pill_bg": "#3a2320", "pill_fg": "#ffb3a7",
        "input_bg": "#17212b", "shadow": "alpha(#000000, 0.35)",
    },
}

CSS = Template("""
/* Classes communes (fenêtre principale et boîtes de dialogue) */
.mono { font-family: monospace; }
.subtitle { font-size: smaller; color: $muted; }
.warning { color: #e5a50a; }
.online { color: $online; }
.offline { color: alpha($muted, 0.6); }
.chat-title { font-weight: bold; font-size: larger; }
.file-name { font-weight: bold; }
.file-btn { padding: 2px; min-height: 0; min-width: 0; }
.emoji-pick { font-size: 150%; padding: 2px 4px; min-height: 0; min-width: 0; }
button.suggested-action { background-image: none; background-color: $accent; color: #ffffff;
                          border-color: $accent_hover; }
button.suggested-action:hover { background-color: $accent_hover; }

/* Fenêtre principale */
window.qchat { background-color: $bg; color: $fg; }
.qchat headerbar { background-image: none; background-color: $bg; color: $fg;
                   border-bottom: 1px solid $border; box-shadow: none; }
.qchat paned > separator { background-image: none; background-color: $border; min-width: 1px; }

.qchat .sidebar, .qchat .sidebar list { background-color: $bg; }
.qchat .sidebar row { border-radius: 12px; margin: 1px 6px; padding: 0; color: $fg; }
.qchat .sidebar row:hover { background-color: $hover; }
.qchat .sidebar row:selected { background-color: $sel_bg; color: $sel_fg; }
.qchat .sidebar row:selected .subtitle,
.qchat .sidebar row:selected .time { color: alpha($sel_fg, 0.85); }
.qchat .section { color: $muted; font-size: smaller; font-weight: bold; padding: 12px 16px 4px 16px; }
.qchat .time { color: $muted; font-size: smaller; }

.qchat .avatar { background-color: $avatar_bg; color: $avatar_fg; border-radius: 999px;
                 min-width: 42px; min-height: 42px; font-weight: bold; }
.qchat .avatar.small { min-width: 36px; min-height: 36px; }
.qchat .avatar.peer { background-color: transparent; border: 1px dashed $avatar_fg; }
.qchat .sidebar row:selected .avatar { background-color: $sel_fg; color: $sel_bg; }
.qchat .presence { background-color: $online; border-radius: 999px; min-width: 9px; min-height: 9px;
                   border: 2px solid $bg; }
.qchat .badge { background-color: $accent; color: #ffffff; border-radius: 999px; padding: 1px 7px;
                font-size: smaller; font-weight: bold; }
.qchat .sidebar row:selected .badge { background-color: $sel_fg; color: $sel_bg; }

.qchat .chat-top { background-color: $bg; border-bottom: 1px solid $border; }
.qchat .chat-area, .qchat .chat-area list, .qchat .chat-area row,
.qchat .empty-page, .qchat .composer-area { background-color: $chat_bg; }
.qchat .chat-area row, .qchat .chat-area row:hover { background-color: transparent; }

.qchat .bubble { padding: 7px 12px 5px 12px; border-radius: 16px; box-shadow: 0 1px 1px $shadow; }
.qchat .bubble.in { background-color: $in_bg; color: $in_fg; border-bottom-left-radius: 4px; }
.qchat .bubble.out { background-color: $out_bg; color: $out_fg; border-bottom-right-radius: 4px; }
.qchat .meta { font-size: smaller; color: $muted; }
.qchat .bubble.out .meta { color: $out_meta; }
.qchat .sender { color: $accent; font-weight: bold; font-size: smaller; }
.qchat .day, .qchat .system { background-color: $chip_bg; color: $chip_fg; border-radius: 999px;
                              padding: 2px 10px; font-size: smaller; }
.qchat .system { font-style: italic; }

.qchat .reaction { background-image: none; background-color: $pill_bg; color: $pill_fg;
                   border: 1px solid transparent; border-radius: 999px; padding: 0 7px;
                   min-height: 0; box-shadow: none; }
.qchat .reaction:hover { border-color: $accent; }
.qchat .reaction.mine { background-color: $accent; color: #ffffff; }
.qchat .react-btn { padding: 2px; min-height: 0; min-width: 0; color: $muted; }

.qchat .composer-area { padding: 6px 16px 14px 16px; }
.qchat .composer { background-color: $input_bg; border-radius: 22px; padding: 3px 5px;
                   box-shadow: 0 1px 2px $shadow; }
.qchat .composer scrolledwindow, .qchat .composer textview,
.qchat .composer textview text { background-color: transparent; border: none; color: $fg; }
.qchat .composer .placeholder { color: $muted; }
.qchat .round { background-image: none; background-color: transparent; border: none;
                box-shadow: none; border-radius: 999px; min-width: 34px; min-height: 34px;
                padding: 0; color: $muted; }
.qchat .round:hover { background-color: $hover; }
.qchat .send-btn { background-image: none; background-color: $accent; color: #ffffff; border: none;
                   box-shadow: none; border-radius: 999px; min-width: 36px; min-height: 36px; padding: 0; }
.qchat .send-btn:hover { background-color: $accent_hover; }

.qchat .drop-hint { background-color: alpha($bg, 0.94); color: $fg; border: 2px dashed $accent;
                    border-radius: 14px; padding: 24px 36px; font-size: larger; font-weight: bold; }
.qchat .empty-title { font-weight: bold; font-size: larger; color: $fg; }
""")


def _theme_exists(name):
    dirs = [os.path.join(GLib.get_user_data_dir(), "themes"), os.path.expanduser("~/.themes")]
    dirs += [os.path.join(d, "themes") for d in GLib.get_system_data_dirs()]
    return any(os.path.isdir(os.path.join(d, name, "gtk-3.0")) for d in dirs)


class ThemeManager:
    def __init__(self, mode="system", on_change=None):
        self.mode = mode if mode in MODES else "system"
        self._on_change = on_change or (lambda: None)
        self._settings = Gtk.Settings.get_default()
        self._provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self._provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._portal = None
        self._gsettings = None
        self.system_dark = self._read_system_dark()
        self.apply()

    @property
    def dark(self):
        return self.system_dark if self.mode == "system" else self.mode == "dark"

    @property
    def palette(self):
        return PALETTES["dark" if self.dark else "light"]

    def set_mode(self, mode):
        if mode in MODES and mode != self.mode:
            self.mode = mode
            self.apply()

    def apply(self):
        self._provider.load_from_data(CSS.substitute(self.palette).encode())
        self._apply_gtk_theme()
        self._on_change()

    def _apply_gtk_theme(self):
        """Adapte le thème GTK de base (boîtes de dialogue, menus) au mode choisi."""
        s = self._settings
        s.reset_property("gtk-theme-name")
        s.reset_property("gtk-application-prefer-dark-theme")
        if self.mode == "system":
            return
        name = s.props.gtk_theme_name or "Adwaita"
        stem = name[:-5] if name.lower().endswith("-dark") else name
        if self.dark and _theme_exists(stem + "-dark"):
            target = stem + "-dark"
        elif not self.dark and name != stem and (_theme_exists(stem) or stem == "Adwaita"):
            target = stem
        else:
            target = name
        if target != name:
            s.props.gtk_theme_name = target
        s.props.gtk_application_prefer_dark_theme = self.dark

    # -- préférence du système --------------------------------------------------
    def _read_system_dark(self):
        try:
            self._portal = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Settings", None)
            args = GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme"))
            try:
                value = self._portal.call_sync("ReadOne", args, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                value = self._portal.call_sync("Read", args, Gio.DBusCallFlags.NONE, 2000, None)
            self._portal.connect("g-signal", self._on_portal_signal)
            return self._scheme_is_dark(value.unpack()[0])
        except GLib.Error as e:
            log.info("Portail d'apparence indisponible (%s), repli sur les réglages GNOME", e.message)
            self._portal = None
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("org.gnome.desktop.interface", True) if source else None
        if schema and schema.has_key("color-scheme"):
            self._gsettings = Gio.Settings.new("org.gnome.desktop.interface")
            self._gsettings.connect("changed::color-scheme", self._on_gsettings_changed)
            return self._gsettings.get_string("color-scheme") == "prefer-dark"
        return (self._settings.props.gtk_theme_name or "").lower().endswith("-dark")

    @staticmethod
    def _scheme_is_dark(value):
        while isinstance(value, (tuple, list)) and value:   # « Read » renvoie une valeur imbriquée
            value = value[0]
        return value == 1   # 0 : sans préférence, 1 : sombre, 2 : clair

    def _system_changed(self, dark):
        if dark != self.system_dark:
            self.system_dark = dark
            if self.mode == "system":
                self.apply()

    def _on_portal_signal(self, _proxy, _sender, signal, params):
        if signal == "SettingChanged":
            namespace, key, value = params.unpack()
            if namespace == "org.freedesktop.appearance" and key == "color-scheme":
                self._system_changed(self._scheme_is_dark(value))

    def _on_gsettings_changed(self, settings, key):
        self._system_changed(settings.get_string(key) == "prefer-dark")
