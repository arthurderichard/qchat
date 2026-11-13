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
