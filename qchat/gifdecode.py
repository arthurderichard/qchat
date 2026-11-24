"""Décodeur GIF de secours, en Python pur.

Depuis gdk-pixbuf 2.44 (Ubuntu 26.04…), les GIF sont décodés par glycin dans un bac à sable
bubblewrap. Quand ce bac à sable ne peut pas démarrer (profil AppArmor non chargé, espaces de noms
utilisateur interdits…), gdk-pixbuf échoue et QChat n'afficherait qu'une icône de fichier. Ce module
prend alors le relais : décompression LZW ici, composition des images par GdkPixbuf (en C).
"""

import logging
import os
import struct
from collections import OrderedDict

from gi.repository import GdkPixbuf, GLib

log = logging.getLogger("qchat")

MAX_PIXELS = 4096 * 4096
MAX_FRAMES = 500


class GifError(ValueError):
    pass


def is_gif(data):
    return data[:6] in (b"GIF87a", b"GIF89a")


def _palette(raw):
    return [raw[i:i + 3] + b"\xff" for i in range(0, len(raw), 3)]


def _lzw(data, min_size, count):
    """Indices de couleur (``count`` au plus) depuis le flux LZW d'une image GIF."""
    if not 2 <= min_size <= 11:
        raise GifError("taille LZW invalide")
    clear, end = 1 << min_size, (1 << min_size) + 1
    base = [bytes((i,)) for i in range(clear)] + [b"", b""]
    table = list(base)
    size, mask = min_size + 1, (1 << (min_size + 1)) - 1
    out = bytearray()
    prev = None
    bits = nbits = 0
    for byte in data:
        bits |= byte << nbits
        nbits += 8
        while nbits >= size:
            code = bits & mask
            bits >>= size
            nbits -= size
            if code == clear:
                table = list(base)
                size, mask = min_size + 1, (1 << (min_size + 1)) - 1
                prev = None
                continue
            if code == end:
                return bytes(out[:count])
            if prev is None:
                if code >= len(table):
                    raise GifError("flux LZW corrompu")
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                if len(table) < 4096:
                    table.append(prev + entry[:1])
            elif code == len(table):
                entry = prev + prev[:1]
                if len(table) < 4096:
                    table.append(entry)
            else:
                raise GifError("flux LZW corrompu")
            out += entry
            prev = entry
            if len(table) > mask and size < 12:
                size += 1
                mask = (1 << size) - 1
            if len(out) >= count:
                return bytes(out[:count])
    return bytes(out[:count])


def _blocks(data, pos):
    """Sous-blocs concaténés à partir de ``pos`` ; renvoie (octets, position suivante)."""
    parts = []
    while True:
        if pos >= len(data):
            raise GifError("fichier tronqué")
        n = data[pos]
        pos += 1
        if n == 0:
            return b"".join(parts), pos
        parts.append(data[pos:pos + n])
        pos += n


def _deinterlace(indices, w, h):
    rows = [indices[y * w:(y + 1) * w] for y in range(h)]
    order = list(range(0, h, 8)) + list(range(4, h, 8)) + list(range(2, h, 4)) + list(range(1, h, 2))
    out = [b""] * h
    for src, dst in enumerate(order):
        out[dst] = rows[src]
    return b"".join(out)


def decode(data):
    """Images complètes du GIF : liste de (Pixbuf RGBA à la taille du GIF, délai en ms)."""
    if not is_gif(data) or len(data) < 13:
        raise GifError("pas un GIF")
    width, height, flags = struct.unpack_from("<HHB", data, 6)
    if not width or not height or width * height > MAX_PIXELS:
        raise GifError("dimensions invalides")
    pos = 13
    global_pal = None
    if flags & 0x80:
        n = 3 << ((flags & 7) + 1)
        global_pal = _palette(data[pos:pos + n])
        pos += n

    canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    canvas.fill(0)
    frames = []
    delay, transparent, disposal = 100, None, 0
    while pos < len(data) and len(frames) < MAX_FRAMES:
        kind = data[pos]
        pos += 1
        if kind == 0x3B:                    # fin du fichier
            break
        if kind == 0x21:                    # extension
            label = data[pos] if pos < len(data) else 0
            body, pos = _blocks(data, pos + 1)
            if label == 0xF9 and len(body) >= 4:
                packed, d, t = struct.unpack_from("<BHB", body)
                delay = d * 10 if d > 1 else 100        # 0 ou 1 : les navigateurs affichent à 10 ips
                transparent = t if packed & 1 else None
                disposal = (packed >> 2) & 7
            continue
        if kind != 0x2C:
            raise GifError("bloc inconnu")
        if pos + 9 > len(data):
            raise GifError("fichier tronqué")
        x, y, w, h, iflags = struct.unpack_from("<HHHHB", data, pos)
        pos += 9
        pal = global_pal
        if iflags & 0x80:
            n = 3 << ((iflags & 7) + 1)
            pal = _palette(data[pos:pos + n])
            pos += n
        if pal is None:
            raise GifError("pas de palette")
        min_size = data[pos] if pos < len(data) else 0
        lzw, pos = _blocks(data, pos + 1)
        indices = _lzw(lzw, min_size, w * h) if w and h else b""
        cw, ch = min(w, width - x) if x < width else 0, min(h, height - y) if y < height else 0
        if cw and ch:
            indices = indices.ljust(w * h, b"\0")
            if iflags & 0x40:
                indices = _deinterlace(indices, w, h)
            pal = pal + [b"\0\0\0\xff"] * (256 - len(pal))
            if transparent is not None:
                pal[transparent] = b"\0\0\0\0"
            pixels = b"".join(map(pal.__getitem__, indices))
            patch = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(pixels), GdkPixbuf.Colorspace.RGB,
                                                    True, 8, w, h, w * 4)
            saved = canvas.copy() if disposal == 3 else None
            patch.composite(canvas, x, y, cw, ch, x, y, 1, 1, GdkPixbuf.InterpType.NEAREST, 255)
            frames.append((canvas.copy(), delay))
            if disposal == 2:
                canvas.new_subpixbuf(x, y, cw, ch).fill(0)
            elif saved is not None:
                canvas = saved
        delay, transparent, disposal = 100, None, 0
    if not frames:
        raise GifError("aucune image")
    return frames


def animation(data):
    """(animation, première image) ; animation None pour un GIF à une seule image."""
    frames = decode(data)
    first = frames[0][0]
    if len(frames) == 1:
        return None, first
    avg = sum(d for _p, d in frames) / len(frames)
    anim = GdkPixbuf.PixbufSimpleAnim.new(first.get_width(), first.get_height(), 1000 / max(avg, 20))
    anim.set_loop(True)
    for pb, _d in frames:
        anim.add_frame(pb)
    return anim, first


_warned = False
_cache = OrderedDict()      # quelques fichiers récents : l'aperçu puis l'animation ne décodent qu'une fois


def fallback_used(error):
    """Note (une seule fois) que gdk-pixbuf n'a pas su lire un GIF, pour le diagnostic."""
    global _warned
    if not _warned:
        _warned = True
        log.warning("gdk-pixbuf ne lit pas les GIF (%s) : décodeur intégré utilisé", error)


def from_bytes(data):
    """Comme ``animation`` ; (None, None) si ce n'est pas un GIF lisible."""
    try:
        return animation(data) if is_gif(data) else (None, None)
    except (GifError, GLib.Error, IndexError, struct.error, ValueError):
        return None, None


def load(path):
    """Comme ``from_bytes`` pour un fichier."""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        with open(path, "rb") as f:
            if not is_gif(f.read(6)):
                return None, None
            f.seek(0)
            data = f.read()
    except OSError:
        return None, None
    result = from_bytes(data)
    _cache[key] = result
    while len(_cache) > 4:
        _cache.popitem(last=False)
    return result
