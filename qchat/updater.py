"""Mises à jour automatiques : détection d'une nouvelle version installée et redémarrage.

L'installation elle-même est faite par le service système ``qchat-update`` (minuteur systemd
installé par le paquet .deb), qui interroge régulièrement le dépôt apt signé de QChat. Une fois
le paquet remplacé sur le disque, l'application en cours le remarque ici et se relance d'elle-même.
"""

import logging
import os
import re
import subprocess
import sys

from . import __version__

log = logging.getLogger("qchat")

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PREFIXES = ("/usr/", "/opt/")
_VERSION = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


def parse_version(text):
    """« 1.10.2 » → (1, 10, 2) ; les parties non numériques valent 0."""
    parts = []
    for p in str(text).strip().split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def is_newer(candidate, current=__version__):
    return parse_version(candidate) > parse_version(current)


def installed_version(pkg_dir=PKG_DIR):
    """Version actuellement présente sur le disque (peut différer de celle qui tourne)."""
    try:
        with open(os.path.join(pkg_dir, "__init__.py"), encoding="utf-8") as f:
            m = _VERSION.search(f.read())
    except OSError:
        return None
    return m.group(1) if m else None


def auto_restart_enabled(pkg_dir=PKG_DIR):
    """Seulement pour une installation système (paquet, /opt) : pas en développement."""
    forced = os.environ.get("QCHAT_AUTO_RESTART")
    if forced is not None:
        return forced == "1"
    return pkg_dir.startswith(SYSTEM_PREFIXES)


def pending_update(pkg_dir=PKG_DIR):
    """Version plus récente installée sur le disque depuis le lancement, ou None."""
    version = installed_version(pkg_dir)
    return version if version and is_newer(version) else None


def relaunch(args):
    """Lance une nouvelle instance de QChat dès que celle-ci se sera arrêtée.

    L'application est unique par session (D-Bus) : la nouvelle instance doit attendre la fin de
    l'ancienne, sinon elle se contenterait de lui passer la main. Un petit shell détaché attend
    donc la disparition de ce processus avant de relancer QChat avec ``args``.
    """
    cmd = [sys.executable, "-m", "qchat", *args]
    script = ('while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; exec "$@"')
    subprocess.Popen(["/bin/sh", "-c", script, str(os.getpid()), *cmd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    log.info("Redémarrage programmé : %s", " ".join(cmd))


# -- recherche et installation à la demande (menu ☰ › Rechercher des mises à jour) -------------
REPO_PACKAGES = os.environ.get("QCHAT_REPO_URL", "https://arthurderichard.github.io/qchat/Packages")
UPDATE_UNIT = "qchat-update.service"
UPDATE_SCRIPT = "/usr/lib/qchat/qchat-update"


class UpdateError(Exception):
    """Erreur affichable telle quelle."""


def latest_available(url=REPO_PACKAGES, timeout=10):
    """Version la plus récente de QChat publiée dans le dépôt apt."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"QChat/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("Impossible de joindre le dépôt de QChat : ce poste a-t-il accès à Internet ?") from e
    best = None
    for stanza in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines() if ": " in line)
        if fields.get("Package") == "qchat" and fields.get("Version"):
            if best is None or parse_version(fields["Version"]) > parse_version(best):
                best = fields["Version"]
    if best is None:
        raise UpdateError("Le dépôt de QChat ne contient aucune version lisible.")
    return best


def can_install():
    """QChat a été installé par le paquet .deb (seul cas où il sait se mettre à jour lui-même)."""
    return os.path.isfile(UPDATE_SCRIPT) and PKG_DIR.startswith("/usr/share/qchat")


def start_install():
    """Demande à systemd de lancer qchat-update.service (autorisé sans mot de passe par la règle
    polkit du paquet ; sinon, le bureau demande un mot de passe administrateur)."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        bus.call_sync("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                      "org.freedesktop.systemd1.Manager", "StartUnit",
                      GLib.Variant("(ss)", (UPDATE_UNIT, "replace")), None,
                      Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 120000, None)
    except GLib.Error as e:
        if "AccessDenied" in e.message or "NotAuthorized" in e.message or "dismissed" in e.message.lower():
            raise UpdateError("Installation refusée : les droits d'administration n'ont pas été accordés.") from e
        raise UpdateError(f"Impossible de lancer la mise à jour : {e.message}") from e


def install_state():
    """(état systemd, résultat) de qchat-update.service, par ex. ('activating', 'success')."""
    try:
        out = subprocess.run(["systemctl", "show", UPDATE_UNIT, "-p", "ActiveState", "-p", "Result"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown", "unknown"
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return props.get("ActiveState", "unknown"), props.get("Result", "unknown")
