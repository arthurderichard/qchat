"""Journal des nouveautés (CHANGELOG.md), affiché au premier lancement d'une nouvelle version.

Le fichier est à la racine du dépôt ; le paquet .deb l'installe à côté du code
(/usr/share/qchat/qchat/CHANGELOG.md).
"""

import os
import re

from . import __version__
from .updater import parse_version

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = (os.path.join(PKG_DIR, "CHANGELOG.md"), os.path.join(os.path.dirname(PKG_DIR), "CHANGELOG.md"))
_HEADING = re.compile(r"^##\s+(\d+(?:\.\d+)*)\s*(?:[—–-]\s*(\S.*))?$")


class Release:
    __slots__ = ("version", "date", "items")

    def __init__(self, version, date):
        self.version, self.date, self.items = version, date, []

    def __repr__(self):
        return f"Release({self.version!r}, {len(self.items)} entrées)"


def path():
    return next((p for p in _CANDIDATES if os.path.isfile(p)), None)


def parse(text):
    """Versions du journal, de la plus récente à la plus ancienne."""
    releases, current = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _HEADING.match(line)
        if m:
            current = Release(m.group(1), (m.group(2) or "").strip())
            releases.append(current)
        elif line.startswith("#"):
            current = None
        elif current is not None:
            stripped = line.strip()
            if line.startswith(("- ", "* ")):
                current.items.append(stripped[2:].strip())
            elif stripped and current.items and raw[:1].isspace():   # suite d'une puce sur plusieurs lignes
                current.items[-1] += " " + stripped
    return releases


def load():
    p = path()
    if p is None:
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return parse(f.read())
    except OSError:
        return []


def since(previous, current=__version__, releases=None):
    """Versions plus récentes que ``previous`` et pas plus récentes que ``current``."""
    releases = load() if releases is None else releases
    low, high = parse_version(previous or "0"), parse_version(current)
    return [r for r in releases if low < parse_version(r.version) <= high]


def to_markup(text):
    """**gras**, *italique* et `code` en balisage Pango (le reste est échappé)."""
    from gi.repository import GLib
    out, pos = [], 0
    pattern = re.compile(r"\*\*(.+?)\*\*|`(.+?)`|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
    for m in pattern.finditer(text):
        out.append(GLib.markup_escape_text(text[pos:m.start()]))
        bold, code, italic = m.groups()
        if bold is not None:
            out.append(f"<b>{GLib.markup_escape_text(bold)}</b>")
        elif code is not None:
            out.append(f"<tt>{GLib.markup_escape_text(code)}</tt>")
        else:
            out.append(f"<i>{GLib.markup_escape_text(italic)}</i>")
        pos = m.end()
    out.append(GLib.markup_escape_text(text[pos:]))
    return "".join(out)
