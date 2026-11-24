"""Cache disque des miniatures (aperçus d'images, photos rondes).

Décoder une image passe par gdk-pixbuf, donc par glycin et son bac à sable (Ubuntu 26.04) : plusieurs
millisecondes par image. Une miniature déjà calculée est enregistrée en PNG et relue directement par
cairo (~0,1 ms), au prochain affichage comme au prochain lancement.
"""

import hashlib
import logging
import os
import time

import cairo

log = logging.getLogger("qchat")


class ThumbCache:
    def __init__(self, directory, run_later=None):
        self.dir = directory
        self._later = run_later     # exécute une fonction hors du fil de l'interface (écriture PNG)
        self._mem = {}          # clé -> surface (session)
        self._bad = set()       # clés d'images illisibles (pas de nouvel essai pendant la session)

    def _file(self, path, variant):
        key = self._key(path, variant)
        return key, os.path.join(self.dir, key[:2], key + ".png")

    def _write(self, surf, file):
        try:
            os.makedirs(os.path.dirname(file), mode=0o700, exist_ok=True)
            tmp = f"{file}.{os.getpid()}.{id(surf)}.part"
            surf.write_to_png(tmp)
            os.replace(tmp, file)
        except (cairo.Error, OSError) as e:
            log.debug("Miniature non enregistrée : %s", e)

    def warm(self, path, variant, make):
        """Prépare la miniature sur disque si elle n'y est pas (appelé hors du fil de l'interface)."""
        try:
            _key, file = self._file(path, variant)
        except OSError:
            return
        if not os.path.exists(file):
            surf = make()
            if surf is not None:
                self._write(surf, file)

    def _key(self, path, variant):
        st = os.stat(path)
        raw = f"{path}\0{st.st_mtime_ns}\0{st.st_size}\0{variant}".encode()
        return hashlib.sha256(raw).hexdigest()[:40]

    def get(self, path, variant, make, scale=1):
        """Surface cairo de la miniature ``variant`` de ``path``. ``make()`` la calcule si besoin et
        renvoie une surface cairo (ou None si l'image est illisible). ``scale`` : échelle d'écran."""
        try:
            key, file = self._file(path, variant)
        except OSError:
            return None
        surf = self._mem.get(key)
        if surf is not None or key in self._bad:
            return surf
        try:
            surf = cairo.ImageSurface.create_from_png(file)
        except (cairo.Error, OSError, MemoryError):
            surf = make()
            if surf is None:
                self._bad.add(key)
                return None
            if self._later:
                self._later(lambda s=surf, f=file: self._write(s, f))
            else:
                self._write(surf, file)
        surf.set_device_scale(scale, scale)
        if len(self._mem) > 400:
            self._mem.clear()
        self._mem[key] = surf
        return surf


    def prune(self, max_age_days=60):
        """Efface les miniatures plus utilisées depuis longtemps (recalculées si besoin)."""
        limit = time.time() - max_age_days * 86400
        for root, _dirs, files in os.walk(self.dir):
            for name in files:
                p = os.path.join(root, name)
                try:
                    if os.stat(p).st_atime < limit and os.stat(p).st_mtime < limit:
                        os.unlink(p)
                except OSError:
                    pass


def surface_from_pixbuf(pb):
    """Surface cairo ARGB32 contenant ``pb``."""
    from gi.repository import Gdk
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
    cr = cairo.Context(surf)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    return surf
