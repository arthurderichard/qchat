"""Notifications de bureau via D-Bus (org.freedesktop.Notifications).

Fonctionne avec GNOME, KDE Plasma, XFCE, Cinnamon, MATE, etc. Un clic sur la
notification appelle ``on_activate(conv)``. Repli sur ``notify-send`` si le
service D-Bus n'est pas joignable.

Les notifications restent dans la liste du bureau tant qu'on ne les retire pas, et le dock
d'Ubuntu en affiche le nombre en pastille sur l'icône : QChat retire donc celles des conversations
lues, et retient leurs identifiants d'un lancement à l'autre (redémarrage après mise à jour).
Les simples informations (mise à jour installée…) sont « transitoires » : jamais conservées.
"""

import json
import logging
import shutil
import subprocess

from gi.repository import Gio, GLib

log = logging.getLogger("qchat")

APP_NAME = "QChat"
ICON = "qchat"
DESKTOP_ENTRY = "fr.qchat.QChat"


class Notifier:
    STATE_KEY = "notifications"

    def __init__(self, on_activate, store=None):
        self._on_activate = on_activate
        self._store = store   # pour retenir les identifiants d'un lancement à l'autre
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

    def notify(self, title, body, conv, transient=False):
        """``transient`` : simple information, affichée puis oubliée (pas gardée dans la liste)."""
        conv = conv or ""       # "" : notification sans conversation
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
        if transient:
            hints["transient"] = GLib.Variant("b", True)
        params = GLib.Variant("(susssasa{sv}i)", (
            APP_NAME, self._by_conv.get(conv, 0), ICON, title, body,
            ["default", "Ouvrir"], hints, -1))
        self._proxy.call("Notify", params, Gio.DBusCallFlags.NONE, -1, None, self._on_sent, conv)

    def withdraw(self, conv):
        nid = self._by_conv.pop(conv, None)
        if nid:
            self._by_id.pop(nid, None)
            self._close(nid)
            self._save()

    def withdraw_read(self, has_unread):
        """Retire toutes les notifications dont la conversation n'a plus rien de non lu
        (``has_unread(conv)``), et celles qui ne concernent aucune conversation."""
        for conv in [c for c in self._by_conv if not c or not has_unread(c)]:
            self.withdraw(conv)

    def restore(self, has_unread):
        """Au lancement : reprend les notifications du lancement précédent ; celles des
        conversations déjà lues sont retirées, les autres pourront l'être plus tard."""
        if self._store is None:
            return
        try:
            saved = json.loads(self._store.get_setting(self.STATE_KEY, "{}"))
        except ValueError:
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        for conv, nid in saved.items():
            if not isinstance(nid, int) or nid <= 0:
                continue
            if conv and has_unread(conv):
                self._by_conv[conv] = nid
                self._by_id[nid] = conv
            else:
                self._close(nid)
        self._save()

    def _close(self, nid):
        if self._proxy:
            self._proxy.call("CloseNotification", GLib.Variant("(u)", (nid,)),
                             Gio.DBusCallFlags.NONE, -1, None, None)

    def _save(self):
        if self._store is not None:
            state = {c or "": n for c, n in self._by_conv.items()}
            self._store.set_setting(self.STATE_KEY, json.dumps(state))

    def _on_sent(self, proxy, result, conv):
        try:
            (nid,) = proxy.call_finish(result).unpack()
        except GLib.Error as e:
            log.warning("Notification refusée : %s", e.message)
            return
        self._by_conv[conv] = nid
        self._by_id[nid] = conv
        self._save()

    def _on_signal(self, proxy, sender, signal, params):
        if signal == "ActionInvoked":
            nid, _action = params.unpack()
            conv = self._by_id.get(nid)
            if conv:
                self._on_activate(conv)
        elif signal == "NotificationClosed":
            nid, _reason = params.unpack()
            conv = self._by_id.pop(nid, None)
            if self._by_conv.get(conv) == nid:
                del self._by_conv[conv]
                self._save()

    @staticmethod
    def _notify_send(title, body):
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", ICON, title, body])
