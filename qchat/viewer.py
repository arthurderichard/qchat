"""Visionneuse intégrée : images (GIF animés compris), PDF et fichiers texte."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import warnings

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("GtkSource", "4")
    from gi.repository import GtkSource  # noqa: E402
except (ValueError, ImportError):
    GtkSource = None

log = logging.getLogger("qchat")

TEXT_LIMIT = 2 * 1024 * 1024        # au-delà, seul le début du fichier texte est affiché
ANIM_LIMIT = 50 * 1024 * 1024       # pas d'animation pour les fichiers énormes
PDF_DPI = 150
ZOOM_STEPS = (0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
_TEXT_MIMES = {"application/json", "application/xml", "application/javascript", "application/x-yaml",
               "application/yaml", "application/sql", "application/x-sh", "application/x-shellscript",
               "application/toml", "application/x-desktop", "application/x-php", "application/x-ruby",
               "application/x-perl", "application/x-python", "application/x-subrip", "application/csv"}


def human_size(n):
    for unit in ("octets", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return f"{n} {unit}" if unit == "octets" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


class NoPreview(Exception):
    """Le fichier ne peut pas être affiché dans la visionneuse (le message est destiné à l'utilisateur)."""


def _head(path, n=8192):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return None


def _looks_like_text(head):
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start >= len(head) - 4     # caractère multi-octet coupé en fin de lecture


def viewable_kind(path, mime):
    """Type d'aperçu intégré possible : 'image', 'pdf', 'text', ou None (application externe)."""
    head = _head(path)
    if head is None:
        return None
    mime = mime or "application/octet-stream"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if mime.startswith("image/") or mime == "application/octet-stream":
        try:
            if GdkPixbuf.Pixbuf.get_file_info(path)[0] is not None:
                return "image"
        except (GLib.Error, TypeError):
            pass
        if mime.startswith("image/"):
            return None
    ctype = Gio.content_type_from_mime_type(mime) or mime
    texty = (mime.startswith("text/") or mime in _TEXT_MIMES or Gio.content_type_is_a(ctype, "text/plain")
             or mime == "application/octet-stream")
    if texty and _looks_like_text(head):
        return "text"
    return None


def load_animation(path):
    """Animation GdkPixbuf si le fichier est une image animée (GIF, WebP…), sinon None."""
    try:
        if os.path.getsize(path) > ANIM_LIMIT:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            anim = GdkPixbuf.PixbufAnimation.new_from_file(path)
            return None if anim.is_static_image() else anim
    except (GLib.Error, OSError):
        return None


class AnimatedImage(Gtk.Image):
    """Image animée redimensionnée à la volée (Gtk.Image ne sait pas mettre une animation à l'échelle).
    ``size_fn(largeur, hauteur)`` renvoie la taille d'affichage souhaitée."""

    def __init__(self, anim, size_fn):
        super().__init__()
        self._size_fn = size_fn
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self._iter = anim.get_iter(None)
        self._timer = None
        self.connect("map", lambda *_a: self._schedule())
        self.connect("unmap", lambda *_a: self._stop())
        self.connect("destroy", lambda *_a: self._stop())
        self.refresh()

    def refresh(self):
        pb = self._iter.get_pixbuf()
        w, h = self._size_fn(pb.get_width(), pb.get_height())
        if (w, h) != (pb.get_width(), pb.get_height()):
            pb = pb.scale_simple(max(1, w), max(1, h), GdkPixbuf.InterpType.BILINEAR)
        self.set_from_pixbuf(pb)

    def _stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None

    def _schedule(self):
        self._stop()
        delay = self._iter.get_delay_time()
        if delay >= 0:
            self._timer = GLib.timeout_add(max(delay, 20), self._tick)

    def _tick(self):
        self._timer = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self._iter.advance(None)
        self.refresh()
        self._schedule()
        return False


def fit(w, h, max_w, max_h, upscale=False):
    scale = min(max_w / w, max_h / h)
    if not upscale:
        scale = min(scale, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


# ==========================================================================
# Contenus
# ==========================================================================
class _Content:
    """Interface commune : un widget, un zoom (None = ajusté à la fenêtre)."""
    widget = None
    zoomable = True

    def set_zoom(self, zoom):
        pass

    def effective_zoom(self):
        return 1.0

    def on_resize(self):
        pass

    def close(self):
        pass


class _ImageContent(_Content):
    def __init__(self, viewer, path):
        self.v = viewer
        self.zoom = None
        self.anim = load_animation(path)
        self.pixbuf = None
        if self.anim is None:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)          # GLib.Error si illisible
            self.pixbuf = pb.apply_embedded_orientation() or pb
            self.size = (self.pixbuf.get_width(), self.pixbuf.get_height())
            self.image = Gtk.Image()
        else:
            self.size = (self.anim.get_width(), self.anim.get_height())
            self.image = AnimatedImage(self.anim, lambda w, h: self._target())
        self.image.set_halign(Gtk.Align.CENTER)
        self.image.set_valign(Gtk.Align.CENTER)
        ev = Gtk.EventBox()
        ev.add(self.image)
        ev.connect("button-press-event", self._on_click)
        self.widget = ev
        self._last = None

    def _target(self):
        w, h = self.size
        if self.zoom is None:
            aw, ah = self.v.area_size()
            return fit(w, h, max(aw - 32, 50), max(ah - 32, 50))
        return max(1, round(w * self.zoom)), max(1, round(h * self.zoom))

    def effective_zoom(self):
        return self._target()[0] / self.size[0]

    def render(self):
        target = self._target()
        if target == self._last:
            return
        self._last = target
        if self.pixbuf is not None:
            pb = self.pixbuf
            if target != self.size:
                pb = pb.scale_simple(*target, GdkPixbuf.InterpType.BILINEAR)
            self.image.set_from_pixbuf(pb)
        else:
            self.image.refresh()

    def set_zoom(self, zoom):
        self.zoom = zoom
        self.render()

    def on_resize(self):
        if self.zoom is None:
            self.render()

    def _on_click(self, _w, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.v.set_zoom(None if self.zoom is not None else 1.0)
        return False


class _TextContent(_Content):
    def __init__(self, viewer, path, name, mime, dark):
        self.v = viewer
        with open(path, "rb") as f:
            data = f.read(TEXT_LIMIT + 1)
        truncated = len(data) > TEXT_LIMIT
        data = data[:TEXT_LIMIT]
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16", errors="replace")
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as e:
                text = data.decode("utf-8", errors="replace") if e.start >= len(data) - 4 \
                    else data.decode("latin-1")
        if GtkSource:
            buf = GtkSource.Buffer()
            lang = GtkSource.LanguageManager.get_default().guess_language(
                name, Gio.content_type_from_mime_type(mime or "") or None)
            if lang:
                buf.set_language(lang)
            mgr = GtkSource.StyleSchemeManager.get_default()
            for sid in (("Adwaita-dark", "oblivion", "cobalt") if dark else ("Adwaita", "classic", "tango")):
                scheme = mgr.get_scheme(sid)
                if scheme:
                    buf.set_style_scheme(scheme)
                    break
            view = GtkSource.View.new_with_buffer(buf)
            view.set_show_line_numbers(True)
            view.set_highlight_current_line(False)
        else:
            view = Gtk.TextView()
            buf = view.get_buffer()
        buf.set_text(text)
        buf.place_cursor(buf.get_start_iter())
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        for side in ("left", "right", "top", "bottom"):
            getattr(view, f"set_{side}_margin")(12)
        view.get_style_context().add_class("viewer-text")
        self.view = view
        self._css = Gtk.CssProvider()
        view.get_style_context().add_provider(self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.zoom = None
        self.set_zoom(None)
        if truncated:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            note = Gtk.Label(label=f"Fichier volumineux : seuls les {TEXT_LIMIT // (1024 * 1024)} premiers Mo "
                                   "sont affichés. Ouvrez-le avec une application pour le lire en entier.",
                             xalign=0.0, wrap=True)
            note.get_style_context().add_class("viewer-note")
            box.pack_start(note, False, False, 0)
            box.pack_start(view, True, True, 0)
            self.widget = box
        else:
            self.widget = view

    def effective_zoom(self):
        return self.zoom or 1.0

    def set_zoom(self, zoom):
        self.zoom = zoom
        size = 10.5 * (zoom or 1.0)
        self._css.load_from_data(f"textview, textview text {{ font-size: {size:.1f}pt; }}".encode())


class _PdfContent(_Content):
    """Pages rendues à la demande par ``pdftoppm`` (poppler-utils), seulement autour de la zone visible."""

    def __init__(self, viewer, path):
        self.v = viewer
        self.path = path
        self.zoom = None
        self.tmp = tempfile.mkdtemp(prefix="qchat-pdf-")
        self.sizes = self._page_sizes(path)      # en points
        if not self.sizes:
            raise NoPreview("Document PDF illisible ou protégé.")
        self.scale = max(1, viewer.get_scale_factor())
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, halign=Gtk.Align.CENTER,
                           margin_top=16, margin_bottom=16)
        self.pages = []
        for _ in self.sizes:
            img = Gtk.Image()
            frame = Gtk.EventBox()
            frame.get_style_context().add_class("pdf-page")
            frame.add(img)
            self.box.pack_start(frame, False, False, 0)
            self.pages.append([frame, img, None])     # widget, image, largeur affichée
        self.widget = self.box
        self._lock = threading.Condition()
        self._wanted = set()
        self._rendered = set()
        self._closed = False
        self._width = None
        threading.Thread(target=self._worker, daemon=True).start()
        viewer.vadj.connect("value-changed", self._on_scroll)

    @staticmethod
    def _page_sizes(path):
        try:
            n = int(re.search(r"^Pages:\s+(\d+)", subprocess.run(
                ["pdfinfo", path], capture_output=True, text=True, timeout=20).stdout, re.M).group(1))
            out = subprocess.run(["pdfinfo", "-f", "1", "-l", str(min(n, 5000)), path],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
            return []
        sizes = [(float(w), float(h)) for w, h in
                 re.findall(r"^Page\s+\d+\s+size:\s+([\d.]+) x ([\d.]+)", out, re.M)]
        rots = [int(r) for r in re.findall(r"^Page\s+\d+\s+rot:\s+(\d+)", out, re.M)]
        for i, r in enumerate(rots[:len(sizes)]):
            if r % 180 == 90:
                sizes[i] = sizes[i][::-1]
        return sizes or [(595.0, 842.0)] * n

    def _display_width(self, i):
        w, _h = self.sizes[i]
        if self.zoom is None:
            aw, _ah = self.v.area_size()
            maxw = max(self.sizes, key=lambda s: s[0])[0]
            return max(50, round(min(aw - 48, maxw * 1.5) * w / maxw))
        return max(20, round(w * self.zoom * 96 / 72))

    def effective_zoom(self):
        return self._display_width(0) / (self.sizes[0][0] * 96 / 72)

    def _layout(self):
        for i, (frame, img, _shown) in enumerate(self.pages):
            w = self._display_width(i)
            pw, ph = self.sizes[i]
            frame.set_size_request(w, round(w * ph / pw))
            if self.pages[i][2] != w:
                img.clear()
                self.pages[i][2] = None
        GLib.idle_add(self._update)

    def set_zoom(self, zoom):
        self.zoom = zoom
        self._layout()

    def on_resize(self):
        w = self.v.area_size()[0]
        if self.zoom is None and w != self._width:
            self._width = w
            self._layout()
        else:
            GLib.idle_add(self._update)

    def _on_scroll(self, *_a):
        if not self._closed:
            self._update()

    def _visible_pages(self):
        top = self.v.vadj.get_value()
        bottom = top + self.v.vadj.get_page_size()
        vis = []
        for i, (frame, _img, _w) in enumerate(self.pages):
            a = frame.get_allocation()
            if a.y + a.height >= top and a.y <= bottom:
                vis.append(i)
        return vis or [0]

    def _png(self, i):
        return os.path.join(self.tmp, f"p{i + 1}.png")

    def _update(self):
        if self._closed:
            return False
        vis = self._visible_pages()
        lo, hi = max(0, vis[0] - 2), min(len(self.pages) - 1, vis[-1] + 2)
        wanted = set()
        for i, page in enumerate(self.pages):
            frame, img, shown = page
            if lo <= i <= hi:
                w = self._display_width(i)
                if i in self._rendered:
                    if shown != w:
                        try:
                            ph = round(w * self.sizes[i][1] / self.sizes[i][0])
                            img.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(self._png(i), w, ph,
                                                                                        False))
                            page[2] = w
                        except GLib.Error:
                            pass
                else:
                    wanted.add(i)
            elif shown is not None and (i < lo - 6 or i > hi + 6):
                img.clear()          # libère la mémoire des pages éloignées
                page[2] = None
        with self._lock:
            self._wanted = wanted
            self._lock.notify()
        current = vis[len(vis) // 2] + 1
        self.v.set_position(f"Page {current} / {len(self.pages)}")
        return False

    def _worker(self):
        dpi = PDF_DPI * self.scale
        while True:
            with self._lock:
                while not self._wanted and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                i = min(self._wanted)
                self._wanted.discard(i)
            if i in self._rendered:
                continue
            try:
                subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(i + 1), "-l", str(i + 1),
                                "-singlefile", self.path, self._png(i)[:-4]],
                               capture_output=True, timeout=60, check=True)
                self._rendered.add(i)
            except (OSError, subprocess.SubprocessError) as e:
                log.info("Rendu de la page %d impossible : %s", i + 1, e)
                continue
            GLib.idle_add(self._update)

    def close(self):
        with self._lock:
            self._closed = True
            self._lock.notify()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ==========================================================================
# Fenêtre
# ==========================================================================
class Viewer(Gtk.Window):
    """``items`` : liste de dict {path, name, mime, size} ; flèches ← → pour passer de l'un à l'autre."""

    def __init__(self, parent, dark, on_save, on_open_external):
        super().__init__(title="Aperçu", transient_for=parent, destroy_with_parent=True)
        self.get_style_context().add_class("qchat")
        self.get_style_context().add_class("viewer")
        self.set_icon_name("qchat")
        self.dark = dark
        self._on_save = on_save
        self._on_external = on_open_external
        w, h = parent.get_size()
        self.set_default_size(max(640, int(w * 0.92)), max(480, int(h * 0.92)))
        self.items, self.index, self.content = [], 0, None

        hb = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(hb)
        self.hb = hb
        nav = Gtk.Box()
        nav.get_style_context().add_class("linked")
        self.prev_btn = self._button("go-previous-symbolic", "Précédent (←)", lambda: self.go(-1))
        self.next_btn = self._button("go-next-symbolic", "Suivant (→)", lambda: self.go(1))
        nav.pack_start(self.prev_btn, False, False, 0)
        nav.pack_start(self.next_btn, False, False, 0)
        hb.pack_start(nav)

        hb.pack_end(self._button("document-save-as-symbolic", "Enregistrer sous… (Ctrl+S)",
                                 lambda: self._on_save(self.item)))
        hb.pack_end(self._button("document-open-symbolic", "Ouvrir avec l'application par défaut",
                                 lambda: self._on_external(self.item["path"])))
        zoom = Gtk.Box()
        zoom.get_style_context().add_class("linked")
        self.zoom_out = self._button("zoom-out-symbolic", "Zoom arrière (−)", lambda: self.step_zoom(-1))
        self.zoom_label = Gtk.Button(label="100 %", tooltip_text="Ajuster à la fenêtre (0)")
        self.zoom_label.get_style_context().add_class("zoom-label")
        self.zoom_label.connect("clicked", lambda _b: self.set_zoom(None))
        self.zoom_in = self._button("zoom-in-symbolic", "Zoom avant (+)", lambda: self.step_zoom(1))
        for b in (self.zoom_out, self.zoom_label, self.zoom_in):
            zoom.pack_start(b, False, False, 0)
        self.zoom_box = zoom
        hb.pack_end(zoom)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.get_style_context().add_class("viewer-area")
        self.vadj = self.scroll.get_vadjustment()
        self.scroll.connect("size-allocate", self._on_size)
        self.scroll.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.scroll.connect("scroll-event", self._on_scroll_event)
        self.add(self.scroll)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *_a: self._close_content())
        self._size = None

    @staticmethod
    def _button(icon, tip, action):
        b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        b.set_tooltip_text(tip)
        b.connect("clicked", lambda _b: action())
        return b

    @property
    def item(self):
        return self.items[self.index]

    def area_size(self):
        a = self.scroll.get_allocation()
        return (a.width, a.height) if a.width > 1 else self.get_size()

    def show_items(self, items, index):
        self.items, self.index = items, index
        self._load()
        self.show_all()
        self.present()

    def go(self, delta):
        j = self.index + delta
        if 0 <= j < len(self.items):
            self.index = j
            self._load()

    def _close_content(self):
        if self.content:
            self.content.close()
            self.content = None

    def _load(self):
        self._close_content()
        child = self.scroll.get_child()
        if child:
            self.scroll.remove(child)
            child.destroy()
        it = self.item
        self.hb.set_title(it["name"])
        self.set_title(it["name"])
        self._base_sub = human_size(it["size"]) + (
            f"  ·  {self.index + 1} sur {len(self.items)}" if len(self.items) > 1 else "")
        self.hb.set_subtitle(self._base_sub)
        self.prev_btn.set_sensitive(self.index > 0)
        self.next_btn.set_sensitive(self.index < len(self.items) - 1)
        self.prev_btn.get_parent().set_visible(len(self.items) > 1)
        kind = viewable_kind(it["path"], it["mime"])
        try:
            if kind == "image":
                self.content = _ImageContent(self, it["path"])
            elif kind == "pdf":
                if not shutil.which("pdftoppm") or not shutil.which("pdfinfo"):
                    raise NoPreview("La lecture des PDF nécessite le paquet poppler-utils "
                                     "(sudo apt install poppler-utils).")
                self.content = _PdfContent(self, it["path"])
            elif kind == "text":
                self.content = _TextContent(self, it["path"], it["name"], it["mime"], self.dark)
            else:
                raise NoPreview("Aucun aperçu disponible pour ce type de fichier.")
        except NoPreview as e:
            self.content = None
            self._show_message(str(e))
            return
        except (GLib.Error, OSError) as e:
            log.info("Aperçu impossible pour %s : %s", it["path"], e)
            self.content = None
            self._show_message("Ce fichier n'a pas pu être affiché : il est peut-être endommagé.")
            return
        self.zoom_box.set_visible(self.content.zoomable)
        widget = self.content.widget
        if kind == "text":
            self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        else:
            self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroll.add(widget)
        widget.show_all()
        self.vadj.set_value(0)
        self.content.set_zoom(None)
        self._update_zoom_label()

    def _show_message(self, text):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                      valign=Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_gicon(
            Gio.content_type_get_icon(Gio.content_type_from_mime_type(self.item["mime"]) or ""),
            Gtk.IconSize.DIALOG)
        icon.set_pixel_size(96)
        box.pack_start(icon, False, False, 0)
        lbl = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=50)
        box.pack_start(lbl, False, False, 0)
        btn = Gtk.Button(label="Ouvrir avec l'application par défaut", halign=Gtk.Align.CENTER)
        btn.get_style_context().add_class("suggested-action")
        btn.connect("clicked", lambda _b: self._on_external(self.item["path"]))
        box.pack_start(btn, False, False, 0)
        self.zoom_box.set_visible(False)
        self.scroll.add(box)
        box.show_all()

    # -- zoom ------------------------------------------------------------------
    def set_zoom(self, zoom):
        if self.content:
            self.content.set_zoom(zoom)
            self._update_zoom_label()

    def step_zoom(self, direction):
        if not self.content:
            return
        cur = self.content.effective_zoom()
        if direction > 0:
            nxt = next((z for z in ZOOM_STEPS if z > cur + 0.01), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < cur - 0.01), ZOOM_STEPS[0])
        self.set_zoom(nxt)

    def _update_zoom_label(self):
        if self.content:
            GLib.idle_add(lambda: self.content and self.zoom_label.set_label(
                f"{round(self.content.effective_zoom() * 100)} %") and False)

    def set_position(self, text):
        self.hb.set_subtitle(f"{text}  ·  {self._base_sub}")

    # -- événements ------------------------------------------------------------
    def _on_size(self, _w, alloc):
        size = (alloc.width, alloc.height)
        if size != self._size and self.content:
            self._size = size
            self.content.on_resize()
            self._update_zoom_label()

    def _on_scroll_event(self, _w, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK and self.content and self.content.zoomable:
            ok, _dx, dy = event.get_scroll_deltas()
            if event.direction == Gdk.ScrollDirection.UP or ok and dy < 0:
                self.step_zoom(1)
            elif event.direction == Gdk.ScrollDirection.DOWN or ok and dy > 0:
                self.step_zoom(-1)
            return True
        return False

    def _on_key(self, _w, event):
        k, ctrl = event.keyval, event.state & Gdk.ModifierType.CONTROL_MASK
        if k == Gdk.KEY_Escape or ctrl and k in (Gdk.KEY_w, Gdk.KEY_W):
            self.destroy()
        elif ctrl and k in (Gdk.KEY_s, Gdk.KEY_S):
            self._on_save(self.item)
        elif k == Gdk.KEY_Left:
            self.go(-1)
        elif k == Gdk.KEY_Right:
            self.go(1)
        elif k in (Gdk.KEY_plus, Gdk.KEY_KP_Add, Gdk.KEY_equal):
            self.step_zoom(1)
        elif k in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.step_zoom(-1)
        elif k in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.set_zoom(None)
        else:
            return False
        return True

