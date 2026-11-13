"""Wizz / poke : fenêtre surgissante animée, façon messagerie des années 2000.

La fenêtre tremble, l'avatar rebondit sur fond de rayons de soleil, le titre « WIZZ ! » en
WordArt arc-en-ciel se dandine au milieu d'étoiles scintillantes, le tout avec un bzzz de
synthèse. Elle se referme seule après quelques secondes, ou d'un clic.
"""

import logging
import math
import os
import random
import shutil
import struct
import subprocess
import wave

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo  # noqa: E402

log = logging.getLogger("qchat")

BASE_W, BASE_H = 440, 290    # repère de dessin de la carte
ZOOM = 1.5                   # taille réelle à l'écran : 660 × 435
WIDTH, HEIGHT = int(BASE_W * ZOOM), int(BASE_H * ZOOM)
LIFETIME = 9.0          # secondes avant fermeture automatique
SHAKE = 1.3             # durée du tremblement
FPS = 60
FONT = "Comic Sans MS, Comic Neue, Chalkboard, Purisa, cursive"

THEMES = {
    # ciel bleu glacé et bordure verte : l'ambiance messagerie instantanée de 2003
    "wizz": {"bg": ((0.84, 0.93, 1.0), (0.55, 0.78, 1.0)), "rays": (1.0, 0.85, 0.2),
             "border": ((0.2, 0.75, 0.25), (0.15, 0.45, 0.95)), "title": "WIZZ !",
             "emoji": "⚡", "button": "Répondre"},
    "poke": {"bg": ((1.0, 0.9, 0.96), (1.0, 0.68, 0.84)), "rays": (1.0, 0.6, 0.15),
             "border": ((1.0, 0.35, 0.65), (0.6, 0.25, 0.95)), "title": "POKE !",
             "emoji": "👉", "button": "Répondre"},
}


def _ease_out_elastic(t):
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return 2 ** (-10 * t) * math.sin((t * 10 - 0.75) * (2 * math.pi / 3)) + 1


def _rainbow(phase):
    """Couleur de l'arc-en-ciel pour une phase quelconque (période 1)."""
    h = phase % 1.0 * 6
    i, f = int(h), h - int(h)
    return [(1, f, 0), (1 - f, 1, 0), (0, 1, f), (0, 1 - f, 1), (f, 0, 1), (1, 0, 1 - f)][i]


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# -- son ----------------------------------------------------------------------
def buzz_path(cache_dir):
    """Fabrique (une fois) un « bzzzz-bzzzz » de synthèse et renvoie son chemin."""
    path = os.path.join(cache_dir, "wizz.wav")
    if os.path.isfile(path):
        return path
    rate = 22050
    frames = bytearray()
    for burst in range(2):
        for i in range(int(rate * 0.32)):
            t = i / rate
            env = min(1.0, t * 40) * min(1.0, (0.32 - t) * 30)
            freq = 150 + 40 * math.sin(2 * math.pi * 9 * t)          # vibreur qui « tangue »
            saw = 2 * ((t * freq) % 1.0) - 1
            wobble = 0.55 + 0.45 * (1 if math.sin(2 * math.pi * 28 * t) > 0 else -0.2)
            frames += struct.pack("<h", int(9000 * env * saw * wobble))
        frames += bytes(2 * int(rate * 0.08)) if burst == 0 else b""
    os.makedirs(cache_dir, exist_ok=True)
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    os.replace(tmp, path)
    return path


def play_buzz(cache_dir):
    try:
        path = buzz_path(cache_dir)
    except OSError as e:
        log.warning("Son du wizz indisponible : %s", e)
        return
    for player in ("pw-play", "paplay", "aplay"):
        exe = shutil.which(player)
        if exe:
            args = [exe, "-q", path] if player == "aplay" else [exe, path]
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                continue
            return


# -- tremblement d'un widget existant (côté expéditeur) -----------------------
def shake_widget(widget, duration=0.6, amplitude=10):
    """Fait trembler un widget en jouant sur ses marges (fonctionne aussi sous Wayland)."""
    start = GLib.get_monotonic_time()
    base = (widget.get_margin_start(), widget.get_margin_top())

    def tick(w, _clock):
        t = (GLib.get_monotonic_time() - start) / 1e6
        if t >= duration:
            w.set_margin_start(base[0])
            w.set_margin_end(base[0])
            w.set_margin_top(base[1])
            w.set_margin_bottom(base[1])
            return False
        a = amplitude * (1 - t / duration)
        dx, dy = int(a * math.sin(t * 90)), int(a * 0.6 * math.cos(t * 70))
        w.set_margin_start(base[0] + max(dx, 0))
        w.set_margin_end(base[0] + max(-dx, 0))
        w.set_margin_top(base[1] + max(dy, 0))
        w.set_margin_bottom(base[1] + max(-dy, 0))
        return True

    widget.add_tick_callback(tick)


# -- fenêtre surgissante --------------------------------------------------------
class WizzPopup(Gtk.Window):
    """``name`` vous a envoyé un wizz/poke. ``on_reply()`` ouvre la conversation."""

    _open = []      # fenêtres affichées, pour décaler les suivantes

    def __init__(self, name, kind="wizz", avatar=None, on_reply=None, initials="?"):
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title=f"{kind.capitalize()} de {name}")
        self.kind = kind if kind in THEMES else "wizz"
        self.theme = THEMES[self.kind]
        self.name = name
        self.initials = initials
        self.on_reply = on_reply
        self.start = None
        self.closing = None
        self.rng = random.Random()
        self.stars = [(self.rng.uniform(0, BASE_W), self.rng.uniform(0, BASE_H),
                       self.rng.uniform(5, 13), self.rng.uniform(0, 1), self.rng.uniform(-30, -12))
                      for _ in range(22)]
        self.avatar = None
        if avatar:
            try:
                self.avatar = GdkPixbuf.Pixbuf.new_from_file_at_scale(avatar, 96, 96, False)
            except GLib.Error:
                self.avatar = None

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(False)
        self.set_urgency_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_default_size(WIDTH, HEIGHT)
        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        self.transparent = bool(visual and screen.is_composited())
        if self.transparent:
            self.set_visual(visual)
        self.get_style_context().add_class("wizz-popup")

        overlay = Gtk.Overlay()
        self.area = Gtk.DrawingArea()
        self.area.set_size_request(WIDTH, HEIGHT)
        self.area.connect("draw", self._draw)
        ev = Gtk.EventBox(visible_window=False)
        ev.add(self.area)
        ev.connect("button-press-event", lambda *_: self.dismiss())
        overlay.add(ev)

        reply = Gtk.Button(label=self.theme["button"])
        reply.get_style_context().add_class("wizz-reply")
        reply.set_halign(Gtk.Align.CENTER)
        reply.set_valign(Gtk.Align.END)
        reply.set_margin_bottom(int(18 * ZOOM))
        reply.connect("clicked", self._reply)
        overlay.add_overlay(reply)
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("wizz-close")
        close.set_tooltip_text("Fermer")
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.START)
        close.set_margin_top(int(20 * ZOOM))
        close.set_margin_end(int(20 * ZOOM))
        close.connect("clicked", lambda *_: self.dismiss())
        overlay.add_overlay(close)
        self.add(overlay)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *_: WizzPopup._open.remove(self) if self in WizzPopup._open
                     else None)
        self.base_pos = None
        self.add_tick_callback(self._tick)

    # -- cycle de vie -------------------------------------------------------------
    def popup(self):
        """Affiche la fenêtre au centre de l'écran principal, au premier plan."""
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        offset = 34 * len(WizzPopup._open)
        WizzPopup._open.append(self)
        if mon is not None:
            geo = mon.get_geometry()
            x = geo.x + (geo.width - WIDTH) // 2 + offset
            y = geo.y + (geo.height - HEIGHT) // 2 + offset
            self.base_pos = (x, y)
            self.move(x, y)        # ignoré sous Wayland : le compositeur centre la fenêtre
        else:
            self.set_position(Gtk.WindowPosition.CENTER)
        self.show_all()
        self.present_with_time(Gtk.get_current_event_time() or Gdk.CURRENT_TIME)
        GLib.timeout_add(int(LIFETIME * 1000), lambda: self.dismiss() and False)

    def dismiss(self):
        if self.closing is None:
            self.closing = GLib.get_monotonic_time()
        return False

    def _reply(self, *_):
        if self.on_reply:
            self.on_reply()
        self.dismiss()

    def _on_key(self, _w, event):
        if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_space):
            self.dismiss()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._reply()
            return True
        return False

    def _tick(self, _w, _clock):
        now = GLib.get_monotonic_time()
        if self.start is None:
            self.start = now
        t = (now - self.start) / 1e6
        if self.closing is not None and (now - self.closing) / 1e6 > 0.35:
            self.destroy()
            return False
        # Sous X11, la fenêtre elle-même tremble ; partout ailleurs, son contenu.
        if self.base_pos and t < SHAKE and not self._wayland():
            dx, dy = self._shake(t)
            self.move(self.base_pos[0] + int(dx), self.base_pos[1] + int(dy))
        self.area.queue_draw()
        return True

    @staticmethod
    def _wayland():
        return type(Gdk.Display.get_default()).__name__.startswith("Wayland")

    def _shake(self, t):
        if t >= SHAKE:
            return 0.0, 0.0
        a = 16 * (1 - t / SHAKE) ** 1.5
        return a * math.sin(t * 95), a * 0.7 * math.cos(t * 77)

    # -- dessin -------------------------------------------------------------------
    def _draw(self, area, cr):
        now = GLib.get_monotonic_time()
        t = 0.0 if self.start is None else (now - self.start) / 1e6
        w, h = area.get_allocated_width(), area.get_allocated_height()
        alpha = 1.0
        scale = 1.0
        if self.closing is not None:
            f = min(1.0, (now - self.closing) / 1e6 / 0.35)
            alpha, scale = 1 - f, 1 - 0.25 * f
        else:
            scale = 0.3 + 0.7 * _ease_out_elastic(min(1.0, t / 0.9))

        if self.transparent:
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
        cr.push_group()
        if self._wayland() or not self.base_pos:
            dx, dy = self._shake(t)
            cr.translate(dx * 0.6, dy * 0.6)
        cr.translate(w / 2, h / 2)
        cr.scale(scale, scale)
        cr.translate(-w / 2, -h / 2)
        cr.scale(w / BASE_W, h / BASE_H)
        self._draw_card(cr, t, BASE_W, BASE_H)
        cr.pop_group_to_source()
        cr.paint_with_alpha(alpha)
        return True

    def _draw_card(self, cr, t, w, h):
        th = self.theme
        m = 8 if self.transparent else 0
        r = 22 if self.transparent else 0
        # ombre
        if self.transparent:
            _rounded(cr, m + 3, m + 5, w - 2 * m, h - 2 * m, r)
            cr.set_source_rgba(0, 0, 0, 0.25)
            cr.fill()
        # fond dégradé façon verre
        _rounded(cr, m, m, w - 2 * m, h - 2 * m, r)
        cr.save()
        cr.clip_preserve()
        grad = cairo.LinearGradient(0, 0, 0, h)
        grad.add_color_stop_rgb(0, *th["bg"][0])
        grad.add_color_stop_rgb(1, *th["bg"][1])
        cr.set_source(grad)
        cr.fill()

        # rayons de soleil tournants derrière l'avatar
        cx, cy = w / 2, 82
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(t * 0.9)
        for i in range(16):
            cr.move_to(0, 0)
            a0 = i * 2 * math.pi / 16
            cr.arc(0, 0, 330, a0, a0 + math.pi / 16)
            cr.close_path()
        rays = cairo.RadialGradient(0, 0, 10, 0, 0, 260)
        rays.add_color_stop_rgba(0, *th["rays"], 0.55)
        rays.add_color_stop_rgba(1, *th["rays"], 0.0)
        cr.set_source(rays)
        cr.fill()
        cr.restore()

        # reflet brillant sur la moitié haute
        gloss = cairo.LinearGradient(0, 0, 0, h * 0.45)
        gloss.add_color_stop_rgba(0, 1, 1, 1, 0.55)
        gloss.add_color_stop_rgba(1, 1, 1, 1, 0.0)
        cr.rectangle(0, 0, w, h * 0.45)
        cr.set_source(gloss)
        cr.fill()

        # étoiles scintillantes qui montent
        for x0, y0, size, phase, speed in self.stars:
            y = (y0 + speed * t) % (h + 20) - 10
            x = x0 + 8 * math.sin(t * 2 + phase * 6)
            tw = 0.5 + 0.5 * math.sin(t * 6 + phase * 12)
            behind_text = 125 < y < 235 and 40 < x < w - 40     # discrètes derrière le texte
            self._star(cr, x, y, size * (0.6 + 0.6 * tw) * (0.6 if behind_text else 1), t * 2 + phase * 6)
            col = _rainbow(phase + t * 0.3)
            cr.set_source_rgba(*col, (0.35 + 0.6 * tw) * (0.3 if behind_text else 1))
            cr.fill()
        cr.restore()

        # bordure arc-en-ciel… ou presque
        _rounded(cr, m + 2, m + 2, w - 2 * m - 4, h - 2 * m - 4, max(r - 2, 0))
        k = 0.5 + 0.5 * math.sin(t * 5)
        c0, c1 = th["border"]
        cr.set_source_rgb(*(a + (b - a) * k for a, b in zip(c0, c1)))
        cr.set_line_width(5)
        cr.stroke()
        _rounded(cr, m + 6, m + 6, w - 2 * m - 12, h - 2 * m - 12, max(r - 6, 0))
        cr.set_source_rgba(1, 1, 1, 0.8)
        cr.set_line_width(1.5)
        cr.set_dash([6, 5], -t * 30)
        cr.stroke()
        cr.set_dash([])

        # avatar qui rebondit
        bounce = abs(math.sin(t * 4.2)) * 10 * max(0.0, 1 - t / 6) if t > 0.6 else 0
        pop = _ease_out_elastic(min(1.0, max(0.0, (t - 0.1) / 0.8)))
        ar = 40 * pop
        ay = cy - bounce
        if ar > 1:
            cr.save()
            cr.arc(cx, ay, ar + 5, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill()
            cr.arc(cx, ay, ar, 0, 2 * math.pi)
            cr.clip()
            if self.avatar:
                cr.translate(cx - ar, ay - ar)
                cr.scale(2 * ar / self.avatar.get_width(), 2 * ar / self.avatar.get_height())
                Gdk.cairo_set_source_pixbuf(cr, self.avatar, 0, 0)
                cr.paint()
            else:
                cr.set_source_rgb(*th["border"][1])
                cr.paint()
                cr.set_source_rgb(1, 1, 1)
                self._text(cr, self.initials, f"{FONT} Bold {max(1, int(ar * 0.7))}px", cx, ay)
            cr.restore()
            cr.new_path()
            cr.arc(cx, ay, ar + 5, 0, 2 * math.pi)
            cr.set_source_rgb(*th["border"][0])
            cr.set_line_width(3)
            cr.stroke()
            # petit éclair / doigt qui gigote à côté de l'avatar
            ex = cx + ar + 22 + 6 * math.sin(t * 12)
            self._text(cr, th["emoji"], f"Noto Color Emoji {int(26 * pop) or 1}px", ex, ay - ar + 8,
                       angle=0.3 * math.sin(t * 9))

        # titre WordArt : lettres arc-en-ciel qui ondulent
        title = th["title"]
        font = f"{FONT} Bold 50px"
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(font))
        widths = []
        for ch in title:
            layout.set_text(ch, -1)
            widths.append(layout.get_pixel_size()[0])
        total = sum(widths) + 2 * (len(title) - 1)
        x = (w - total) / 2
        appear = min(1.0, max(0.0, (t - 0.25) / 0.5))
        for i, ch in enumerate(title):
            if ch != " " and appear > i / len(title):
                y = 158 + 6 * math.sin(t * 7 + i * 0.8)
                ang = 0.12 * math.sin(t * 5 + i)
                self._wordart(cr, ch, font, x + widths[i] / 2, y, ang, _rainbow(i / len(title) + t * 0.5))
            x += widths[i] + 2

        # « Untel vous a envoyé un wizz ! »
        # sur une étiquette blanche, pour rester lisible au-dessus des rayons et des étoiles
        pill_w, pill_h, pill_y = w - 70, 34, 210
        _rounded(cr, (w - pill_w) / 2, pill_y - pill_h / 2, pill_w, pill_h, pill_h / 2)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.fill_preserve()
        cr.set_source_rgb(*th["border"][1])
        cr.set_line_width(1.5)
        cr.stroke()
        cr.set_source_rgb(0.08, 0.1, 0.25)
        msg = (f"<b>{GLib.markup_escape_text(self.name)}</b> vous a envoyé un "
               f"<b>{self.kind}</b> !")
        self._text(cr, msg, f"{FONT} 16px", w / 2, pill_y, max_width=pill_w - 24, markup=True)

    @staticmethod
    def _star(cr, x, y, r, rot):
        cr.new_path()
        for i in range(10):
            rad = r if i % 2 == 0 else r * 0.45
            a = rot + i * math.pi / 5 - math.pi / 2
            cr.line_to(x + rad * math.cos(a), y + rad * math.sin(a))
        cr.close_path()

    @staticmethod
    def _text(cr, text, font, x, y, angle=0.0, max_width=None, markup=False):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(font))
        if markup:
            layout.set_markup(text, -1)
        else:
            layout.set_text(text, -1)
        if max_width:
            layout.set_width(int(max_width * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            layout.set_alignment(Pango.Alignment.CENTER)
        tw, th = layout.get_pixel_size()
        cr.save()
        cr.translate(x, y)
        cr.rotate(angle)
        cr.move_to(-(max_width or tw) / 2, -th / 2)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    @staticmethod
    def _wordart(cr, ch, font, x, y, angle, color):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(font))
        layout.set_text(ch, -1)
        tw, th = layout.get_pixel_size()
        cr.save()
        cr.translate(x, y)
        cr.rotate(angle)
        # halo blanc qui détache la lettre du fond
        cr.move_to(-tw / 2, -th / 2)
        PangoCairo.layout_path(cr, layout)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(7)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
        # ombre portée façon 3D
        for d in (4, 3, 2):
            cr.move_to(-tw / 2 + d, -th / 2 + d)
            PangoCairo.layout_path(cr, layout)
            cr.set_source_rgba(0.1, 0.1, 0.3, 0.25)
            cr.fill()
        cr.move_to(-tw / 2, -th / 2)
        PangoCairo.layout_path(cr, layout)
        cr.set_source_rgb(*(0.85 * c for c in color))
        cr.fill_preserve()
        cr.set_source_rgb(0.1, 0.08, 0.3)
        cr.set_line_width(2.4)
        cr.stroke()
        # reflet blanc sur le haut de la lettre
        cr.move_to(-tw / 2, -th / 2)
        PangoCairo.layout_path(cr, layout)
        cr.clip()
        cr.rectangle(-tw, -th / 2, 2 * tw, th * 0.42)
        cr.set_source_rgba(1, 1, 1, 0.45)
        cr.fill()
        cr.restore()
