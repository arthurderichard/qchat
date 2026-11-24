"""Mise en forme légère des messages, façon messageries courantes :

    *gras* ou **gras**   _italique_   __souligné__   ~barré~ ou ~~barré~~   `code`   ```bloc de code```
    [texte affiché](https://adresse)

Les liens (http://, https://, www.) deviennent cliquables. Le texte envoyé garde ces marques : une
ancienne version de QChat les affiche simplement telles quelles.

Module sans GTK : ``markup`` produit du balisage Pango, ``plain`` le texte sans les marques (aperçus,
notifications), ``spans`` les zones à mettre en forme ; ``serialize`` fait l'inverse de ``parse``
pour la zone de saisie, qui affiche le texte déjà mis en forme.
"""

import re
from html import escape

BOLD, ITALIC, UNDERLINE, STRIKE, CODE, MENTION = "b", "i", "u", "s", "code", "mention"

_BLOCK = re.compile(r"```\n?(.+?)\n?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"(?<![\w@/.])(?:https?://|www\.)[^\s<>\"]+", re.IGNORECASE)
_LABELED = re.compile(r"\[([^\[\]\n]+)\]\(((?:https?://|www\.)[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)\)",
                      re.IGNORECASE)
_TRAILING = ".,;:!?'»…*_~"
# (marque, style) : les marques doubles d'abord, pour que **x** ne soit pas lu comme *(*x*)*.
_MARKS = (("**", BOLD), ("__", UNDERLINE), ("~~", STRIKE), ("*", BOLD), ("_", ITALIC), ("~", STRIKE))


def _mark_pattern(mark):
    m = re.escape(mark)
    c = re.escape(mark[0])
    # Ouvrante en début de mot, suivie d'un caractère visible ; fermante juste après un caractère
    # visible et pas collée à une lettre : « snake_case » ou « 2*3*4 » restent intacts.
    return re.compile(rf"(?<![\w{c}]){m}(?=[^\s{c}])([^\n]*?[^\s{c}]|[^\s{c}]){m}(?![\w{c}])")


_MARK_PATTERNS = [(mark, style, _mark_pattern(mark)) for mark, style in _MARKS]
# Souligné : « __ » peut toucher le « _ » de l'italique (« ___mot___ » : souligné et italique).
_MARK_PATTERNS[1] = ("__", UNDERLINE, re.compile(r"(?<![\w_])__(?=\S)([^\n]*?\S)__(?!\w)"))


def _trim_link(url):
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    # parenthèse fermante finale gardée seulement si elle ferme une parenthèse du lien
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def href_of(url):
    """Adresse complète d'un lien (``www.x`` -> ``https://www.x``)."""
    url = url.strip()
    return url if "://" in url else "https://" + url


def is_url(text):
    """Vrai si ``text`` est une adresse web seule."""
    text = text.strip()
    m = _LINK.fullmatch(text)
    return bool(m) and _trim_link(text) == text and len(text) >= 5


def parse(text, mentions=()):
    """Analyse ``text`` : (caché, styles, liens), une entrée par caractère.
    ``caché[i]`` : marque de mise en forme ; ``styles[i]`` : ensemble de styles ; ``liens[i]`` : adresse."""
    return analyse(text, mentions)[:3]


def analyse(text, mentions=()):
    """Comme ``parse``, plus ``nommé[i]`` : vrai pour le texte d'un lien [texte](adresse)."""
    n = len(text)
    labeled = [False] * n
    hidden = [False] * n
    styles = [set() for _ in range(n)]
    links = [None] * n
    locked = [False] * n            # code et liens : pas de marques à l'intérieur
    in_code = [False] * n           # code : pas de mise en forme du tout (un lien peut être en gras)

    def free(a, b):
        return not any(locked[a:b]) and not any(hidden[a:b])

    def apply(a, b, inner_a, inner_b, style, lock=False):
        for i in range(a, inner_a):
            hidden[i] = True
        for i in range(inner_b, b):
            hidden[i] = True
        for i in range(inner_a, inner_b):
            styles[i].add(style)
            if lock:
                locked[i] = in_code[i] = True
        if lock:
            for i in range(a, b):
                locked[i] = True

    for m in _BLOCK.finditer(text):
        if free(m.start(), m.end()):
            apply(m.start(), m.end(), m.start(1), m.end(1), CODE, lock=True)
    for m in _INLINE_CODE.finditer(text):
        if free(m.start(), m.end()):
            apply(m.start(), m.end(), m.start(1), m.end(1), CODE, lock=True)
    for m in _LABELED.finditer(text):          # [texte](adresse)
        a, b, la, lb = m.start(), m.end(), m.start(1), m.end(1)
        if not (free(a, la) and free(lb, b)) or any(links[la:lb]):
            continue
        for i in list(range(a, la)) + list(range(lb, b)):
            hidden[i] = locked[i] = True
        for i in range(la, lb):
            links[i] = href_of(m.group(2))
            labeled[i] = True
    for m in _LINK.finditer(text):
        url = _trim_link(m.group(0))
        a, b = m.start(), m.start() + len(url)
        if (len(url) < 5 or not free(a, b) or any(links[a:b])
                or url.lower() in ("www.", "http://", "https://")):
            continue
        href = href_of(url)
        for i in range(a, b):
            links[i] = href
            locked[i] = True
    for a, b in mentions:
        for i in range(max(0, a), min(n, b)):
            styles[i].add(MENTION)
    for mark, style, pattern in _MARK_PATTERNS:
        # Marques déjà reconnues neutralisées : « ___mot___ » = souligné autour d'italique.
        masked = "".join("\x01" if hidden[i] else text[i] for i in range(n)) if any(hidden) else text
        pos = 0
        while True:
            m = pattern.search(masked, pos)
            if m is None:
                break
            a, b = m.start(), m.end()
            k = len(mark)
            if free(a, a + k) and free(b - k, b) and not any(in_code[a:b]):
                apply(a, b, a + k, b - k, style)
                masked = masked[:a] + "\x01" * k + masked[a + k:b - k] + "\x01" * k + masked[b:]
                pos = b
            else:
                pos = a + 1
    return hidden, styles, links, labeled


def _runs(text, mentions=()):
    """Morceaux visibles consécutifs de même apparence : [(texte, styles, lien)]."""
    hidden, styles, links = parse(text, mentions)
    runs = []
    for i, ch in enumerate(text):
        if hidden[i]:
            continue
        key = (frozenset(styles[i]), links[i])
        if runs and runs[-1][1:] == key:
            runs[-1][0].append(ch)
        else:
            runs.append(([ch], *key))
    return [("".join(chars), st, link) for chars, st, link in runs]


def has_format(text, mentions=()):
    """Vrai si le texte contient un lien, une mise en forme ou une mention à afficher."""
    _hidden, styles, links = parse(text, mentions)
    return any(styles) or any(links)


def plain(text):
    """Texte tel qu'il s'affiche, sans les marques de mise en forme."""
    return "".join(run for run, _st, _link in _runs(text))


def markup(text, mentions=(), mention_color=None, code_bg=None):
    """Balisage Pango du texte mis en forme (liens compris)."""
    out = []
    for run, st, link in _runs(text, mentions):
        piece = escape(run, quote=False)
        if CODE in st:
            bg = f" background='{code_bg}'" if code_bg else " background='#888888' bgalpha='22%'"
            piece = f"<span font_family='monospace'{bg}>{piece}</span>"
        if BOLD in st:
            piece = f"<b>{piece}</b>"
        if ITALIC in st:
            piece = f"<i>{piece}</i>"
        if UNDERLINE in st and not link:
            piece = f"<u>{piece}</u>"
        if STRIKE in st:
            piece = f"<s>{piece}</s>"
        if MENTION in st:
            color = f" foreground='{mention_color}'" if mention_color else ""
            piece = f"<span{color} weight='bold'>{piece}</span>"
        if link:
            piece = f"<a href=\"{escape(link, quote=True)}\" title=\"{escape(link, quote=True)}\">{piece}</a>"
        out.append(piece)
    return "".join(out)


def spans(text):
    """Pour la zone de saisie : [(début, fin, style)] où style est un style, « link » ou « marker »."""
    hidden, styles, links = parse(text)
    out = []
    for i in range(len(text)):
        kinds = set(styles[i])
        if links[i]:
            kinds.add("link")
        if hidden[i]:
            kinds = {"marker"}
        for k in kinds:
            if out and out[-1][2] == k and out[-1][1] == i:
                out[-1] = (out[-1][0], i + 1, k)
            else:
                # fusion avec un morceau du même style terminé juste avant (styles qui se chevauchent)
                for j in range(len(out) - 1, -1, -1):
                    if out[j][2] == k and out[j][1] == i:
                        out[j] = (out[j][0], i + 1, k)
                        break
                else:
                    out.append((i, i + 1, k))
    return out


_ORDER = {"link": 0, CODE: 1, BOLD: 2, UNDERLINE: 3, ITALIC: 4, STRIKE: 5}
_OPEN = {CODE: "`", BOLD: "*", UNDERLINE: "__", ITALIC: "_", STRIKE: "~"}


def serialize(chars):
    """Texte à envoyer depuis une suite de (caractère, styles, adresse du lien ou None) : les marques
    sont ajoutées autour de chaque zone mise en forme. ``plain(serialize(x))`` redonne le texte et
    ``markup`` la même mise en forme."""
    text = "".join(c for c, _st, _url in chars)
    n = len(text)
    spans = []                          # (début, fin, clé, adresse)

    def runs(test):
        start = None
        for i in range(n + 1):
            on = i < n and test(i)
            if on and start is None:
                start = i
            elif not on and start is not None:
                yield start, i
                start = None

    for a, b in runs(lambda i: chars[i][2] is not None):
        # un lien par adresse, même collé à un autre
        i = a
        while i < b:
            url = chars[i][2]
            j = i
            while j < b and chars[j][2] == url:
                j += 1
            spans.append((i, j, "link", url))
            i = j
    for a, b in runs(lambda i: CODE in chars[i][1]):
        spans.append((a, b, CODE, None))
    for style in (BOLD, UNDERLINE, ITALIC, STRIKE):
        for a, b in runs(lambda i, st=style: st in chars[i][1] and CODE not in chars[i][1]
                         and text[i] != "\n"):
            while a < b and text[a].isspace():          # espaces laissés hors des marques
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
            if a < b:
                spans.append((a, b, style, None))

    opens, closes = {}, {}
    for a, b, key, url in spans:
        if key == "link":
            label = text[a:b]
            styled = any(chars[i][1] for i in range(a, b))
            if not styled and href_of(label) == url and is_url(label):
                continue                # adresse affichée telle quelle : reconnue à l'arrivée
            o, c = "[", f"]({url})"
        elif key == CODE:
            o = c = "```" if "\n" in text[a:b] or "`" in text[a:b] else "`"
            if o == "```" and "\n" in text[a:b]:
                o, c = "```\n", "\n```"
        else:
            o = c = _OPEN[key]
        opens.setdefault(a, []).append((_ORDER[key], -b, o))
        closes.setdefault(b, []).append((_ORDER[key], -a, c))
    out = []
    for i in range(n + 1):
        for _k, _a, c in sorted(closes.get(i, ()), reverse=True):     # intérieures d'abord
            out.append(c)
        for _k, _b, o in sorted(opens.get(i, ())):                    # extérieures d'abord
            out.append(o)
        if i < n:
            out.append(text[i])
    return "".join(out)
