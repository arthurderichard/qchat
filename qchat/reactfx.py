"""Animations des réactions : emoji qui surgit (zoom, légère rotation, rebond) puis se pose sous le
message, pastille qui rebondit au survol.

GTK 3 ne sait ni agrandir ni tourner un texte en CSS : les emoji sont donc dessinés avec cairo, avec
une transformation recalculée à chaque image (horloge d'affichage de GTK).
"""

import math

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gtk, Pango, PangoCairo  # noqa: E402

POP_TIME = 0.9          # apparition : zoom depuis 0 avec dépassement puis rebond
BUMP_TIME = 0.8         # survol : grossit, rebondit, revient
FLY_GROW = 0.55         # trajet : l'emoji grossit au centre du message…
FLY_MOVE = 0.4          # … puis file se poser sur sa pastille
FLY_SIZE = 64


def pop_curve(t):
    """(échelle, rotation) d'une apparition : de 0 à ~1,25 puis rebond amorti vers 1."""
    if t >= POP_TIME:
        return 1.0, 0.0
    damp = math.exp(-6 * t)
    return 1 - damp * math.cos(13 * t), -0.5 * damp * math.cos(11 * t)


def bump_curve(t):
    """(échelle, rotation) d'un rebond sur place : ~1,45 puis ~0,9 puis 1."""
    if t >= BUMP_TIME:
        return 1.0, 0.0
    return 1 + 0.9 * math.exp(-6 * t) * math.sin(12 * t), 0.22 * math.exp(-5 * t) * math.sin(16 * t)


def _ease_in_out(u):
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def draw_emoji(widget, cr, art, size):
    """Dessine ``art`` (caractère emoji ou GdkPixbuf) centré sur l'origine, dans un carré ``size``."""
    if isinstance(art, GdkPixbuf.Pixbuf):
        w, h = art.get_width(), art.get_height()
        scale = size / max(w, h, 1)
        cr.save()
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, art, -w / 2, -h / 2)
        cr.paint()
        cr.restore()
        return
    layout = widget.create_pango_layout(art)
    layout.set_font_description(Pango.FontDescription.from_string(f"{max(1, size * 0.8):.1f}px"))
    _ink, logical = layout.get_pixel_extents()
    cr.move_to(-logical.width / 2 - logical.x, -logical.height / 2 - logical.y)
    PangoCairo.show_layout(cr, layout)


class EmojiSprite(Gtk.DrawingArea):
    """Emoji dessiné, capable de surgir (``pop``) et de rebondir (``bump``).
    La zone est plus grande que l'emoji pour laisser la place au dépassement de l'animation."""

    def __init__(self, art, size, room=1.5):
        super().__init__()
        self.art, self.size = art, size
        self.hidden = False             # caché en attendant l'emoji qui arrive en vol
        self._curve = None
        self._start = 0
        self._tick = 0
        box = int(math.ceil(size * room))
        self.set_size_request(box, box)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._on_draw)
        self.connect("unrealize", lambda *_a: self._stop())

    def _play(self, curve):
        self._curve = curve
        self.hidden = False
        clock = self.get_frame_clock()
        if clock is None:               # pas encore affiché : on rejouera au premier dessin
            self._start = None
        else:
            self._start = clock.get_frame_time()
        if not self._tick:
            self._tick = self.add_tick_callback(self._on_tick)
        self.queue_draw()

    def pop(self):
        self._play(pop_curve)

    def bump(self):
        self._play(bump_curve)

    def _stop(self):
        if self._tick:
            self.remove_tick_callback(self._tick)
            self._tick = 0
        self._curve = None

    def _elapsed(self):
        clock = self.get_frame_clock()
        if clock is None:
            return 0.0
        if self._start is None:
            self._start = clock.get_frame_time()
        return (clock.get_frame_time() - self._start) / 1e6

    def _on_tick(self, _w, _clock):
        self.queue_draw()
        limit = POP_TIME if self._curve is pop_curve else BUMP_TIME
        if self._curve is None or self._elapsed() >= limit:
            self._tick = 0
            self._curve = None
            return False
        return True

    def _on_draw(self, _w, cr):
        if self.hidden:
            return False
        scale, angle = self._curve(self._elapsed()) if self._curve else (1.0, 0.0)
        alloc = self.get_allocation()
        cr.translate(alloc.width / 2, alloc.height / 2)
        cr.rotate(angle)
        cr.scale(max(scale, 0.001), max(scale, 0.001))
        draw_emoji(self, cr, self.art, self.size)
        return False


class FxLayer(Gtk.Box):
    """Calque transparent posé sur la liste des messages : un emoji y surgit au centre du message,
    grossit en tournant, puis vole se poser sur sa pastille de réaction.
    (Gtk.Box sans fenêtre propre : il ne capte aucun clic.)"""

    def __init__(self):
        super().__init__()
        self.set_can_focus(False)
        self._flights = []
        self._tick = 0
        self.connect("draw", self._on_draw)

    def fly(self, art, source, target):
        """``source`` : widget d'où l'emoji surgit ; ``target`` : EmojiSprite où il se pose."""
        clock = self.get_frame_clock()
        start = self._center(source) if source.get_mapped() and target.get_mapped() else None
        alloc = self.get_allocation()
        if clock is None or start is None or not (0 <= start[1] <= alloc.height):
            target.pop()                # message hors de vue : simple apparition sur place
            return
        target.hidden = True
        target.queue_draw()
        self._flights.append({"art": art, "source": source, "target": target,
                              "start": clock.get_frame_time()})
        if not self._tick:
            self._tick = self.add_tick_callback(self._on_tick)

    def _center(self, widget):
        if not widget.get_mapped():
            return None
        alloc = widget.get_allocation()
        pos = widget.translate_coordinates(self, alloc.width // 2, alloc.height // 2)
        return pos if pos else None

    def _on_tick(self, _w, clock):
        now = clock.get_frame_time()
        for f in list(self._flights):
            if (now - f["start"]) / 1e6 >= FLY_GROW + FLY_MOVE or self._center(f["target"]) is None:
                self._flights.remove(f)
                f["target"].bump()          # se pose avec un petit rebond
        self.queue_draw()
        if not self._flights:
            self._tick = 0
            return False
        return True

    def _on_draw(self, _w, cr):
        clock = self.get_frame_clock()
        if clock is None:
            return False
        now = clock.get_frame_time()
        for f in self._flights:
            start, end = self._center(f["source"]), self._center(f["target"])
            if start is None or end is None:
                continue
            t = (now - f["start"]) / 1e6
            small = f["target"].size
            if t < FLY_GROW:
                (x, y), size = start, FLY_SIZE
                scale, angle = pop_curve(t)
            else:
                u = _ease_in_out(min(1.0, (t - FLY_GROW) / FLY_MOVE))
                x = start[0] + (end[0] - start[0]) * u
                y = start[1] + (end[1] - start[1]) * u - 36 * math.sin(math.pi * u)   # petite courbe
                size = FLY_SIZE + (small - FLY_SIZE) * u
                scale, angle = pop_curve(FLY_GROW)[0], 0.35 * math.sin(math.pi * u)
            cr.save()
            cr.translate(x, y)
            cr.rotate(angle)
            cr.scale(max(scale, 0.001), max(scale, 0.001))
            draw_emoji(self, cr, f["art"], size)
            cr.restore()
        return False
