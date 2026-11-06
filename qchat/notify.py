"""Notifications de bureau via D-Bus (org.freedesktop.Notifications).

Fonctionne avec GNOME, KDE Plasma, XFCE, Cinnamon, MATE, etc. Un clic sur la
notification appelle ``on_activate(conv)``. Repli sur ``notify-send`` si le
service D-Bus n'est pas joignable.
"""

import logging
import shutil
import subprocess

from gi.repository import Gio, GLib

log = logging.getLogger("qchat")

APP_NAME = "QChat"
ICON = "qchat"
DESKTOP_ENTRY = "fr.qchat.QChat"


class Notifier:
    def __init__(self, on_activate):
        self._on_activate = on_activate
        self._by_conv = {}    # conv -> id de notification (remplacée au lieu d'empiler)
        self._by_id = {}      # id -> conv
        self._proxy = None
        self._has_markup = False
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", None)
            self._proxy.connect("g-signal", self._on_signal)
            caps = self._proxy.call_sync("GetCapabilities", None, Gio.DBusCallFlags.NONE, 2000, None)
            self._has_markup = "body-markup" in caps.unpack()[0]
        except GLib.Error as e:
            log.warning("Service de notifications D-Bus indisponible : %s", e.message)
            self._proxy = None

    def notify(self, title, body, conv):
        if len(body) > 200:
            body = body[:200] + "…"
        if self._proxy is None:
            self._notify_send(title, body)
            return
        if self._has_markup:
            body = GLib.markup_escape_text(body)
        hints = {
            "desktop-entry": GLib.Variant("s", DESKTOP_ENTRY),
            "category": GLib.Variant("s", "im.received"),
            "sound-name": GLib.Variant("s", "message-new-instant"),
            "urgency": GLib.Variant("y", 1),
        }
        params = GLib.Variant("(susssasa{sv}i)", (
            APP_NAME, self._by_conv.get(conv, 0), ICON, title, body,
            ["default", "Ouvrir"], hints, -1))
        self._proxy.call("Notify", params, Gio.DBusCallFlags.NONE, -1, None, self._on_sent, conv)

    def withdraw(self, conv):
        nid = self._by_conv.pop(conv, None)
        if nid and self._proxy:
            self._by_id.pop(nid, None)
            self._proxy.call("CloseNotification", GLib.Variant("(u)", (nid,)),
                             Gio.DBusCallFlags.NONE, -1, None, None)

    def _on_sent(self, proxy, result, conv):
        try:
            (nid,) = proxy.call_finish(result).unpack()
        except GLib.Error as e:
            log.warning("Notification refusée : %s", e.message)
            return
        self._by_conv[conv] = nid
        self._by_id[nid] = conv

    def _on_signal(self, proxy, sender, signal, params):
        if signal == "ActionInvoked":
            nid, _action = params.unpack()
            conv = self._by_id.get(nid)
            if conv:
                self._on_activate(conv)
        elif signal == "NotificationClosed":
            nid, _reason = params.unpack()
            conv = self._by_id.pop(nid, None)
            if conv and self._by_conv.get(conv) == nid:
                del self._by_conv[conv]

    @staticmethod
    def _notify_send(title, body):
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", ICON, title, body])
