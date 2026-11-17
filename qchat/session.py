"""Démarrage automatique de QChat à l'ouverture de session.

Deux mécanismes complémentaires, installés par le paquet :

* le **service utilisateur systemd** ``qchat.service`` (activé pour tous les utilisateurs), lancé avec
  la session graphique (``graphical-session.target``) et relancé s'il s'arrête anormalement ;
* l'**entrée XDG autostart** ``/etc/xdg/autostart/fr.qchat.QChat.desktop``, en secours pour les
  bureaux dont la session n'active pas ``graphical-session.target``.

L'entrée XDG s'efface quand le service prend le relais, et l'utilisateur qui a désactivé QChat dans
ses « Applications au démarrage » est respecté par les deux.
"""

import os
import subprocess

from gi.repository import GLib

SERVICE = "qchat.service"
DESKTOP_ID = "fr.qchat.QChat.desktop"
RESTART_EXIT_CODE = 75          # demande à systemd de relancer QChat (RestartForceExitStatus)


def system_account(uid=None, login_defs="/etc/login.defs"):
    """Compte système (écran de connexion gdm, démons…) : QChat n'a rien à y faire."""
    uid = os.getuid() if uid is None else uid
    uid_min = 1000
    try:
        with open(login_defs, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "UID_MIN" and parts[1].isdigit():
                    uid_min = int(parts[1])
    except OSError:
        pass
    return uid < uid_min


def under_service():
    """QChat tourne-t-il comme service utilisateur systemd ?"""
    return os.environ.get("QCHAT_SERVICE") == "1"


def _systemctl_user(*args):
    try:
        out = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def service_handles_session():
    """Le service systemd est activé et la session graphique l'a (ou va l') lancer."""
    if _systemctl_user("is-enabled", SERVICE) not in ("enabled", "enabled-runtime", "static", "linked"):
        return False
    return _systemctl_user("is-active", "graphical-session.target") == "active"


def autostart_disabled_by_user():
    """L'utilisateur a désactivé QChat dans ses applications au démarrage (copie personnelle
    de l'entrée XDG marquée ``Hidden=true`` ou ``X-GNOME-Autostart-enabled=false``)."""
    path = os.path.join(GLib.get_user_config_dir(), "autostart", DESKTOP_ID)
    if not os.path.isfile(path):
        return False
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return False

    def flag(key, default):
        try:
            return kf.get_boolean("Desktop Entry", key)
        except GLib.Error:
            return default

    return flag("Hidden", False) or not flag("X-GNOME-Autostart-enabled", True)
