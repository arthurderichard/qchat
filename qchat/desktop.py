"""Intégration au bureau (indépendante de GTK, donc testable sans interface)."""

import logging
import os
import shutil
import subprocess

log = logging.getLogger("qchat")


def user_data_dir():
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def repair_user_icon_cache(data_dir=None):
    """Répare un cache d'icônes utilisateur périmé.

    Une ancienne installation par install.sh crée ~/.local/share/icons/hicolor/icon-theme.cache.
    Si l'icône est ensuite supprimée sans régénérer ce cache, il annonce toujours « qchat » à un
    emplacement vide : GTK et GNOME s'y fient et n'affichent plus aucune icône, même celle du
    paquet installée dans /usr/share. Renvoie True si le cache a été régénéré.
    """
    base = os.path.join(data_dir or user_data_dir(), "icons", "hicolor")
    cache = os.path.join(base, "icon-theme.cache")
    try:
        if os.path.exists(os.path.join(base, "scalable", "apps", "qchat.svg")):
            return False
        with open(cache, "rb") as f:
            if b"qchat" not in f.read():
                return False
    except OSError:
        return False
    try:
        if shutil.which("gtk-update-icon-cache"):
            subprocess.run(["gtk-update-icon-cache", "-q", "-t", "-f", base], timeout=15, check=False)
        if os.path.exists(cache):
            with open(cache, "rb") as f:
                stale = b"qchat" in f.read()
            if stale:
                os.unlink(cache)
    except (OSError, subprocess.SubprocessError):
        return False
    log.info("Cache d'icônes utilisateur périmé régénéré : %s", base)
    return True
