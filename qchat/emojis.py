"""Liste complète des emoji, avec noms et mots-clés en français, pour le sélecteur d'emoji.

Les données viennent de GTK lui-même : la liste anglaise est intégrée à la bibliothèque, et les
traductions sont livrées par ``libgtk-3-common`` (/usr/share/gtk-3.0/emoji/<langue>.gresource).
"""

import locale
import os
import re
import unicodedata

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402,F401  (charge les ressources de GTK)

GTK_EMOJI_DIR = "/usr/share/gtk-3.0/emoji"
_TYPE = "a(ausasu)"

# Groupes Unicode, dans l'ordre des données de GTK (le groupe 2, « composants », est masqué).
GROUPS = [
    (0, "😀", "Smileys et émotions"),
    (1, "👋", "Personnes et corps"),
    (3, "🐻", "Animaux et nature"),
    (4, "🍔", "Nourriture et boissons"),
    (5, "✈️", "Voyages et lieux"),
    (6, "⚽", "Activités"),
    (7, "💡", "Objets"),
    (8, "❤️", "Symboles"),
    (9, "🏳️", "Drapeaux"),
]


class Emoji:
    __slots__ = ("char", "name", "keywords", "group", "_haystack", "_folded")

    def __init__(self, char, name, keywords, group):
        self.char, self.name, self.keywords, self.group = char, name, keywords, group
        self._haystack = fold(" ".join([name, *keywords]))
        self._folded = None

    def rank(self, query):
        """Pertinence pour l'auto-complétion (0 = la meilleure), None si ``query`` ne correspond pas."""
        if self._folded is None:
            self._folded = (fold(self.name), [fold(k) for k in self.keywords])
        name, keywords = self._folded
        if name == query:
            return 0
        if query in keywords:
            return 1
        if name.startswith(query):
            return 2
        if any(w.startswith(query) for w in name.split()):
            return 3
        if any(w.startswith(query) for k in keywords for w in k.split()):
            return 4
        if query in self._haystack:
            return 5
        return None

    def matches(self, words):
        return all(w in self._haystack for w in words)


def fold(text):
    """Minuscules sans accents, pour une recherche tolérante (« cafe » trouve « café »)."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("’", "'").replace("œ", "oe").replace("æ", "ae")


def _languages():
    langs = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in os.environ.get(var, "").split(":"):
            code = part.split(".")[0].split("@")[0]
            if code and code not in ("C", "POSIX"):
                langs += [code, code.split("_")[0]]
    try:
        code = locale.getlocale()[0]
    except ValueError:
        code = None
    if code:
        langs += [code, code.split("_")[0]]
    return langs + ["fr"]


def _load_raw():
    for lang in _languages():
        path = os.path.join(GTK_EMOJI_DIR, f"{lang}.gresource")
        if os.path.isfile(path):
            try:
                res = Gio.Resource.load(path)
                return res.lookup_data(f"/org/gtk/libgtk/emoji/{lang}.data", 0)
            except GLib.Error:
                continue
    return Gio.resources_lookup_data("/org/gtk/libgtk/emoji/en.data", 0)


_cache = None


def load():
    """Liste des emoji (sans les variantes de couleur de peau), dans l'ordre Unicode."""
    global _cache
    if _cache is None:
        data = GLib.Variant.new_from_bytes(GLib.VariantType.new(_TYPE), _load_raw(), True).unpack()
        out = []
        for codepoints, name, keywords, group in data:
            if group == 2:
                continue
            # 0 marque l'emplacement d'un modificateur de couleur de peau : on garde la version jaune.
            char = "".join(chr(c) for c in codepoints if c)
            if len(char) == 1 and ord(char) < 0x1F000:
                char += "\ufe0f"      # présentation emoji (couleur) et non texte : ☺ → ☺️
            if char:
                out.append(Emoji(char, name, keywords, group))
        _cache = out
    return _cache


def search(query, emojis=None):
    words = fold(query).split()
    return [e for e in (emojis or load()) if e.matches(words)]


# -- raccourcis tapés dans la saisie --------------------------------------------------
_SHORTCODE = r"[^\W_]?[\w'’-]+"
_QUERY_RE = re.compile(r"(?:^|(?<=\s)):(" + _SHORTCODE + r")$")
_CLOSED_RE = re.compile(r"(?:^|(?<=\s)):(" + _SHORTCODE + r"):$")
COMPLETE_MIN = 2            # lettres à taper après « : » avant de proposer des emoji


# Pour un mot courant, l'emoji auquel tout le monde pense, avant les autres correspondances.
FAVOURITES = {
    "coeur": "❤️", "amour": "❤️", "love": "❤️", "pouce": "👍", "oui": "👍", "top": "👍", "ok": "👌",
    "non": "👎", "nul": "👎", "feu": "🔥", "fete": "🥳", "bravo": "👏", "merci": "🙏",
    "salut": "👋", "coucou": "👋", "bonjour": "👋", "rire": "😂", "mdr": "😂", "lol": "😂",
    "sourire": "🙂", "content": "😄", "triste": "😢", "pleure": "😢", "colere": "😠",
    "bisou": "😘", "cool": "😎", "clin": "😉", "surpris": "😮", "choque": "😮", "langue": "😛",
    "reflechir": "🤔", "hmm": "🤔", "peur": "😱", "fatigue": "😴", "dodo": "😴", "malade": "🤒",
    "cafe": "☕️", "biere": "🍺", "pizza": "🍕", "gateau": "🎂", "anniversaire": "🎂",
    "cadeau": "🎁", "soleil": "☀️", "pluie": "🌧️", "check": "✅", "valide": "✅", "croix": "❌",
    "attention": "⚠️", "fusee": "🚀", "cent": "💯", "applaudir": "👏", "muscle": "💪",
    "force": "💪", "prier": "🙏", "yeux": "👀", "regarde": "👀", "ange": "😇", "diable": "😈",
    "caca": "💩", "fantome": "👻", "mort": "💀", "etoile": "⭐", "idee": "💡",
}


def complete(query, recent=(), limit=6, accept=None):
    """Emoji proposés pour « :query » : nom exact, mot-clé exact, début du nom, d'un mot du nom,
    d'un mot-clé, puis le reste ; les emoji récents d'abord à pertinence égale."""
    q = fold(query).replace("_", " ").replace("-", " ").strip()
    if len(q) < COMPLETE_MIN:
        return []
    words = q.split()
    # mots favoris commençant par la requête : le plus court (le plus proche) en tête
    hits = sorted((k for k in FAVOURITES if k.startswith(q)), key=len)
    favourites = {}
    for k in hits:
        favourites.setdefault(FAVOURITES[k].replace("\ufe0f", ""), -len(favourites) - 1)
    favourites = {c: len(favourites) + v for c, v in favourites.items()}
    recent_pos = {c: i for i, c in enumerate(recent)}
    scored = []
    for i, e in enumerate(load()):
        r = e.rank(q)
        if r is None and len(words) > 1 and e.matches(words):
            r = 6                       # « pouce_haut » : tous les mots, dans le désordre
        favourite = favourites.get(e.char.replace("\ufe0f", ""))
        if favourite is not None:
            r = -1 - favourite          # l'emoji « évident » pour ce mot passe devant
        if r is not None and (accept is None or accept(e.char)):
            # à pertinence égale : récents d'abord, puis l'ordre Unicode
            scored.append((r, recent_pos.get(e.char, len(recent_pos)), i, e))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:limit]]


def completion_query(before):
    """(début, requête) si le texte avant le curseur finit par « :mot » (après une espace ou en
    début de ligne), sinon None."""
    m = _QUERY_RE.search(before)
    if not m or len(m.group(1)) < COMPLETE_MIN:
        return None
    return m.start(), m.group(1)


def closed_shortcode(before):
    """(début, requête) si le texte avant le curseur finit par « :mot: », sinon None."""
    m = _CLOSED_RE.search(before)
    return (m.start(), m.group(1)) if m and len(m.group(1)) >= COMPLETE_MIN else None


# Smileys « à l'ancienne » convertis à la frappe d'une espace ou à l'envoi.
SMILEYS = {
    ":)": "🙂", ":-)": "🙂", "(:": "🙂",
    ":D": "😄", ":-D": "😄", "xD": "😆", "XD": "😆",
    ";)": "😉", ";-)": "😉",
    ":(": "😢", ":-(": "😢", ":'(": "😭",
    ":p": "😛", ":P": "😛", ":-p": "😛", ":-P": "😛",
    ":o": "😮", ":O": "😮", ":-o": "😮", ":-O": "😮",
    ":*": "😘", ":-*": "😘",
    ":|": "😐", ":-|": "😐", ":/": "😕", ":-/": "😕",
    "B)": "😎", "B-)": "😎", "^^": "😊", ">:(": "😠",
    "<3": "❤️", "</3": "💔", "(y)": "👍", "(Y)": "👍", "(n)": "👎", "(N)": "👎",
}


def smiley_before(before):
    """(début, emoji) si le texte avant le curseur finit par un smiley isolé (après une espace ou en
    début de texte), sinon None."""
    start = max(before.rfind(" "), before.rfind("\n"), before.rfind("\t")) + 1
    emoji = SMILEYS.get(before[start:])
    return (start, emoji) if emoji else None


# -- messages composés uniquement d'emoji ----------------------------------------------
_JOINERS = {0x200D, 0xFE0F, 0xFE0E, 0x20E3}
_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2190, 0x21FF), (0x2300, 0x23FF), (0x2460, 0x24FF),
                 (0x25A0, 0x27BF), (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x3030, 0x3030),
                 (0x303D, 0x303D), (0x3297, 0x3299))
_SINGLES = {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139}


def _is_emoji_base(cp):
    return cp in _SINGLES or any(a <= cp <= b for a, b in _EMOJI_RANGES)


def emoji_only(text, limit=30):
    """Nombre d'emoji si ``text`` n'est fait que d'emoji (et d'espaces), sinon 0."""
    text = text.strip()
    if not text or len(text) > 200:
        return 0
    count = 0
    joined = False          # caractère précédent : liaison (ZWJ) → l'emoji suivant ne compte pas
    pending_flag = False    # indicateurs régionaux : deux lettres forment un drapeau
    for ch in text:
        cp = ord(ch)
        if ch.isspace():
            joined = pending_flag = False
            continue
        if cp in _JOINERS:
            joined = cp == 0x200D
            continue
        if 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F:   # couleur de peau, étiquettes
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:
            if pending_flag:
                pending_flag = False
                continue
            pending_flag = True
        elif not _is_emoji_base(cp):
            return 0
        if not joined:
            count += 1
        joined = False
    return count if 0 < count <= limit else 0
