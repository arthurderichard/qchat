"""Encodeur GIF animé minimal, en Python pur : sert à réduire un GIF à la taille d'un emoji
personnalisé (quelques dizaines de Ko) sans perdre l'animation.

Palette commune calculée par coupe médiane, transparence binaire, images suivantes réduites aux
seuls pixels qui changent quand le GIF n'a pas de transparence.
"""

import struct

from gi.repository import GdkPixbuf

MAX_FRAMES = 60


def _k15(c):
    return ((c >> 9) & 0x7C00) | ((c >> 6) & 0x3E0) | ((c >> 3) & 0x1F)


def _median_cut(hist, count):
    """Palette (liste de (r, g, b)) et table couleur 15 bits -> indice, depuis l'histogramme
    {couleur 15 bits: [nombre, somme r, somme g, somme b]}."""
    boxes = [[(k, v[0]) for k, v in hist.items()]]
    while len(boxes) < count:
        best, best_range, best_ch = None, 0, 0
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            for ch in range(3):
                vals = [(k >> (10 - 5 * ch)) & 31 for k, _n in box]
                rng = max(vals) - min(vals)
                if rng > best_range:
                    best, best_range, best_ch = i, rng, ch
        if best is None:
            break
        box = sorted(boxes.pop(best), key=lambda kn: (kn[0] >> (10 - 5 * best_ch)) & 31)
        total, acc, cut = sum(n for _k, n in box), 0, 1
        for j, (_k, n) in enumerate(box):
            acc += n
            if acc * 2 >= total:
                cut = max(1, min(len(box) - 1, j + 1))
                break
        boxes += [box[:cut], box[cut:]]
    palette, lut = [], {}
    for idx, box in enumerate(boxes):
        total = sum(n for _k, n in box) or 1
        palette.append(tuple(min(255, round(sum(hist[k][1 + ch] for k, _n in box) / total))
                             for ch in range(3)))
        for k, _n in box:
            lut[k] = idx
    return palette, lut


def _keys(pb):
    """Couleur 24 bits de chaque pixel (None si transparent)."""
    w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
    data = pb.get_pixels()
    out = []
    for y in range(h):
        row = data[y * stride:y * stride + w * 4]
        r, g, b, a = row[0::4], row[1::4], row[2::4], row[3::4]
        out += [None if aa < 128 else (rr << 16) | (gg << 8) | bb for rr, gg, bb, aa in zip(r, g, b, a)]
    return out


def _lzw(indices, min_size):
    """Flux LZW d'une image GIF, découpé en sous-blocs."""
    clear, end = 1 << min_size, (1 << min_size) + 1
    out = bytearray()
    bits = nbits = 0
    size = min_size + 1

    def emit(code):
        nonlocal bits, nbits
        bits |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8

    table = {}
    nxt = end + 1
    emit(clear)
    prefix = None
    for px in indices:
        if prefix is None:
            prefix = px
            continue
        key = (prefix, px)
        code = table.get(key)
        if code is not None:
            prefix = code
            continue
        emit(prefix)
        if nxt < 4096:
            table[key] = nxt
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
        else:
            emit(clear)
            table.clear()
            nxt = end + 1
            size = min_size + 1
        prefix = px
    if prefix is not None:
        emit(prefix)
    emit(end)
    if nbits:
        out.append(bits & 0xFF)
    blocks = bytearray([min_size])
    for i in range(0, len(out), 255):
        chunk = out[i:i + 255]
        blocks.append(len(chunk))
        blocks += chunk
    blocks.append(0)
    return bytes(blocks)


def encode(frames):
    """GIF animé (octets) depuis une liste de (Pixbuf RGBA, délai en ms), en boucle infinie."""
    w, h = frames[0][0].get_width(), frames[0][0].get_height()
    keyed = [_keys(pb) for pb, _d in frames]
    transparent = any(None in k for k in keyed)
    counts = {}
    for keys in keyed:
        for c in keys:
            counts[c] = counts.get(c, 0) + 1
    counts.pop(None, None)
    hist = {}
    for c, n in counts.items():
        v = hist.setdefault(_k15(c), [0, 0, 0, 0])
        v[0] += n
        v[1] += (c >> 16) * n
        v[2] += ((c >> 8) & 255) * n
        v[3] += (c & 255) * n
    if not hist:
        hist[0] = [1, 0, 0, 0]
    palette, lut15 = _median_cut(hist, 255)
    lut = {c: lut15[_k15(c)] for c in counts}
    tidx = len(palette)                 # indice réservé à la transparence (et aux pixels inchangés)
    palette.append((0, 0, 0))
    depth = max(1, (len(palette) - 1).bit_length())
    palette += [(0, 0, 0)] * ((1 << depth) - len(palette))

    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", w, h, 0x80 | (depth - 1), 0, 0)
    out += b"".join(bytes(c) for c in palette)
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"
    prev = None
    for keys, (_pb, delay) in zip(keyed, frames):
        idx = [tidx if k is None else lut[k] for k in keys]
        x0, y0, fw, fh = 0, 0, w, h
        if transparent:
            disposal = 2                # chaque image est complète, sur fond transparent
        else:
            disposal = 1                # seuls les pixels qui changent sont codés
            if prev is not None:
                changed = [a != b for a, b in zip(idx, prev)]
                rows = [y for y in range(h) if any(changed[y * w:(y + 1) * w])]
                if rows:
                    cols = [x for x in range(w) if any(changed[y * w + x] for y in rows)]
                    x0, y0 = cols[0], rows[0]
                    fw, fh = cols[-1] - x0 + 1, rows[-1] - y0 + 1
                else:
                    x0, y0, fw, fh = 0, 0, 1, 1
                cur = idx
                idx = [cur[(y0 + y) * w + x0 + x] if changed[(y0 + y) * w + x0 + x] else tidx
                       for y in range(fh) for x in range(fw)]
                prev = cur
            else:
                prev = idx
        cs = max(2, min(65535, round(delay / 10)))
        out += struct.pack("<BBBBHBB", 0x21, 0xF9, 4, (disposal << 2) | 1, cs, tidx, 0)
        out += struct.pack("<BHHHHB", 0x2C, x0, y0, fw, fh, 0)
        out += _lzw(idx, max(2, depth))
    out += b"\x3B"
    return bytes(out)


def _stride(count, step):
    while count / step > MAX_FRAMES:
        step += 1
    return step


def _resample(frames, step):
    """Garde une image sur ``step`` (délais cumulés) et au plus MAX_FRAMES images."""
    step = _stride(len(frames), step)
    out = []
    for i in range(0, len(frames), step):
        group = frames[i:i + step]
        out.append((group[0][0], sum(d for _p, d in group)))
    return out


# (côté maximal, une image sur …) dans l'ordre de préférence : la fluidité compte plus que la taille,
# un emoji s'affiche au plus en 96 px.
ATTEMPTS = ((128, 1), (112, 1), (96, 1), (80, 1), (96, 2), (64, 1), (80, 2), (64, 2), (56, 2),
            (48, 2), (64, 3), (48, 3), (40, 3), (48, 4), (40, 5), (32, 6))


def shrink(frames, limit):
    """GIF animé d'au plus ``limit`` octets, réduit (taille, puis nombre d'images) ; None si impossible."""
    pw, ph = frames[0][0].get_width(), frames[0][0].get_height()
    scaled = {}
    tried = set()
    for side, step in ATTEMPTS:
        k = min(1.0, side / max(pw, ph))
        w, h = max(1, round(pw * k)), max(1, round(ph * k))
        subset = _resample(frames, step)
        if (w, h, len(subset)) in tried or (len(subset) < 2 <= len(frames)):
            continue
        tried.add((w, h, len(subset)))
        if (w, h) not in scaled:
            scaled = {(w, h): [pb if (w, h) == (pw, ph) else pb.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
                               for pb, _d in frames]}
        pbs = scaled[(w, h)]
        subset = [(pbs[i], d) for i, (_pb, d) in zip(range(0, len(frames), _stride(len(frames), step)), subset)]
        data = encode(subset)
        if len(data) <= limit:
            return data
    return None
