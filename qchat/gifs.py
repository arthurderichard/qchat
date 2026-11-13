"""Recherche et téléchargement de GIF via l'API KLIPY (https://klipy.com).

La clé d'API de QChat est intégrée : les utilisateurs n'ont rien à configurer, comme sur Teams ou
Slack. Elle peut être remplacée par la variable d'environnement ``QCHAT_KLIPY_KEY``.
"""

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# Clé d'API de QChat, brouillée pour ne pas apparaître en clair dans le code ni dans le paquet
# (simple offuscation : une application cliente ne peut pas réellement cacher sa clé).
_K = (
    'B&juX^(@8PA<xbv@x?A_Ct<#9*u'
    'SBTgESD@zalnt5ztRXq!{A9H1EP'
    '#>fvn{du5t<p}Nz-gG+m}nIl?R'
)


def _reveal(parts=_K, salt=b"qchat/gifs:v1"):
    pad = hashlib.sha256(salt).digest() * 4
    return bytes(a ^ b for a, b in zip(base64.b85decode("".join(parts)), pad))[::-1].decode()


KLIPY_KEY = None     # surcharge possible (tests) ; sinon la clé intégrée est utilisée
API = os.environ.get("QCHAT_KLIPY_API", "https://api.klipy.com/api/v1")   # surchargé par les tests
PAGE = 24
MAX_PREVIEW = 4 * 1024 * 1024
MAX_GIF = 8 * 1024 * 1024
TIMEOUT = 15
CONTENT_FILTER = "high"          # messagerie professionnelle : filtrage le plus strict
_HEADERS = {"User-Agent": "QChat", "Accept": "application/json"}


class GifError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def api_key():
    return (os.environ.get("QCHAT_KLIPY_KEY") or KLIPY_KEY or _reveal()).strip()


def _allowed(url):
    """N'accepte que les médias servis par KLIPY (ou par l'API de test configurée)."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    api = urllib.parse.urlsplit(API)
    if u.scheme == api.scheme and u.netloc == api.netloc:
        return True
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "klipy.com" or host.endswith(".klipy.com"))


def _get(url, limit):
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GifError("Le service de GIF a refusé la clé de QChat : mettez QChat à jour.") from e
        if e.code == 429:
            raise GifError("Trop de recherches de GIF en ce moment, réessayez dans une minute.") from e
        raise GifError(f"Le service de GIF a répondu par une erreur ({e.code}).") from e
    except (urllib.error.URLError, OSError) as e:
        raise GifError("Service de GIF injoignable : ce poste a-t-il accès à Internet ?") from e
    if len(data) > limit:
        raise GifError("Fichier trop volumineux.")
    return data


def _media(files, size, fmt):
    entry = files.get(size) if isinstance(files, dict) else None
    entry = entry.get(fmt) if isinstance(entry, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("url"), str) and _allowed(entry["url"]):
        return entry
    return None


def _num(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def search(query="", page=1, customer_id="qchat", locale="fr"):
    """Une page de résultats (tendances si ``query`` est vide) : (liste, il_y_a_une_suite).
    Chaque résultat : {id, title, still, preview, url, width, height}."""
    params = {"page": page, "per_page": PAGE, "customer_id": customer_id, "locale": locale,
              "content_filter": CONTENT_FILTER}
    if query.strip():
        params["q"] = query.strip()[:50]
        endpoint = "gifs/search"
    else:
        endpoint = "gifs/trending"
    key = urllib.parse.quote(api_key(), safe="")
    raw = _get(f"{API}/{key}/{endpoint}?{urllib.parse.urlencode(params)}", 4 * 1024 * 1024)
    try:
        body = json.loads(raw).get("data") or {}
        items = body.get("data") or []
        has_next = bool(body.get("has_next"))
    except (ValueError, AttributeError) as e:
        raise GifError("Réponse du service de GIF illisible.") from e
    results = []
    for it in items:
        if not isinstance(it, dict) or it.get("type", "gif") != "gif":
            continue            # publicités et autres contenus : ignorés
        files = it.get("file")
        still = _media(files, "sm", "jpg") or _media(files, "xs", "jpg")
        preview = _media(files, "sm", "gif") or _media(files, "xs", "gif")
        # Envoi : taille moyenne si raisonnable, sinon la petite.
        full = next((m for m in (_media(files, "md", "gif"), _media(files, "sm", "gif"),
                                 _media(files, "xs", "gif"))
                     if m and _num(m.get("size"), MAX_GIF) <= MAX_GIF), None)
        if not (still or preview) or not full:
            continue
        ref = still or preview
        results.append({"id": str(it.get("id", "")), "title": str(it.get("title") or "GIF"),
                        "still": still["url"] if still else None,
                        "preview": preview["url"] if preview else None, "url": full["url"],
                        "width": _num(ref.get("width"), 100) or 100,
                        "height": _num(ref.get("height"), 100) or 100})
    return results, has_next


def download(url, limit=MAX_GIF, kind="gif"):
    """Télécharge un média KLIPY ; ``kind`` 'gif' vérifie qu'il s'agit bien d'un GIF."""
    if not _allowed(url):
        raise GifError("Adresse de GIF inattendue.")
    data = _get(url, limit)
    if kind == "gif" and not data.startswith((b"GIF87a", b"GIF89a")):
        raise GifError("Le fichier reçu n'est pas un GIF.")
    return data


def filename(title):
    """« Happy Dance GIF by Studio » → « happy-dance.gif »."""
    title = re.sub(r"\s+GIF(\s+by\s+.*)?$", "", title or "", flags=re.I)
    slug = re.sub(r"[^\w-]+", "-", title.lower()).strip("-")[:40].strip("-")
    return f"{slug or 'gif'}.gif"
