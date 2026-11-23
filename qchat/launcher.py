"""Compteur de messages non lus sur l'icône de QChat (dock, barre des tâches).

Utilise l'interface standard ``com.canonical.Unity.LauncherEntry``, reconnue par le dock d'Ubuntu
(Dash to Dock), Dash to Panel, la barre des tâches de KDE Plasma, Latte Dock, Plank, Cairo-Dock et
le dock d'elementary OS. Sans elle, ces docks affichent à la place le nombre de notifications
restées dans la liste du bureau, qui ne dit rien des messages réellement non lus.

Les environnements sans pastille (XFCE, MATE…) ignorent simplement ces signaux.
"""

import logging

from gi.repository import Gio, GLib

log = logging.getLogger("qchat")

INTERFACE = "com.canonical.Unity.LauncherEntry"
_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <signal name="Update">
      <arg type="s" name="app_uri"/>
      <arg type="a{{sv}}" name="properties"/>
    </signal>
    <method name="Query">
      <arg type="s" name="app_uri" direction="out"/>
      <arg type="a{{sv}}" name="properties" direction="out"/>
    </method>
  </interface>
</node>
"""


class LauncherBadge:
    PATH = "/fr/qchat/QChat/LauncherEntry"

    def __init__(self, desktop_id="fr.qchat.QChat.desktop", bus=None):
        self.uri = f"application://{desktop_id}"
        self.count = None
        self._bus = bus
        self._reg = 0
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            info = Gio.DBusNodeInfo.new_for_xml(_XML).interfaces[0]
            self._reg = self._bus.register_object(self.PATH, info, self._on_call, None, None)
        except GLib.Error as e:
            log.info("Compteur d'icône indisponible : %s", e.message)
            self._bus = None

    def _props(self):
        count = self.count or 0
        return {"count": GLib.Variant("x", count), "count-visible": GLib.Variant("b", count > 0)}

    def _on_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "Query":
            invocation.return_value(GLib.Variant("(sa{sv})", (self.uri, self._props())))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def set_count(self, count):
        """Affiche ``count`` sur l'icône (rien à 0) ; n'émet qu'en cas de changement."""
        count = max(0, int(count))
        if count == self.count or self._bus is None:
            self.count = count
            return
        self.count = count
        try:
            self._bus.emit_signal(None, self.PATH, INTERFACE, "Update",
                                  GLib.Variant("(sa{sv})", (self.uri, self._props())))
        except GLib.Error as e:
            log.debug("Compteur d'icône non transmis : %s", e.message)

    def close(self):
        self.set_count(0)
        if self._bus is not None and self._reg:
            self._bus.unregister_object(self._reg)
            self._reg = 0
