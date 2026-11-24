"""Interface GTK 3 de QChat."""

import json
import logging
import os
import pwd
import shutil
import tempfile
import threading
import time

import cairo
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import __version__, changelog, emojis, gifdecode, gifs, netutil, session, updater  # noqa: E402
from .core import (DEFAULT_PORT, MAX_AVATAR, MAX_CEMOJI, Core, cemoji_name, is_custom,  # noqa: E402
                   mention_pattern, sniff_mime, valid_emoji)
from .reactfx import EmojiSprite, FxLayer  # noqa: E402
from .crypto import fingerprint  # noqa: E402
from .desktop import repair_user_icon_cache  # noqa: E402
from .launcher import LauncherBadge  # noqa: E402
from .notify import Notifier  # noqa: E402
from .theme import MODES, ThemeManager  # noqa: E402
from .thumbcache import ThumbCache, surface_from_pixbuf  # noqa: E402
from .viewer import AnimatedImage, Viewer, cover, fit, load_animation, load_pixbuf, viewable_kind  # noqa: E402
from .viewer import human_size as _human_size  # noqa: E402
from .wizz import WizzPopup, play_buzz, shake_widget  # noqa: E402

log = logging.getLogger("qchat")

APP_ID = "fr.qchat.QChat"


def _label(text="", css=None, xalign=0.0, wrap=False, selectable=False, markup=False):
    lbl = Gtk.Label(xalign=xalign)
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    if wrap:
        lbl.set_line_wrap(True)
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lbl.set_selectable(selectable)
    for c in (css or "").split():
        lbl.get_style_context().add_class(c)
    return lbl


def _day_label(ts):
    day = time.localtime(ts)[:3]
    if day == time.localtime()[:3]:
        return "Aujourd'hui"
    if day == time.localtime(time.time() - 86400)[:3]:
        return "Hier"
    return time.strftime("%d/%m/%Y", time.localtime(ts))


def _short_time(ts):
    day = time.localtime(ts)[:3]
    if day == time.localtime()[:3]:
        return time.strftime("%H:%M", time.localtime(ts))
    if day == time.localtime(time.time() - 86400)[:3]:
        return "Hier"
    return time.strftime("%d/%m", time.localtime(ts))


def _initials(name):
    words = [w for w in name.replace("-", " ").split() if w[0].isalnum()]
    if not words:
        return "?"
    letters = words[0][0] + (words[1][0] if len(words) > 1 else "")
    return letters.upper()


_DECODER = None


def _decoder():
    """Fils de décodage des images animées (l'interface ne les attend pas)."""
    global _DECODER
    if _DECODER is None:
        _DECODER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="qchat-decode")
    return _DECODER


def _maybe_animated(path):
    """GIF ou WebP (seuls formats animés courants), d'après les premiers octets."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return head[:6] in (b"GIF87a", b"GIF89a") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


THUMBS = None           # ThumbCache de l'application (dossier de QChat), voir QChatApp.do_startup


def _thumbs():
    global THUMBS
    if THUMBS is None:
        THUMBS = ThumbCache(os.path.join(GLib.get_user_cache_dir(), "qchat", "thumbs"), _decoder().submit)
    return THUMBS


def _make_round(path, px):
    """Surface cairo : la photo recadrée en disque de ``px`` pixels (utilisable hors de l'interface)."""
    try:
        pb = load_pixbuf(path)
    except GLib.Error:
        return None
    side = min(pb.get_width(), pb.get_height())
    pb = pb.new_subpixbuf((pb.get_width() - side) // 2, (pb.get_height() - side) // 2, side, side)
    pb = pb.scale_simple(px, px, GdkPixbuf.InterpType.HYPER)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
    cr = cairo.Context(surf)
    cr.arc(px / 2, px / 2, px / 2, 0, 2 * 3.141592653589793)
    cr.clip()
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    return surf


def _make_preview(path):
    """Surface cairo : l'aperçu 320 × 240 au plus d'une image (utilisable hors de l'interface)."""
    try:
        if os.path.getsize(path) > 50 * 1024 * 1024:     # pas d'aperçu pour les images énormes
            return None
        pb = load_pixbuf(path, 320, 240)
    except (GLib.Error, OSError):
        return None
    return surface_from_pixbuf(pb.apply_embedded_orientation() or pb)


PREVIEW = "preview-320x240"
AVATAR_SIZES = (42, 36)     # barre latérale, messages de groupe et en-tête


def _round_photo(path, size, scale):
    """Photo recadrée en disque de ``size`` px (surface cairo à l'échelle de l'écran), ou None.
    Calculée une fois par photo et par taille, puis relue depuis le cache disque : la reconstruire
    à chaque ligne coûtait ~3 ms."""
    return _thumbs().get(path, f"round-{size}-{scale}", lambda: _make_round(path, size * scale), scale)


def _warm_thumbnails(previews=(), photos=(), scale=1):
    """Prépare en arrière-plan les miniatures absentes du cache disque (aperçus, photos rondes)."""
    cache = _thumbs()

    def work():
        for p in photos:
            for size in AVATAR_SIZES:
                cache.warm(p, f"round-{size}-{scale}", lambda p=p, s=size: _make_round(p, s * scale))
        for p in previews:
            if os.path.isfile(p):
                cache.warm(p, PREVIEW, lambda p=p: _make_preview(p))
    _decoder().submit(work)


def _screen_scale():
    display = Gdk.Display.get_default()
    try:
        return max(display.get_monitor(i).get_scale_factor() for i in range(display.get_n_monitors())) or 1
    except (AttributeError, ValueError):
        return 1


def _avatar(name, small=False, online=False, peer=False, image=None, large=False):
    """Pastille ronde : photo si ``image`` (chemin) est fourni, sinon initiales ; point vert de présence."""
    size = 84 if large else 36 if small else 42
    photo = _round_photo(image, size, _screen_scale()) if image else None
    if photo is not None:
        lbl = Gtk.Image.new_from_surface(photo)
        lbl.set_size_request(size, size)
        lbl.get_style_context().add_class("avatar-photo")
    else:
        lbl = _label(_initials(name), "avatar", xalign=0.5)
    ctx = lbl.get_style_context()
    if small:
        ctx.add_class("small")
    if large:
        ctx.add_class("large")
    if peer:
        ctx.add_class("peer")
    lbl.set_valign(Gtk.Align.CENTER)
    overlay = Gtk.Overlay(valign=Gtk.Align.CENTER)
    overlay.add(lbl)
    if online:
        dot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
        if large:
            dot.set_margin_end(6)
            dot.set_margin_bottom(6)
        dot.get_style_context().add_class("presence")
        overlay.add_overlay(dot)
    return overlay


UPDATE_POLL = int(os.environ.get("QCHAT_UPDATE_POLL", 60))   # s entre deux vérifications de version
READ_COLOR = "#3d9bff"      # ✓✓ d'un message lu


def _when(ts):
    """« 16:42 » aujourd'hui, « hier 16:42 », sinon « 12/11 16:42 »."""
    if not ts:
        return ""
    t = time.localtime(ts)
    today = time.localtime()
    if t[:3] == today[:3]:
        return time.strftime("à %H:%M", t)
    if time.localtime(ts + 86400)[:3] == today[:3]:
        return time.strftime("hier à %H:%M", t)
    return time.strftime("le %d/%m à %H:%M", t)


MUTE_DURATIONS = [(3600, "Pendant 1 heure"), (4 * 3600, "Pendant 4 heures"), (12 * 3600, "Pendant 12 heures")]


def _clock(ts):
    """« 15:30 » aujourd'hui, « demain 03:30 » le lendemain."""
    t = time.localtime(ts)
    if t[:3] == time.localtime()[:3]:
        return time.strftime("%H:%M", t)
    return time.strftime("demain %H:%M" if t[:3] == time.localtime(time.time() + 86400)[:3] else "%d/%m %H:%M", t)


PAGE = 50
REACT_INSET = 12        # retrait des pastilles de réaction depuis le bord de la bulle
REACT_OVERLAP = 13      # hauteur des pastilles qui chevauche le bas de la bulle
PEEK_DELAY = 150        # ms sur une pastille avant d'afficher l'emoji en grand
PEEK_HIDE_DELAY = 120   # ms avant de le masquer (un retour rapide sur la pastille le garde)
STICKER = 96            # taille d'affichage d'un emoji personnalisé envoyé seul                   # messages construits à l'ouverture d'une conversation, puis par paquet
OLDER_MARGIN = 1500         # px du haut à partir desquels on charge les messages plus anciens
OLDER_STEP = 10             # messages construits à la fois en remontant (petits paquets : pas d'à-coup)
RUN_GAP = 10 * 60           # au-delà, un nouveau message du même auteur reprend photo et nom
PORT_WAIT = 15              # sous le service : délai d'attente d'un port encore occupé

INTERACTIONS = [("wizz", "⚡  Wizz"), ("poke", "👉  Poke")]   # menu « Interactions » de l'en-tête
WIZZ_COMMANDS = {"/wizz": "wizz", "/poke": "poke"}


def uuid_hex():
    return os.urandom(8).hex()


def _animation_from_bytes(data):
    """(animation, pixbuf fixe) décodés depuis des octets ; animation None si l'image est fixe."""
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error as e:
        anim, still = gifdecode.from_bytes(data)
        if still is None:
            return None, None
        gifdecode.fallback_used(e.message)
        return (anim, None) if anim else (None, still)
    anim = loader.get_animation()
    if anim is None:
        return None, loader.get_pixbuf()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        static = anim.is_static_image()
    return (None, anim.get_static_image()) if static else (anim, None)


def _confirm(parent, text, secondary, action_label, destructive=True):
    dlg = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                            buttons=Gtk.ButtonsType.NONE, text=text)
    dlg.format_secondary_text(secondary)
    dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)
    btn = dlg.add_button(action_label, Gtk.ResponseType.OK)
    if destructive:
        btn.get_style_context().add_class("destructive-action")
    ok = dlg.run() == Gtk.ResponseType.OK
    dlg.destroy()
    return ok


def _dialog(parent, title, ok_label=None):
    dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True, use_header_bar=True)
    dlg.set_default_size(420, -1)
    if ok_label:
        dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)
        btn = dlg.add_button(ok_label, Gtk.ResponseType.OK)
        btn.get_style_context().add_class("suggested-action")
        dlg.set_default_response(Gtk.ResponseType.OK)
    box = dlg.get_content_area()
    box.set_spacing(8)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(14)
    return dlg, box


def _scrolled(child, height):
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    sw.set_min_content_height(height)
    sw.set_shadow_type(Gtk.ShadowType.IN)
    sw.add(child)
    return sw


# ==========================================================================
# Barre latérale
# ==========================================================================
class ConvRow(Gtk.ListBoxRow):
    """Ligne façon messagerie : avatar, nom, heure, aperçu du dernier message, non-lus."""

    def __init__(self, section, key, name, subtitle, when="", online=False, unread=0, warn=False, image=None,
                 muted=False):
        super().__init__()
        self.section, self.key = section, key
        box = Gtk.Box(spacing=10, margin=7, margin_start=8, margin_end=10)
        box.pack_start(_avatar(name, online=online, peer=section == "peer", image=image), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=6)
        title = _label(name)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_markup(f"<b>{GLib.markup_escape_text(name)}</b>")
        top.pack_start(title, not muted, True, 0)
        if muted:       # 🔕 juste après le nom
            icon = Gtk.Image.new_from_icon_name("notifications-disabled-symbolic", Gtk.IconSize.MENU)
            icon.get_style_context().add_class("muted-icon")
            icon.set_tooltip_text("Conversation en sourdine")
            top.pack_start(icon, False, False, 0)
            top.pack_start(Gtk.Box(), True, True, 0)
        if when:
            top.pack_end(_label(when, "time"), False, False, 0)
        bottom = Gtk.Box(spacing=6)
        sub = _label(subtitle, "subtitle warning" if warn else "subtitle")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        bottom.pack_start(sub, True, True, 0)
        if unread:
            bottom.pack_end(_label(str(unread), "badge muted" if muted else "badge", xalign=0.5), False, False, 0)
        texts.pack_start(top, False, False, 0)
        texts.pack_start(bottom, False, False, 0)
        box.pack_start(texts, True, True, 0)
        self.add(box)


QUICK_EMOJIS = ["❤️", "👍", "👎", "😂", "😮", "😢", "🔥"]

SECTIONS = {"group": "Groupes", "contact": "Contacts", "peer": "Sur le réseau (non ajoutés)"}


# ==========================================================================
# Fenêtre principale
# ==========================================================================
class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="QChat")
        self.app = app
        self.core = app.core
        self.current = None          # conv affichée ('c:<id>' / 'g:<gid>')
        self._status_labels = {}     # msg_id -> label méta (conversation affichée)
        self._reaction_boxes = {}    # msg_id -> boîte des réactions (conversation affichée)
        self._bubbles = {}           # msg_id -> bulle (point de départ de l'animation des réactions)
        self._emoji_arts = {}        # jeton d'emoji personnalisé -> image réduite
        self._emoji_pickers = []     # sélecteurs d'emoji à rafraîchir quand mes emojis changent
        self._peek = None            # bulle d'aperçu d'une réaction survolée
        self._peek_button = None     # pastille à laquelle elle est rattachée
        self._peek_timers = {}       # "show" / "hide" -> minuteur en attente
        self._text_labels = {}       # msg_id -> (texte, méta) des bulles affichées
        self._editing = None         # msg_id du message en cours de modification
        self._replying = None        # msg_id du message auquel on répond
        self._rows = {}              # msg_id -> ligne de la liste (conversation affichée)
        self._run_first = {}         # msg_id -> premier d'une suite de messages du même auteur
        self._prev_msg = None        # dernier message affiché (regroupement par auteur)
        self._replies = {}           # msg_id -> ids des messages affichés qui le citent
        self._sel_label = None       # dernier texte de bulle cliqué (pour Ctrl+C)
        self._react_target = None    # message visé par le sélecteur d'emoji des réactions
        self._viewer = None          # visionneuse de pièces jointes ouverte
        self._last_day = None
        self._stick_bottom = True
        self._older = []            # messages plus anciens pas encore affichés (chargés en remontant)
        self._older_pending = False
        self._anchor = None         # (hauteur, position) avant un ajout en haut, pour ne pas sauter
        self._top = None            # plus ancien message affiché
        self.set_default_size(980, 640)
        self.set_icon_name("qchat")
        self._build()
        self.connect("delete-event", self._on_close)
        self.connect("key-press-event", self._on_window_key)
        self.connect("notify::is-active", self._on_active_changed)
        self.refresh_sidebar()

    # -- construction -------------------------------------------------------
    def _build(self):
        self.get_style_context().add_class("qchat")
        hb = Gtk.HeaderBar(show_close_button=True)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title_label = _label("", xalign=0.5)
        self.subtitle_label = _label("", "subtitle", xalign=0.5)
        titles.pack_start(self.title_label, False, False, 0)
        titles.pack_start(self.subtitle_label, False, False, 0)
        hb.set_custom_title(titles)
        self.header = hb
        self.set_titlebar(hb)

        add_menu = Gio.Menu()
        add_menu.append("Ajouter un contact par IP…", "app.add-contact")
        add_menu.append("Nouveau groupe…", "app.new-group")
        add_menu.append("Découvrir le réseau…", "app.scan")
        add_btn = Gtk.MenuButton(menu_model=add_menu, tooltip_text="Ajouter")
        add_btn.add(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_start(add_btn)
        scan_btn = Gtk.Button.new_from_icon_name("system-search-symbolic", Gtk.IconSize.BUTTON)
        scan_btn.set_tooltip_text("Découvrir les postes QChat du réseau (Ctrl+D)")
        scan_btn.set_action_name("app.scan")
        hb.pack_start(scan_btn)

        main_menu = Gio.Menu()
        main_menu.append("Mon identité…", "app.identity")
        theme_menu = Gio.Menu()
        for mode, label in MODES.items():
            theme_menu.append(label, f"app.theme::{mode}")
        main_menu.append_submenu("Thème", theme_menu)
        main_menu.append("Convertir les smileys :) en 🙂", "app.smileys")
        main_menu.append("Nouveautés", "app.whats-new")
        main_menu.append("Rechercher des mises à jour…", "app.check-updates")
        main_menu.append("Redémarrer", "app.restart")
        main_menu.append("Quitter", "app.quit")
        self.main_menu = main_menu
        self.menu_btn = menu_btn = Gtk.MenuButton(menu_model=main_menu)
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(menu_btn)
        if self.app._update_version:
            self.set_update_ready(self.app._update_version)

        paned = Gtk.Paned(position=310)
        self.add(paned)

        # Barre latérale
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar.set_sort_func(lambda a, b: a.order - b.order)
        self._side_rows = {}         # clé -> ligne de la barre latérale (réutilisée si inchangée)
        self.sidebar.connect("row-activated", self._on_row_activated)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroll.get_style_context().add_class("sidebar")
        side_scroll.add(self.sidebar)
        paned.pack1(side_scroll, resize=False, shrink=False)

        # Zone de discussion
        self.stack = Gtk.Stack()
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=10)
        empty.get_style_context().add_class("empty-page")
        logo = Gtk.Image.new_from_icon_name("qchat", Gtk.IconSize.DIALOG)
        logo.set_pixel_size(96)
        empty.pack_start(logo, False, False, 0)
        empty.pack_start(_label("Sélectionnez une conversation", "empty-title", xalign=0.5), False, False, 0)
        empty.pack_start(_label("Cliquez sur 🔍 pour découvrir les collègues du réseau,\n"
                                "ou ajoutez-en un par son adresse IP avec le bouton +.",
                                "subtitle", xalign=0.5), False, False, 0)
        empty_page = Gtk.EventBox()
        empty_page.get_style_context().add_class("empty-page")
        empty_page.add(empty)
        self.stack.add_named(empty_page, "empty")

        chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=10, margin=0)
        top.get_style_context().add_class("chat-top")
        self.chat_avatar = Gtk.Box(margin_start=14, margin_top=8, margin_bottom=8)
        top.pack_start(self.chat_avatar, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.chat_title = _label("", "chat-title")
        self.chat_sub = _label("", "subtitle")
        self.chat_sub.set_ellipsize(Pango.EllipsizeMode.END)
        titles.pack_start(self.chat_title, False, False, 0)
        titles.pack_start(self.chat_sub, False, False, 0)
        top.pack_start(titles, True, True, 0)
        interactions = Gio.Menu()
        for kind, label in INTERACTIONS:
            interactions.append(label, f"win.interaction::{kind}")
        action = Gio.SimpleAction.new("interaction", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, v: self._send_wizz(v.get_string()))
        self.add_action(action)
        self.interactions_btn = Gtk.MenuButton(menu_model=interactions, valign=Gtk.Align.CENTER,
                                               tooltip_text="Wizz, poke…")
        ibox = Gtk.Box(spacing=4)
        ibox.pack_start(Gtk.Label(label="⚡"), False, False, 0)
        ibox.pack_start(Gtk.Label(label="Interactions"), False, False, 0)
        ibox.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON),
                        False, False, 0)
        self.interactions_btn.add(ibox)
        ibox.show_all()
        self.interactions_btn.get_style_context().add_class("interactions-btn")
        self.interactions_btn.set_no_show_all(True)
        top.pack_start(self.interactions_btn, False, False, 0)
        mute_action = Gio.SimpleAction.new("mute", GLib.VariantType.new("s"))
        mute_action.connect("activate", lambda _a, v: self._set_mute(int(v.get_string())))
        self.add_action(mute_action)
        self.mute_btn = Gtk.MenuButton(valign=Gtk.Align.CENTER)
        self.mute_icon = Gtk.Image.new_from_icon_name("preferences-system-notifications-symbolic",
                                                      Gtk.IconSize.BUTTON)
        self.mute_btn.add(self.mute_icon)
        self.mute_btn.get_style_context().add_class("round")
        top.pack_start(self.mute_btn, False, False, 0)
        self._mute_timer = None
        info_btn = Gtk.Button.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON)
        info_btn.set_tooltip_text("Détails")
        info_btn.set_valign(Gtk.Align.CENTER)
        info_btn.set_margin_end(10)
        info_btn.get_style_context().add_class("round")
        info_btn.connect("clicked", lambda _b: self.show_details())
        top.pack_end(info_btn, False, False, 0)
        chat.pack_start(top, False, False, 0)

        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, no_show_all=True)
        self.infobar_label = _label("", wrap=True)
        self.infobar.get_content_area().add(self.infobar_label)
        self.infobar.add_button("Accepter la nouvelle clé", 1)
        self.infobar.add_button("C'est quelqu'un d'autre", 2)
        self.infobar.connect("response", self._on_infobar)
        chat.pack_start(self.infobar, False, False, 0)

        self.msg_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.msg_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.msg_scroll.get_style_context().add_class("chat-area")
        self.msg_scroll.add(self.msg_list)
        vadj = self.msg_scroll.get_vadjustment()
        vadj.connect("changed", self._on_adj_changed)
        vadj.connect("value-changed", self._on_adj_value)
        # Calque des animations de réaction, posé sur la liste des messages.
        self.fx = FxLayer()
        msg_overlay = Gtk.Overlay()
        msg_overlay.add(self.msg_scroll)
        msg_overlay.add_overlay(self.fx)
        msg_overlay.set_overlay_pass_through(self.fx, True)
        chat.pack_start(msg_overlay, True, True, 0)

        # Barre de saisie flottante : 📎 | texte | envoyer
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        area.get_style_context().add_class("composer-area")
        self.edit_bar = Gtk.Box(spacing=6, no_show_all=True)
        self.edit_bar.get_style_context().add_class("edit-bar")
        edit_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        edit_texts.pack_start(_label("✏ Modification du message", "edit-title"), False, False, 0)
        self.edit_orig = _label("", "subtitle")
        self.edit_orig.set_ellipsize(Pango.EllipsizeMode.END)
        edit_texts.pack_start(self.edit_orig, False, False, 0)
        self.edit_bar.pack_start(edit_texts, True, True, 0)
        edit_cancel = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        edit_cancel.set_tooltip_text("Annuler la modification (Échap)")
        edit_cancel.set_valign(Gtk.Align.CENTER)
        edit_cancel.get_style_context().add_class("round")
        edit_cancel.connect("clicked", lambda _b: self._cancel_edit())
        self.edit_bar.pack_start(edit_cancel, False, False, 0)
        area.pack_start(self.edit_bar, False, False, 0)
        self.reply_bar = Gtk.Box(spacing=6, no_show_all=True)
        self.reply_bar.get_style_context().add_class("edit-bar")
        reply_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.reply_title = _label("", "edit-title")
        reply_texts.pack_start(self.reply_title, False, False, 0)
        self.reply_orig = _label("", "subtitle")
        self.reply_orig.set_ellipsize(Pango.EllipsizeMode.END)
        reply_texts.pack_start(self.reply_orig, False, False, 0)
        self.reply_bar.pack_start(reply_texts, True, True, 0)
        reply_cancel = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        reply_cancel.set_tooltip_text("Annuler la réponse (Échap)")
        reply_cancel.set_valign(Gtk.Align.CENTER)
        reply_cancel.get_style_context().add_class("round")
        reply_cancel.connect("clicked", lambda _b: self._cancel_reply())
        self.reply_bar.pack_start(reply_cancel, False, False, 0)
        area.pack_start(self.reply_bar, False, False, 0)
        composer = Gtk.Box(spacing=2)
        composer.get_style_context().add_class("composer")
        attach = Gtk.Button.new_from_icon_name("mail-attachment-symbolic", Gtk.IconSize.BUTTON)
        attach.set_tooltip_text("Joindre des fichiers ou des images\n"
                                "(ou glisser-déposer, ou Ctrl+V pour coller une capture d'écran)")
        attach.set_valign(Gtk.Align.END)
        attach.get_style_context().add_class("round")
        attach.connect("clicked", lambda _b: self._pick_files())
        composer.pack_start(attach, False, False, 0)
        self.gif_btn = Gtk.MenuButton(label="GIF", tooltip_text="Envoyer un GIF")
        self.gif_btn.set_valign(Gtk.Align.END)
        for c in ("round", "gif-btn"):
            self.gif_btn.get_style_context().add_class(c)
        self.gif_popover = GifPopover(self)
        self.gif_btn.set_popover(self.gif_popover)
        composer.pack_start(self.gif_btn, False, False, 0)
        self.emoji_btn = Gtk.MenuButton(tooltip_text="Insérer un emoji")
        self.emoji_btn.add(Gtk.Image.new_from_icon_name("face-smile-symbolic", Gtk.IconSize.BUTTON))
        self.emoji_btn.set_valign(Gtk.Align.END)
        self.emoji_btn.get_style_context().add_class("round")
        self.emoji_popover = EmojiPopover(self)
        # Sélecteurs d'emoji (saisie et réactions) préparés en tâche de fond après le démarrage.
        GLib.timeout_add_seconds(2, lambda: (self.emoji_popover.prebuild(), self._react_picker_widget().prebuild())
                                 and False)
        self.emoji_btn.set_popover(self.emoji_popover)
        composer.pack_start(self.emoji_btn, False, False, 0)
        self.input = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.input.set_left_margin(4)
        self.input.set_right_margin(4)
        self.input.set_top_margin(8)
        self.input.set_bottom_margin(8)
        self.input.connect("key-press-event", self._on_input_key)
        in_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        in_scroll.set_propagate_natural_height(True)
        in_scroll.set_max_content_height(140)
        in_scroll.add(self.input)
        in_scroll.set_valign(Gtk.Align.CENTER)    # une seule ligne : centrée entre 📎 et envoyer
        placeholder = _label("Écrire un message…", "placeholder")
        placeholder.set_margin_start(5)
        placeholder.set_halign(Gtk.Align.START)
        placeholder.set_valign(Gtk.Align.CENTER)
        in_overlay = Gtk.Overlay()
        in_overlay.add(in_scroll)
        in_overlay.add_overlay(placeholder)
        in_overlay.set_overlay_pass_through(placeholder, True)
        buf = self.input.get_buffer()
        buf.connect("changed", lambda b: placeholder.set_visible(b.get_char_count() == 0))
        self.mention_popover = MentionPopover(self)
        buf.connect("changed", lambda _b: GLib.idle_add(self.mention_popover.update))
        buf.connect("notify::cursor-position", lambda *_a: self.mention_popover.update())
        self.emoji_completion = EmojiCompletion(self)
        buf.connect("changed", lambda _b: GLib.idle_add(self.emoji_completion.update, True,
                                                        priority=GLib.PRIORITY_HIGH))
        buf.connect("notify::cursor-position", lambda *_a: self.emoji_completion.update())
        buf.connect_after("insert-text", self._on_input_insert)
        self._auto_busy = False     # remplacement automatique en cours (ne pas réagir à nos propres modifs)
        self._last_auto = None      # dernier remplacement automatique, annulable par Retour arrière
        self._kept_smiley = None    # smiley gardé tel quel (Retour arrière) : pas converti à l'envoi
        composer.pack_start(in_overlay, True, True, 0)
        send = Gtk.Button.new_from_icon_name("document-send-symbolic", Gtk.IconSize.BUTTON)
        send.set_tooltip_text("Envoyer (Entrée) — Maj+Entrée pour un retour à la ligne")
        send.get_style_context().add_class("send-btn")
        send.set_valign(Gtk.Align.END)
        send.connect("clicked", lambda _b: self._send())
        composer.pack_start(send, False, False, 0)
        area.pack_start(composer, True, True, 0)
        chat.pack_start(area, False, False, 0)
        self.stack.add_named(chat, "chat")
        # Glisser-déposer de fichiers n'importe où dans la fenêtre, avec un bandeau pendant le survol.
        overlay = Gtk.Overlay()
        overlay.add(self.stack)
        self.drop_hint = _label("", "drop-hint", xalign=0.5)
        self.drop_hint.set_halign(Gtk.Align.CENTER)
        self.drop_hint.set_valign(Gtk.Align.CENTER)
        self.drop_hint.set_no_show_all(True)
        overlay.add_overlay(self.drop_hint)
        overlay.set_overlay_pass_through(self.drop_hint, True)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)
        # Zone de saisie : un fichier déposé est envoyé (au lieu d'insérer son chemin) ;
        # le texte glissé reste inséré normalement.
        targets = Gtk.TargetList.new([])
        targets.add_uri_targets(1)
        targets.add_text_targets(2)
        self.input.drag_dest_set_target_list(targets)
        self.input.connect("drag-data-received", self._on_input_drop)
        for widget in (self, self.input):
            widget.connect("drag-motion", self._on_drag_motion)
            widget.connect("drag-leave", lambda *_a: self.drop_hint.hide())

        paned.pack2(overlay, resize=True, shrink=False)
        self.show_all()

    # -- barre latérale -----------------------------------------------------
    def _sidebar_header(self, row, before):
        if before is None or before.section != row.section:
            row.set_header(_label(SECTIONS[row.section], "section"))
        else:
            row.set_header(None)

    def _preview(self, m):
        """Aperçu d'un message pour la barre latérale."""
        text = " ".join((f"📎 {m.text}" if m.kind == "file" else m.text).split())
        if m.deleted is not None:
            text = "Message supprimé"
        if m.kind == "system":
            return text
        if m.outgoing:
            return f"Vous : {text}"
        if m.conv.startswith("g:"):
            return f"{self._sender_name(m.sender_id).split()[0]} : {text}"
        return text

    def _contact_subtitle(self, c, last):
        if c.pending_vk:
            return "⚠ Clé de sécurité modifiée"
        if last:
            return self._preview(last)
        ips = ", ".join(self.core.store.ips(c.id)) or "aucune IP"
        return f"Jamais joint — {ips}" if not c.vk else ips

    def refresh_sidebar(self):
        """Met à jour la barre latérale. Seules les lignes dont le contenu a changé sont reconstruites
        (elle est rafraîchie à chaque message, présence ou réaction)."""
        store = self.core.store
        entries = []        # (clé, arguments de ConvRow) dans l'ordre d'affichage
        last = {}

        def last_msg(conv):
            if conv not in last:
                last[conv] = store.last_message(conv)
            return last[conv]

        def last_ts(conv):
            m = last_msg(conv)
            return m.ts if m else 0

        total_unread = 0
        badge = 0       # non lus hors conversations en sourdine : compteur de l'icône du dock
        for g in sorted(store.groups(), key=lambda g: -last_ts(g.conv)):
            unread = store.unread_count(g.conv)
            total_unread += unread
            muted = bool(self.core.muted_until(g.conv))
            badge += 0 if muted else unread
            m = last_msg(g.conv)
            n = len(store.group_member_ids(g.gid)) + 1
            entries.append(("group", g.conv, g.name, self._preview(m) if m else f"{n} membres",
                            dict(when=_short_time(m.ts) if m else "", unread=unread,
                                 image=self.core.avatar_path(g.conv), muted=muted)))
        for c in sorted(store.contacts(), key=lambda c: (-last_ts(c.conv), c.name.lower())):
            unread = store.unread_count(c.conv)
            total_unread += unread
            muted = bool(self.core.muted_until(c.conv))
            badge += 0 if muted else unread
            m = last_msg(c.conv)
            entries.append(("contact", c.conv, c.name, self._contact_subtitle(c, m),
                            dict(when=_short_time(m.ts) if m else "", online=self.core.is_online(c.id),
                                 unread=unread, warn=bool(c.pending_vk),
                                 image=self.core.avatar_path(c.conv), muted=muted)))
        for vk, peer in sorted(self.core.discovered.items(), key=lambda kv: kv[1]["name"].lower()):
            entries.append(("peer", vk, peer["name"], ", ".join(peer["ips"]), dict(online=True)))
        self._sync_sidebar(entries)
        accent = self.app.theme.palette["accent"]
        count = f" ({total_unread})" if total_unread else ""
        self.title_label.set_markup(f"<b><span foreground='{accent}'>Q</span>Chat{count}</b>")
        self.subtitle_label.set_text(self.core.name)
        self.set_title(f"QChat{count}")
        self.app.badge.set_count(badge)
        self._refresh_chat_header()
        self._schedule_mute_expiry()

    def _sync_sidebar(self, entries):
        old = self._side_rows
        rows, reorder = {}, False
        for order, (section, key, name, subtitle, kw) in enumerate(entries):
            sig = (section, name, subtitle, tuple(sorted(kw.items())))
            row = old.pop(key, None)
            if row is not None and row.sig != sig:
                row.destroy()
                row = None
            if row is None:
                row = ConvRow(section, key, name, subtitle, **kw)
                row.sig, row.order = sig, order
                row.show_all()
                self.sidebar.add(row)
            elif row.order != order:
                row.order = order
                reorder = True
            rows[key] = row
        for row in old.values():
            row.destroy()
        self._side_rows = rows
        if reorder:
            self.sidebar.invalidate_sort()
        self.sidebar.invalidate_headers()
        current = rows.get(self.current)
        if current is not None:
            if self.sidebar.get_selected_row() is not current:
                self.sidebar.select_row(current)
        else:
            self.sidebar.unselect_all()

    def _on_row_activated(self, _lb, row):
        if row.section == "peer":
            self._add_discovered(row.key)
        else:
            self.open_conv(row.key)

    def _add_discovered(self, vk):
        peer = self.core.discovered.get(vk)
        if not peer:
            return
        if _confirm(self, f"Ajouter {peer['name']} à vos contacts ?",
                    f"Adresses : {', '.join(peer['ips'])}\nEmpreinte : {fingerprint(vk)}",
                    "Ajouter", destructive=False):
            cid = self.core.add_discovered(vk)
            if cid:
                self.open_conv(f"c:{cid}")

    # -- conversation -------------------------------------------------------
    def _conv_exists(self, conv):
        kind, ident = conv.split(":", 1)
        if kind == "g":
            return self.core.store.get_group(ident) is not None
        return self.core.store.get_contact(int(ident)) is not None

    def open_conv(self, conv):
        if not self._conv_exists(conv):
            return
        self.current = conv
        self._status_labels.clear()
        self._reaction_boxes.clear()
        self._bubbles.clear()
        self._text_labels.clear()
        self._rows.clear()
        self._run_first.clear()
        self._prev_msg = None
        self._replies.clear()
        self._sel_label = None
        self._cancel_edit()
        self._cancel_reply()
        self._last_day = None
        self._anchor = None
        for row in self.msg_list.get_children():
            self.msg_list.remove(row)
        # Seuls les derniers messages sont construits à l'ouverture (rapide même pour un long
        # historique) ; les plus anciens arrivent par paquets en remontant.
        history = self.core.store.messages(conv)
        self._older = history[:-PAGE] if len(history) > PAGE else []
        self._top = history[-PAGE] if len(history) >= PAGE else (history[0] if history else None)
        for m in history[-PAGE:]:
            self._append_message(m)
        if self._older:     # aperçus de l'historique préparés d'avance, pour remonter sans à-coup
            _warm_thumbnails(previews=self.core.store.image_paths(conv))
        self._stick_bottom = True
        self.core.mark_read(conv)
        self.app.notifier.withdraw(conv)
        self.stack.set_visible_child_name("chat")
        self.refresh_sidebar()
        self.input.grab_focus()

    def _refresh_chat_header(self):
        if not self.current:
            return
        if not self._conv_exists(self.current):
            self.current = None
            self.stack.set_visible_child_name("empty")
            return
        kind, ident = self.current.split(":", 1)
        self.infobar.hide()
        self._refresh_mute_button()
        for child in self.chat_avatar.get_children():
            self.chat_avatar.remove(child)
        if kind == "g":
            g = self.core.store.get_group(ident)
            names = [self.core.store.get_contact(i).name for i in self.core.store.group_member_ids(ident)]
            self.chat_avatar.add(_avatar(g.name, small=True, image=self.core.avatar_path(g.conv)))
            self.chat_avatar.show_all()
            self.chat_title.set_text(g.name)
            self.interactions_btn.hide()
            self.chat_sub.set_text(f"{len(names) + 1} membres · Vous, "
                                   + ", ".join(sorted(names, key=str.lower)))
        else:
            c = self.core.store.get_contact(int(ident))
            online = self.core.is_online(c.id)
            self.chat_avatar.add(_avatar(c.name, small=True, online=online,
                                         image=self.core.avatar_path(c.conv)))
            self.chat_avatar.show_all()
            self.chat_title.set_text(c.name)
            self.interactions_btn.show()
            if not c.vk:
                state = "jamais joint — les messages partiront dès qu'il sera joignable"
            elif online:
                state = f"<span foreground='{self.app.theme.palette['accent']}'>en ligne</span>"
            else:
                state = "hors ligne"
            peer_until = self.core.peer_muted_until(c.id, c.conv)
            if peer_until:
                state += GLib.markup_escape_text(
                    f"  ·  🔕 a mis la conversation en sourdine jusqu'à {_clock(peer_until)}")
            self.chat_sub.set_markup(state)
            if c.pending_vk:
                self.infobar_label.set_text(
                    f"Une clé de sécurité différente a répondu à l'adresse {c.pending_ip} de {c.name}. "
                    "C'est normal si cette personne a réinstallé QChat ; sinon il peut s'agir d'un autre "
                    "poste ayant récupéré l'IP, ou d'une usurpation. Vérifiez l'empreinte avec elle.\n"
                    f"Nouvelle empreinte : {fingerprint(c.pending_vk)}")
                self.infobar.show_all()

    # -- sourdine -------------------------------------------------------------------
    def _refresh_mute_button(self):
        until = self.core.muted_until(self.current) if self.current else 0
        menu = Gio.Menu()
        durations = Gio.Menu()
        for seconds, label in MUTE_DURATIONS:
            durations.append(label, f"win.mute::{seconds}")
        menu.append_section("Mettre en sourdine" if not until else "Prolonger la sourdine", durations)
        if until:
            off = Gio.Menu()
            off.append("Réactiver les notifications", "win.mute::0")
            menu.append_section(None, off)
        self.mute_btn.set_menu_model(menu)
        if until:
            self.mute_icon.set_from_icon_name("notifications-disabled-symbolic", Gtk.IconSize.BUTTON)
            self.mute_btn.set_tooltip_text(f"En sourdine jusqu'à {_clock(until)} : aucune notification")
            self.mute_btn.get_style_context().add_class("muted")
        else:
            self.mute_icon.set_from_icon_name("preferences-system-notifications-symbolic", Gtk.IconSize.BUTTON)
            self.mute_btn.set_tooltip_text("Mettre la conversation en sourdine")
            self.mute_btn.get_style_context().remove_class("muted")

    def _set_mute(self, seconds):
        if self.current:
            self.core.set_mute(self.current, seconds)

    def _schedule_mute_expiry(self):
        """Rafraîchit l'affichage quand la prochaine sourdine (la mienne ou celle d'en face) se termine."""
        now = time.time()
        ends = [self.core.muted_until(r.key) for r in self.sidebar.get_children() if r.section != "peer"]
        if self.current and self.current.startswith("c:"):
            ends.append(self.core.peer_muted_until(int(self.current[2:]), self.current))
        ends = [e for e in ends if e > now]
        if self._mute_timer:
            GLib.source_remove(self._mute_timer)
            self._mute_timer = None
        if ends:
            delay = int(min(ends) - now) + 1
            self._mute_timer = GLib.timeout_add_seconds(delay, self._mute_expired)

    def _mute_expired(self):
        self._mute_timer = None
        self.refresh_sidebar()
        return False

    def _sender_name(self, sender_id):
        c = self.core.store.get_contact(sender_id) if sender_id is not None else None
        return c.name if c else "?"

    def _append_message(self, m, position=-1):
        """Ajoute ``m`` en bas de la conversation, ou à ``position`` (messages plus anciens) ;
        renvoie la position suivante."""
        day = _day_label(m.ts)
        if day != self._last_day:
            self._last_day = day
            self._prev_msg = None
            self._add_row(_label(day, "day", xalign=0.5), Gtk.Align.CENTER, position)
            position = position + 1 if position >= 0 else position
        if m.kind == "system":
            self._prev_msg = None
            note = _label(m.text, "system", xalign=0.5, wrap=True)
            note.set_max_width_chars(70)
            note.set_justify(Gtk.Justification.CENTER)
            self._add_row(note, Gtk.Align.CENTER, position)
            return position + 1 if position >= 0 else position
        prev = self._prev_msg
        first = not (prev and prev.sender_id == m.sender_id and m.ts - prev.ts < RUN_GAP)
        self._run_first[m.id] = first
        self._prev_msg = m
        self._rows[m.id] = self._add_row(*self._message_widget(m), position)
        return position + 1 if position >= 0 else position

    def _load_older(self):
        """Construit le paquet de messages précédant ceux affichés, en haut de la liste, sans
        faire bouger ce que l'on regarde."""
        self._older_pending = False
        if not self._older:
            return False
        chunk, self._older = self._older[-OLDER_STEP:], self._older[:-OLDER_STEP]
        first_shown = self._top
        self._top = chunk[0]
        adj = self.msg_scroll.get_vadjustment()
        self._anchor = (adj.get_upper(), adj.get_value())
        bottom_state = (self._last_day, self._prev_msg)
        self._last_day, self._prev_msg = None, None
        position = 0
        for m in chunk:
            position = self._append_message(m, position)
        last_old, last_day = self._prev_msg, self._last_day
        self._last_day, self._prev_msg = bottom_state
        if first_shown is not None:
            # Même jour des deux côtés : un seul intitulé de jour.
            header = self.msg_list.get_row_at_index(position)
            if header is not None and last_day == _day_label(first_shown.ts):
                header.destroy()
            # Suite de messages du même auteur à cheval sur les deux paquets : nom et photo une seule fois.
            if (last_old and last_day == _day_label(first_shown.ts) and first_shown.kind != "system"
                    and last_old.sender_id == first_shown.sender_id and first_shown.ts - last_old.ts < RUN_GAP
                    and self._run_first.get(first_shown.id)):
                self._run_first[first_shown.id] = False
                self._rerender(first_shown.id)
        return False

    def _message_widget(self, m):
        """Bulle complète d'un message (citation, contenu, heure, réactions) et son alignement."""
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        ctx = bubble.get_style_context()
        ctx.add_class("bubble")
        ctx.add_class("out" if m.outgoing else "in")
        halign = Gtk.Align.END if m.outgoing else Gtk.Align.START
        in_group = not m.outgoing and m.conv.startswith("g:")
        first = self._run_first.get(m.id, True)
        if in_group and first:
            bubble.pack_start(_label(self._sender_name(m.sender_id), "sender"), False, False, 0)
        if m.deleted is not None:
            ctx.add_class("deleted")
            bubble.pack_start(_label("🚫  Message supprimé par son auteur", "deleted-text"), False, False, 0)
            meta = _label(time.strftime("%H:%M", time.localtime(m.ts)), "meta", xalign=1.0)
            bubble.pack_start(meta, False, False, 0)
            return (self._with_avatar(m, bubble, first) if in_group else bubble), halign
        if m.reply_to:
            bubble.pack_start(self._quote_widget(m.reply_to, m.conv), False, False, 0)
            self._replies.setdefault(m.reply_to, set()).add(m.id)
        sticker = m.sticker and self.core.custom_emoji(m.sticker)
        if m.kind == "file":
            bubble.pack_start(self._attachment_widget(m), False, False, 0)
        elif sticker:
            ctx.add_class("jumbo-bubble")
            bubble.pack_start(self._sticker_widget(m.sticker, *sticker[:2]), False, False, 0)
        else:
            text = _label("", wrap=True, selectable=True)
            self._set_message_text(text, bubble, m)
            text.set_max_width_chars(60)
            text.set_can_focus(False)
            text.connect("button-press-event", lambda w, _e: setattr(self, "_sel_label", w) and False)
            bubble.pack_start(text, False, False, 0)
        meta = _label("", "meta", xalign=1.0)
        bubble.meta = meta
        meta.set_track_visited_links(False)
        meta.connect("activate-link", lambda _l, uri, mid=m.id: (
            self._show_history(mid) if uri == "history" else self._show_receipts(mid)) or True)
        self._set_meta(meta, m)
        bubble.pack_start(meta, False, False, 0)
        if m.outgoing:
            self._status_labels[m.id] = meta
        if m.kind == "text" and not sticker:
            self._text_labels[m.id] = (text, meta)
        widget = self._with_reactions(m, bubble)
        return (self._with_avatar(m, widget, first) if in_group else widget), halign

    def _with_avatar(self, m, widget, first):
        """Groupe : photo de l'auteur à gauche de son message (une fois par suite de messages)."""
        line = Gtk.Box(spacing=8)
        if first:
            c = self.core.store.get_contact(m.sender_id) if m.sender_id is not None else None
            name = c.name if c else "?"
            av = _avatar(name, small=True, image=self.core.avatar_path(c.conv) if c else None)
            av.set_valign(Gtk.Align.START)
            av.set_tooltip_text(name)
            av.get_style_context().add_class("msg-avatar")
        else:
            av = Gtk.Box()
        av.set_size_request(36, -1)
        line.pack_start(av, False, False, 0)
        line.pack_start(widget, False, False, 0)
        return line

    def _mention_names(self, m):
        """Noms affichables des personnes mentionnées dans ``m`` (prénom compris)."""
        me = self.core.identity.vk_hex
        names = []
        for vk in m.mentions:
            if vk == me:
                name = self.core.name
            else:
                c = self.core.store.contact_by_vk(vk)
                name = c.name if c else None
            if name:
                names.append(name)
                if name.split():
                    names.append(name.split()[0])
        return names

    def _set_message_text(self, label, bubble, m):
        """Texte d'une bulle : emoji seuls affichés en grand, mentions @Nom mises en évidence."""
        ctx, bctx = label.get_style_context(), bubble.get_style_context()
        for c in ("jumbo", "jumbo-xl"):
            ctx.remove_class(c)
        bctx.remove_class("jumbo-bubble")
        count = emojis.emoji_only(m.text)
        if count:
            ctx.add_class("jumbo-xl" if count <= 3 else "jumbo")
            bctx.add_class("jumbo-bubble")
            label.set_text(m.text)
            return
        me = self.core.identity.vk_hex
        if me in m.mentions:
            bctx.add_class("mentioned")
        names = sorted(set(self._mention_names(m)), key=len, reverse=True)
        if not names:
            label.set_text(m.text)
            return
        spans = []
        for name in names:
            for match in mention_pattern(name).finditer(m.text):
                if not any(a < match.end() and match.start() < b for a, b in spans):
                    spans.append((match.start(), match.end()))
        if not spans:
            label.set_text(m.text)
            return
        color = self.app.theme.palette["accent"]
        out, pos = [], 0
        for a, b in sorted(spans):
            out.append(GLib.markup_escape_text(m.text[pos:a]))
            out.append(f"<span foreground='{color}' weight='bold'>{GLib.markup_escape_text(m.text[a:b])}</span>")
            pos = b
        out.append(GLib.markup_escape_text(m.text[pos:]))
        label.set_markup("".join(out))

    def _rerender(self, msg_id):
        """Reconstruit la bulle d'un message affiché (suppression, citation modifiée…)."""
        row = self._rows.get(msg_id)
        m = self.core.store.get_message(msg_id)
        if row is None or m is None or row.get_parent() is None:
            return
        index = row.get_index()
        for table in (self._status_labels, self._reaction_boxes, self._text_labels, self._bubbles):
            table.pop(msg_id, None)
        row.destroy()
        self._rows[msg_id] = self._add_row(*self._message_widget(m), position=index)

    def _excerpt(self, m):
        if m.deleted is not None:
            return "Message supprimé"
        if m.kind == "file":
            att = self.core.store.get_attachment(m.id)
            return "🖼 Image" if att and att.is_image else f"📎 {m.text}"
        return " ".join(m.text.split())

    def _quote_widget(self, ref_id, conv):
        """Rappel du message auquel on répond ; un clic fait défiler jusqu'à lui."""
        ref = self.core.store.get_message(ref_id)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.get_style_context().add_class("quote")
        if ref is None or ref.conv != conv:
            box.pack_start(_label("Message d'origine introuvable", "quote-text"), False, False, 0)
            return box
        who = "Vous" if ref.outgoing else self._sender_name(ref.sender_id)
        box.pack_start(_label(who, "quote-name"), False, False, 0)
        text = _label(self._excerpt(ref), "quote-text")
        text.set_ellipsize(Pango.EllipsizeMode.END)
        text.set_max_width_chars(50)
        text.set_lines(2)
        text.set_line_wrap(True)
        box.pack_start(text, False, False, 0)
        ev = Gtk.EventBox(tooltip_text="Afficher le message d'origine")
        ev.add(box)
        ev.connect("button-press-event", lambda _w, e: e.button == 1 and self._scroll_to(ref_id) or True)
        ev.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
        return ev

    def _scroll_to(self, msg_id):
        row = self._rows.get(msg_id)
        if row is None and any(m.id == msg_id for m in self._older):
            while msg_id not in self._rows and self._older:     # message plus ancien : on le charge
                self._load_older()
            GLib.timeout_add(80, lambda: self._scroll_to(msg_id) and False)
            return
        if row is None or row.get_parent() is None:
            return
        self._stick_bottom = False
        adj = self.msg_scroll.get_vadjustment()
        alloc = row.get_allocation()
        adj.set_value(max(0, alloc.y - (adj.get_page_size() - alloc.height) / 2))
        child = row.get_child()
        child.get_style_context().add_class("flash")
        GLib.timeout_add(1600, lambda: child.get_style_context().remove_class("flash") or False)

    # -- réactions -------------------------------------------------------------
    def _with_reactions(self, m, bubble):
        """Bulle + pastilles de réactions + bouton 😊 visible au survol (ou clic droit)."""
        # Les pastilles chevauchent le bas de la bulle (côté gauche, l'heure restant visible à droite).
        # Sans réaction, pas de superposition (créée à la première réaction, voir on_reaction).
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bubble.set_halign(Gtk.Align.END if m.outgoing else Gtk.Align.START)
        column.pack_start(bubble, False, False, 0)
        self._bubbles[m.id] = bubble
        if self.core.store.reactions(m.id):
            spacer = Gtk.Box()
            spacer.set_no_show_all(True)
            column.pack_start(spacer, False, False, 0)
            reacts = Gtk.Box(spacing=3, halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=REACT_INSET)
            reacts.set_no_show_all(True)
            reacts.spacer = spacer
            holder = Gtk.Overlay()
            holder.add(column)
            holder.add_overlay(reacts)
            self._reaction_boxes[m.id] = reacts
            self._fill_reactions(m.id, reacts)
        else:
            holder = column
            self._reaction_boxes[m.id] = None

        # Boutons du survol (😊, ↩, …) : construits au premier survol seulement. Ils apparaissent du
        # côté extérieur, sans déplacer la bulle (alignée sur son bord).
        tools = Gtk.Box(valign=Gtk.Align.CENTER)
        tools.hover = None

        def ensure_tools():
            if tools.hover is None:
                btn = Gtk.Button.new_from_icon_name("face-smile-symbolic", Gtk.IconSize.BUTTON)
                btn.set_relief(Gtk.ReliefStyle.NONE)
                btn.set_valign(Gtk.Align.CENTER)
                btn.set_tooltip_text("Réagir")
                btn.connect("clicked", lambda b: self._react_popover(b, m.id))
                reply = Gtk.Button.new_from_icon_name("mail-reply-sender-symbolic", Gtk.IconSize.BUTTON)
                reply.set_relief(Gtk.ReliefStyle.NONE)
                reply.set_tooltip_text("Répondre")
                reply.connect("clicked", lambda _b: self._start_reply(m.id))
                more = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Plus d'options")
                more.add(Gtk.Image.new_from_gicon(
                    Gio.ThemedIcon.new_with_default_fallbacks("view-more-horizontal-symbolic"), Gtk.IconSize.BUTTON))
                more.connect("clicked", lambda b: self._options_popover(b, m.id))
                tools.hover = [btn, reply, more]
                # De la bulle vers l'extérieur : 😊, ↩, puis « … » au bout.
                for b in (reversed(tools.hover) if m.outgoing else tools.hover):
                    b.get_style_context().add_class("react-btn")
                    b.set_opacity(0)
                    tools.pack_start(b, False, False, 0)
                tools.show_all()
            return tools.hover

        line = Gtk.Box(spacing=2)
        for child in ((tools, holder) if m.outgoing else (holder, tools)):
            line.pack_start(child, False, False, 0)

        # Fenêtre d'entrée seulement (pas de surface propre) : la liste se réagence bien plus vite
        # quand des messages plus anciens s'ajoutent en haut.
        ev = Gtk.EventBox()
        ev.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        ev.add(line)
        ev.connect("enter-notify-event", lambda *_a: [b.set_opacity(1) for b in ensure_tools()])
        ev.connect("leave-notify-event", lambda _w, e: e.detail == Gdk.NotifyType.INFERIOR
                   or [b.set_opacity(0) for b in tools.hover or ()])
        ev.connect("button-press-event", lambda _w, e: e.button == 3
                   and self._options_popover(ensure_tools()[2], m.id))
        ev.tools = ensure_tools
        return ev

    def _emoji_art(self, emoji):
        """Ce qu'il faut dessiner pour une réaction : le caractère, ou l'image d'un emoji personnalisé."""
        if not is_custom(emoji):
            return emoji
        if emoji not in self._emoji_arts:
            known = self.core.custom_emoji(emoji)
            art = None
            if known:
                try:
                    art = load_pixbuf(known[1], 96, 96)
                except GLib.Error:
                    pass
            if art is None:
                return "❔"
            self._emoji_arts[emoji] = art
        return self._emoji_arts[emoji]

    def _emoji_text(self, emoji):
        if not is_custom(emoji):
            return emoji
        known = self.core.custom_emoji(emoji)
        return f":{known[0]}:" if known else "avec un emoji"

    def _animate_later(self, holder, path, size_fn):
        """Décode l'animation de ``path`` hors de l'interface puis remplace l'image fixe de ``holder``."""
        alive = [True]
        holder.connect("destroy", lambda *_a: alive.__setitem__(0, False))

        def done(anim):
            if alive[0] and anim is not None:
                old = holder.get_child()
                if old is not None:
                    holder.remove(old)
                img = AnimatedImage(anim, size_fn)
                holder.add(img)
                img.show()
            return False
        _decoder().submit(lambda: GLib.idle_add(done, load_animation(path)))

    def _sticker_widget(self, token, name, path):
        """Emoji personnalisé envoyé seul : affiché en grand, animé s'il s'agit d'un GIF."""
        anim = load_animation(path)
        if anim:
            img = AnimatedImage(anim, lambda w, h: fit(w, h, STICKER, STICKER, upscale=True))
        else:
            try:
                pb = load_pixbuf(path)
                img = Gtk.Image.new_from_pixbuf(pb.scale_simple(
                    *fit(pb.get_width(), pb.get_height(), STICKER, STICKER, upscale=True),
                    GdkPixbuf.InterpType.BILINEAR))
            except GLib.Error:
                img = _label(f":{name}:")
        img.set_halign(Gtk.Align.START)
        img.set_tooltip_text(f":{name}:")
        return img

    def _fill_reactions(self, msg_id, box):
        for child in box.get_children():
            child.destroy()
        box.sprites = {}
        me = self.core.identity.vk_hex
        by_emoji = {}
        for emoji, reactor in self.core.store.reactions(msg_id):
            by_emoji.setdefault(emoji, []).append(reactor)
        for emoji, reactors in by_emoji.items():
            names = []
            for r in reactors:
                c = None if r == me else self.core.store.contact_by_vk(r)
                names.append("Vous" if r == me else (c.name if c else "?"))
            art = self._emoji_art(emoji)
            sprite = EmojiSprite(art, 18, room=1.4)
            inner = Gtk.Box(spacing=1)
            inner.pack_start(sprite, False, False, 0)
            if len(reactors) > 1:
                inner.pack_start(_label(str(len(reactors)), "reaction-count"), False, False, 0)
            b = Gtk.Button()
            b.add(inner)
            ctx = b.get_style_context()
            ctx.add_class("reaction")
            if me in reactors:
                ctx.add_class("mine")
            b.connect("clicked", lambda _b, e=emoji: self.core.react(msg_id, e))
            b.connect("enter-notify-event", lambda w, _e, s=sprite, a=art, n=names, e=emoji:
                      self._peek_reaction(w, s, a, n, e))
            b.connect("leave-notify-event", lambda _w, e: e.detail == Gdk.NotifyType.INFERIOR
                      or self._unpeek_reaction())
            b.connect("button-press-event", lambda w, e, em=emoji: e.button == 3
                      and self._reaction_menu(w, em))
            b.connect("destroy", lambda w: self._peek_button is w and self._unpeek_reaction(now=True))
            box.pack_start(b, False, False, 0)
            b.show_all()
            box.sprites[emoji] = sprite
        box.set_visible(bool(by_emoji))
        spacer = box.spacer
        if by_emoji:
            width, height = box.get_preferred_width()[1], box.get_preferred_height()[1]
            # Chevauchement franc si les pastilles tiennent à gauche de l'heure, sinon seulement
            # sur la marge du bas de la bulle (l'heure reste lisible).
            bubble = self._bubbles.get(msg_id)
            meta = getattr(bubble, "meta", None) if bubble else None
            room = (bubble.get_preferred_width()[1] - (meta.get_preferred_width()[1] if meta else 0)
                    - REACT_INSET - 10) if bubble else 0
            overlap = REACT_OVERLAP if width <= room else 6
            spacer.set_size_request(width + REACT_INSET, max(4, height - overlap))
            spacer.show()
        else:
            spacer.hide()

    def _cancel_peek_timer(self, kind):
        timer = self._peek_timers.pop(kind, None)
        if timer:
            GLib.source_remove(timer)

    def _peek_reaction(self, button, sprite, art, names, emoji):
        """Survol d'une pastille : elle rebondit et l'emoji s'affiche en grand au-dessus, avec les noms.
        L'aperçu n'apparaît qu'après un court instant sur la pastille et ne capte pas le pointeur :
        un passage rapide ne le fait pas clignoter."""
        self._cancel_peek_timer("hide")
        if self._peek_button is button and (self._peek is not None or "show" in self._peek_timers):
            return False                # déjà affiché (ou sur le point de l'être) pour cette pastille
        self._unpeek_reaction(now=True)
        sprite.bump()
        self._peek_button = button

        def show():
            self._peek_timers.pop("show", None)
            if self._peek_button is button and button.get_mapped():
                self._show_peek(button, art, names, emoji)
            return False
        self._peek_timers["show"] = GLib.timeout_add(PEEK_DELAY, show)
        return False

    def _show_peek(self, button, art, names, emoji):
        pop = Gtk.Popover(relative_to=button, modal=False, position=Gtk.PositionType.TOP)
        pop.get_style_context().add_class("reaction-peek")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=6)
        big = EmojiSprite(art, 56, room=1.45)
        big.set_halign(Gtk.Align.CENTER)
        box.pack_start(big, False, False, 0)
        if is_custom(emoji):
            box.pack_start(_label(self._emoji_text(emoji), "reaction-peek-name", xalign=0.5), False, False, 0)
        who = _label(", ".join(names), "reaction-peek-who", xalign=0.5, wrap=True)
        who.set_max_width_chars(28)
        box.pack_start(who, False, False, 0)
        known = self.core.custom_emoji(emoji) if is_custom(emoji) else None
        if known and not known[2]:
            add = Gtk.Button(label="Ajouter à mes emojis", halign=Gtk.Align.CENTER)
            add.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
            add.set_always_show_image(True)
            add.get_style_context().add_class("reaction-peek-add")

            def keep(btn):
                self.core.keep_custom_emoji(emoji)
                btn.set_label("Ajouté à vos emojis")
                btn.set_image(Gtk.Image.new_from_icon_name("object-select-symbolic", Gtk.IconSize.BUTTON))
                btn.set_sensitive(False)
            add.connect("clicked", keep)
            box.pack_start(add, False, False, 0)
        pop.add(box)
        box.show_all()
        # La souris peut passer de la pastille à la bulle (pour son bouton) sans la refermer :
        # la fermeture attend un court instant, annulé dès que le pointeur est sur l'une ou l'autre.
        pop.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        pop.connect("enter-notify-event", lambda *_a: self._cancel_peek_timer("hide") or False)
        pop.connect("leave-notify-event", lambda _w, e: e.detail == Gdk.NotifyType.INFERIOR
                    or self._peek is not pop or self._unpeek_reaction())
        pop.popup()
        big.pop()
        self._peek = pop

    def _unpeek_reaction(self, now=False):
        """Ferme l'aperçu ; sans ``now``, après un court délai (annulé si le pointeur revient)."""
        self._cancel_peek_timer("show")
        if not now:
            if self._peek is None:
                self._peek_button = None
            elif "hide" not in self._peek_timers:
                def hide():
                    self._peek_timers.pop("hide", None)
                    self._unpeek_reaction(now=True)
                    return False
                self._peek_timers["hide"] = GLib.timeout_add(PEEK_HIDE_DELAY, hide)
            return False
        self._cancel_peek_timer("hide")
        pop, self._peek, self._peek_button = self._peek, None, None
        if pop is not None:
            pop.popdown()
            GLib.timeout_add(400, lambda: pop.destroy() or False)
        return False

    def _reaction_menu(self, button, emoji):
        """Clic droit sur une réaction personnalisée reçue : l'ajouter à mes emojis."""
        known = self.core.custom_emoji(emoji) if is_custom(emoji) else None
        if not known or known[2]:
            return False
        self._unpeek_reaction(now=True)
        pop = Gtk.Popover(relative_to=button)
        row = Gtk.ModelButton(text=f"Ajouter {self._emoji_text(emoji)} à mes emojis", centered=False)
        row.connect("clicked", lambda _b: (pop.popdown(), self.core.keep_custom_emoji(emoji)))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=4)
        content.pack_start(row, False, False, 0)
        pop.add(content)
        content.show_all()
        pop.popup()
        return True

    def _send_sticker(self, token):
        if self.current:
            self._stick_bottom = True
            self.core.send_sticker(self.current, token, reply_to=self._replying)
            self._cancel_reply()

    def on_custom_emojis(self):
        for picker in self._emoji_pickers:
            picker.refresh_custom()

    def _react_popover(self, anchor, msg_id):
        pop = Gtk.Popover(relative_to=anchor)
        grid = Gtk.Box(spacing=0, margin=4)
        for emoji in QUICK_EMOJIS:
            b = Gtk.Button(label=emoji, relief=Gtk.ReliefStyle.NONE)
            b.get_style_context().add_class("emoji-pick")
            b.connect("clicked", lambda _b, e=emoji: (self.core.react(msg_id, e), pop.popdown()))
            grid.pack_start(b, False, False, 0)
        more = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Tous les emoji…")
        more.add(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        more.get_style_context().add_class("emoji-more")
        more.connect("clicked", lambda _b: (pop.popdown(), self._react_picker(anchor, msg_id)))
        grid.pack_start(more, False, False, 0)
        pop.add(grid)
        grid.show_all()
        pop.popup()
        return True

    def _options_popover(self, anchor, msg_id):
        """Menu « … » d'un message : répondre, copier, modifier, historique, informations, supprimer."""
        m = self.core.store.get_message(msg_id)
        if m is None:
            return True
        pop = Gtk.Popover(relative_to=anchor)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=4)
        actions = []
        if m:
            actions.append(("Répondre", self._start_reply))
        if m and m.kind == "text":
            actions.append(("Copier le texte", self._copy_text))
        if m and m.outgoing and m.kind == "text" and not m.sticker:
            actions.append(("Modifier", self._start_edit))
        known = self.core.custom_emoji(m.sticker) if m and m.sticker else None
        if known and not known[2]:
            actions.append(("Ajouter à mes emojis", lambda _mid, t=m.sticker: self.core.keep_custom_emoji(t)))
        if m and m.edited:
            actions.append(("Historique des modifications",
                            self._show_history))
        att = self.core.store.get_attachment(msg_id) if m and m.kind == "file" else None
        if att and os.path.isfile(att.path):
            actions.append(("Enregistrer sous…", lambda _mid, a=att: self._save_file_as(a)))
            actions.append(("Afficher dans le dossier", lambda _mid, a=att: self._show_in_folder(a.path)))
        if m and m.outgoing and m.deleted is None:
            actions.append(("Informations", self._show_receipts))
        if m and m.outgoing:
            actions.append((None, None))
            actions.append(("Supprimer", self._confirm_delete))
        for text, fn in actions:
            if text is None:
                content.pack_start(Gtk.Separator(), False, False, 3)
                continue
            row = Gtk.ModelButton(text=text, centered=False)
            if fn == self._confirm_delete:
                row.get_style_context().add_class("destructive")
            row.connect("clicked", lambda _b, fn=fn: (pop.popdown(), fn(msg_id)))
            content.pack_start(row, False, False, 0)
        pop.add(content)
        content.show_all()
        pop.popup()
        return True

    def _react_picker_widget(self):
        picker = self.__dict__.get("_react_popover_full")
        if picker is None:
            picker = EmojiPopover(self, on_pick=lambda e: self._react_target and self.core.react(
                self._react_target, e), accept=valid_emoji)
            self._react_popover_full = picker
        return picker

    def _react_picker(self, anchor, msg_id):
        """Sélecteur complet pour réagir avec n'importe quel emoji."""
        picker = self._react_picker_widget()
        self._react_target = msg_id
        picker.set_relative_to(anchor)
        picker.popup()

    def on_reaction(self, msg_id, contact_id=None, emoji=None, on=False, incoming=False):
        if msg_id in self._reaction_boxes and self._reaction_boxes[msg_id] is None \
                and self.core.store.reactions(msg_id):
            self._rerender(msg_id)      # première réaction : la bulle reçoit sa zone de pastilles
        box = self._reaction_boxes.get(msg_id)
        if box:
            self._fill_reactions(msg_id, box)
            sprite = box.sprites.get(emoji) if on else None
            if sprite is not None and self.get_visible():
                # L'emoji surgit sur le message puis se pose sur sa pastille (une fois celle-ci placée).
                sprite.hidden = True
                bubble = self._bubbles.get(msg_id)
                GLib.timeout_add(40, lambda: self.fx.fly(self._emoji_art(emoji), bubble, sprite)
                                 if bubble else sprite.pop() or False)
        m = self.core.store.get_message(msg_id)
        visible = m and self.current == m.conv and self.is_active() and self.get_visible()
        if incoming and on and m and m.outgoing and not visible and not self.core.muted_until(m.conv):
            c = self.core.store.get_contact(contact_id)
            self.app.notifier.notify(f"{c.name if c else '?'} a réagi {self._emoji_text(emoji)}",
                                     f"à votre message : {m.text}", m.conv)

    def _receipt_groups(self, msg_id):
        """(lus, distribués non lus, en attente) : listes de (nom, date, contact_id)."""
        read, delivered, pending = [], [], []
        for cid, d, r in self.core.store.receipts(msg_id):
            name = self._sender_name(cid)
            if r:
                read.append((name, r, cid))
            elif d:
                delivered.append((name, d, cid))
            else:
                pending.append((name, None, cid))
        return read, delivered, pending

    def _receipt_status(self, m):
        """(symbole, couleur ou None, info-bulle) de l'état d'un de mes messages ; 🔕 si un destinataire
        avait mis la conversation en sourdine quand le message lui est parvenu."""
        sym, color, tip = self._receipt_status_base(m)
        muted = self.core.store.muted_receipts(m.id)
        if muted:
            names = ", ".join(f"{self._sender_name(cid)} (jusqu'à {_clock(until)})" for cid, until in muted.items())
            sym += " 🔕"
            tip += f"\nEn sourdine à la réception, sans notification : {names}"
        return sym, color, tip

    def _receipt_status_base(self, m):
        if m.status == "failed":
            return "⚠", None, "Échec : le fichier à envoyer a disparu"
        read, delivered, pending = self._receipt_groups(m.id)
        total = len(read) + len(delivered) + len(pending)
        if not total:        # message antérieur au suivi par destinataire
            if m.status == "pending":
                return "🕓", None, "En attente : le destinataire n'est pas joignable"
            return "✓", None, "Distribué"
        group = m.conv.startswith("g:")
        if not group:
            if read:
                return "✓✓", READ_COLOR, f"Lu {_when(read[0][1])}"
            if delivered:
                return "✓✓", None, f"Distribué {_when(delivered[0][1])}, pas encore lu"
            return "🕓", None, "En attente : le destinataire n'est pas joignable"
        lines = []
        for title, people in (("Lu par", read), ("Distribué à", delivered), ("En attente", pending)):
            if people:
                lines.append(f"{title} : " + ", ".join(p[0] for p in people))
        tip = "\n".join(lines) + "\nCliquez pour le détail"
        if len(read) == total:
            return "✓✓", READ_COLOR, tip
        if not pending:
            sym = f"✓✓ {len(read)}/{total}" if read else "✓✓"
            return sym, None, tip
        if read or delivered:
            return f"✓ {len(read) + len(delivered)}/{total}", None, tip
        return "🕓", None, tip

    def _set_meta(self, label, m):
        esc = GLib.markup_escape_text
        parts = []
        if m.edited:
            when = time.strftime("%d/%m à %H:%M", time.localtime(m.edited))
            tip = f"Modifié le {when} — cliquez pour voir l'historique"
            parts.append(f"<a href='history' title='{esc(tip)}'>modifié</a>  ")
        parts.append(esc(time.strftime("%H:%M", time.localtime(m.ts))))
        label.set_tooltip_text(None)
        if m.outgoing and m.deleted is None:
            sym, color, tip = self._receipt_status(m)
            color_attr = f" foreground='{color}' weight='bold'" if color else ""
            inner = f"<span underline='none'{color_attr}>{esc(sym)}</span>"
            parts.append(f"  <a href='receipts' title='{esc(tip)}'>{inner}</a>")
        label.set_markup("".join(parts))

    def _show_receipts(self, msg_id):
        """Détail de la distribution et de la lecture d'un de mes messages, personne par personne."""
        m = self.core.store.get_message(msg_id)
        if not m or not m.outgoing:
            return
        read, delivered, pending = self._receipt_groups(msg_id)
        muted = self.core.store.muted_receipts(msg_id)
        dlg, box = _dialog(self, "Informations sur le message")
        dlg.set_default_size(380, -1)
        dlg.get_style_context().add_class("qchat")
        excerpt = _label(self._excerpt(m), "subtitle", wrap=True)
        excerpt.set_max_width_chars(45)
        excerpt.set_lines(3)
        excerpt.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(excerpt, False, False, 0)
        box.pack_start(_label(f"Envoyé le {time.strftime('%d/%m/%Y à %H:%M', time.localtime(m.ts))}",
                              "meta"), False, False, 0)
        if not (read or delivered or pending):
            box.pack_start(_label("Ce message date d'avant le suivi de lecture : seule sa distribution "
                                  "est connue (" + ("distribué" if m.status == "delivered" else "en attente")
                                  + ").", wrap=True), False, False, 0)
        for title, icon, people, fmt in (
                ("Lu par", "✓✓", read, "lu {}"), ("Distribué à", "✓✓", delivered, "distribué {}"),
                ("En attente", "🕓", pending, None)):
            if not people:
                continue
            box.pack_start(_label(f"{title} ({len(people)})", "receipt-title"), False, False, 0)
            for name, ts, cid in people:
                row = Gtk.Box(spacing=10)
                row.pack_start(_avatar(name, small=True, image=self.core.avatar_path(f"c:{cid}")),
                               False, False, 0)
                texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
                texts.pack_start(_label(name), False, False, 0)
                detail = fmt.format(_when(ts)) if fmt else "pas encore distribué (poste injoignable)"
                if cid in muted:
                    detail += f" · 🔕 en sourdine jusqu'à {_clock(muted[cid])}, non notifié"
                texts.pack_start(_label(detail, "meta"), False, False, 0)
                row.pack_start(texts, True, True, 0)
                mark = _label(icon, "receipt-read" if people is read else "meta")
                row.pack_start(mark, False, False, 0)
                box.pack_start(row, False, False, 0)
        dlg.show_all()
        dlg.connect("response", lambda d, _r: d.destroy())

    def on_receipt(self, msg_id):
        meta = self._status_labels.get(msg_id)
        m = self.core.store.get_message(msg_id) if meta else None
        if m:
            self._set_meta(meta, m)

    def _add_row(self, widget, halign, position=-1):
        widget.set_halign(halign)
        widget.set_margin_start(12)
        widget.set_margin_end(12)
        widget.set_margin_top(3)
        widget.set_margin_bottom(3)
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.add(widget)
        row.show_all()
        self.msg_list.insert(row, position)
        return row

    def _on_adj_changed(self, adj):
        if self._anchor:        # messages plus anciens ajoutés en haut : on garde la même vue
            upper, value = self._anchor
            if adj.get_upper() != upper:
                self._anchor = None
                adj.set_value(value + adj.get_upper() - upper)
            return
        if self._stick_bottom:
            adj.set_value(adj.get_upper() - adj.get_page_size())
        self._maybe_load_older(adj)

    def _maybe_load_older(self, adj):
        """Près du haut (ou historique trop court pour remplir la vue) : paquet précédent."""
        if adj.get_value() < OLDER_MARGIN and self._older and not self._older_pending and not self._anchor:
            self._older_pending = True
            GLib.idle_add(self._load_older)

    def _on_adj_value(self, adj):
        self._stick_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 30
        self._maybe_load_older(adj)

    # -- pièces jointes ---------------------------------------------------------
    def _attachment_widget(self, m):
        att = self.core.store.get_attachment(m.id)
        if att is None:
            return _label(m.text)
        exists = os.path.isfile(att.path)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # Les pièces jointes reçues avant la détection par contenu peuvent être des images sans extension.
        is_image = att.is_image or (att.mime == "application/octet-stream"
                                    and (sniff_mime(att.path) or "").startswith("image/"))
        thumb = self._thumbnail(att.path) if is_image and exists else None
        if thumb:
            ev = Gtk.EventBox(tooltip_text=f"{att.name} — cliquer pour agrandir")
            ev.add(Gtk.Image.new_from_surface(thumb))
            if _maybe_animated(att.path):
                # GIF animé : décodé en arrière-plan, l'aperçu fixe est remplacé une fois prêt.
                self._animate_later(ev, att.path, lambda w, h: fit(w, h, 320, 240))
            ev.connect("button-press-event", lambda _w, e: e.button == 1 and self._view_file(m.id) or True)
            ev.connect("realize", lambda w: w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
            box.pack_start(ev, False, False, 0)
            # Image affichée : ni nom de fichier ni boutons (clic droit : enregistrer, dossier…).
            return box
        row = Gtk.Box(spacing=8)
        if not thumb:
            icon = Gio.content_type_get_icon(Gio.content_type_from_mime_type(att.mime) or att.mime)
            row.pack_start(Gtk.Image.new_from_gicon(icon, Gtk.IconSize.DND), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        name = _label(att.name, "file-name")
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_max_width_chars(36)
        name.set_tooltip_text(att.name)
        texts.pack_start(name, False, False, 0)
        texts.pack_start(_label(_human_size(att.size) if exists else "fichier supprimé du poste", "meta"),
                         False, False, 0)
        row.pack_start(texts, True, True, 0)
        for icon_name, tip, action in (
                ("document-open-symbolic", "Ouvrir", lambda: self._view_file(m.id)),
                ("document-save-as-symbolic", "Enregistrer sous…", lambda: self._save_file_as(att)),
                ("folder-open-symbolic", "Afficher dans le dossier", lambda: self._show_in_folder(att.path))):
            btn = Gtk.Button.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
            btn.set_tooltip_text(tip)
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_valign(Gtk.Align.CENTER)
            btn.get_style_context().add_class("file-btn")
            btn.set_sensitive(exists)
            btn.connect("clicked", lambda _b, a=action: a())
            row.pack_start(btn, False, False, 0)
        box.pack_start(row, False, False, 0)
        return box

    # -- visionneuse -------------------------------------------------------------
    def _view_file(self, msg_id):
        """Ouvre la pièce jointe dans la visionneuse intégrée si son type s'y prête (image, PDF, texte),
        sinon avec l'application par défaut. Les flèches parcourent les autres fichiers de la discussion."""
        att = self.core.store.get_attachment(msg_id)
        if not att or not os.path.isfile(att.path):
            return
        if not viewable_kind(att.path, att.mime):
            self._open_file(att.path)
            return
        items, index = [], 0
        for m in self.core.store.messages(self.current or "", limit=500):
            a = self.core.store.get_attachment(m.id) if m.kind == "file" else None
            if a and (a.msg_id == msg_id or os.path.isfile(a.path) and viewable_kind(a.path, a.mime)):
                if a.msg_id == msg_id:
                    index = len(items)
                items.append({"path": a.path, "name": a.name, "mime": a.mime, "size": a.size, "att": a})
        if not items:
            items = [{"path": att.path, "name": att.name, "mime": att.mime, "size": att.size, "att": att}]
        if self._viewer is None:
            self._viewer = Viewer(self, self.app.theme.dark, lambda it: self._save_file_as(it["att"], self._viewer),
                                  self._open_file)
            self._viewer.connect("destroy", lambda *_a: setattr(self, "_viewer", None))
        self._viewer.show_items(items, index)

    def _thumbnail(self, path):
        """Aperçu (surface cairo, 320 × 240 au plus) d'une image, depuis le cache disque si possible."""
        return _thumbs().get(path, PREVIEW, lambda: _make_preview(path))

    def _error(self, text, secondary=None):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.CLOSE, text=text)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def _open_file(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error as e:
            self._error("Impossible d'ouvrir le fichier", e.message)

    def _show_in_folder(self, path):
        uri = Gio.File.new_for_path(path).get_uri()
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
                "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1", "ShowItems", GLib.Variant("(ass)", ([uri], "")),
                None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error:
            self._open_file(os.path.dirname(path))

    def _save_file_as(self, att, parent=None):
        chooser = Gtk.FileChooserNative(title="Enregistrer le fichier", transient_for=parent or self,
                                        action=Gtk.FileChooserAction.SAVE, accept_label="Enregistrer")
        chooser.set_current_name(att.name)
        chooser.set_do_overwrite_confirmation(True)
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if downloads:
            chooser.set_current_folder(downloads)
        if chooser.run() == Gtk.ResponseType.ACCEPT:
            try:
                shutil.copyfile(att.path, chooser.get_filename())
            except OSError as e:
                self._error("Impossible d'enregistrer le fichier", e.strerror)
        chooser.destroy()

    def _pick_files(self):
        if not self.current:
            return
        chooser = Gtk.FileChooserNative(title="Joindre des fichiers", transient_for=self,
                                        action=Gtk.FileChooserAction.OPEN, accept_label="Envoyer")
        chooser.set_select_multiple(True)
        if chooser.run() == Gtk.ResponseType.ACCEPT:
            self._send_files(chooser.get_filenames())
        chooser.destroy()

    def _send_files(self, paths, cleanup=False, conv=None):
        """Envoie en arrière-plan (la copie d'un gros fichier ne bloque pas l'interface)."""
        conv = conv or self.current
        if not conv or not paths:
            return
        self._stick_bottom = True

        def work():
            errors = []
            for path in paths:
                try:
                    self.core.send_file(conv, path)
                except (ValueError, OSError) as e:
                    errors.append(str(e) if isinstance(e, ValueError)
                                  else f"{os.path.basename(path)} : {e.strerror}")
                finally:
                    if cleanup:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
            if errors:
                GLib.idle_add(lambda: self._error("Certains fichiers n'ont pas été envoyés",
                                                  "\n".join(errors)) and False)

        threading.Thread(target=work, daemon=True).start()

    def _send_gif(self, item):
        """Télécharge le GIF choisi puis l'envoie comme une pièce jointe ordinaire."""
        conv = self.current
        if not conv:
            return

        def work():
            try:
                data = gifs.download(item["url"])
            except gifs.GifError as e:
                GLib.idle_add(lambda: self._error("Impossible d'envoyer le GIF", str(e)) and False)
                return
            tmp_dir = os.path.join(self.core.home, "tmp", uuid_hex())
            os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
            path = os.path.join(tmp_dir, gifs.filename(item["title"]))
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(data)
            GLib.idle_add(lambda: self._send_files([path], cleanup=True, conv=conv) and False)

        threading.Thread(target=work, daemon=True).start()

    def _on_drag_motion(self, *_a):
        if not self.drop_hint.get_visible():
            if self.current:
                kind, ident = self.current.split(":", 1)
                target = (self.core.store.get_group(ident) if kind == "g"
                          else self.core.store.get_contact(int(ident)))
                self.drop_hint.set_text(f"📎  Déposez ici pour envoyer à {target.name if target else '…'}")
            else:
                self.drop_hint.set_text("Ouvrez d'abord une conversation pour y déposer des fichiers")
            self.drop_hint.show()
        return False

    def _drop_paths(self, data):
        self.drop_hint.hide()
        paths = [Gio.File.new_for_uri(u).get_path() for u in data.get_uris() or []]
        paths = [p for p in paths if p]
        if paths and not self.current:
            self._error("Aucune conversation ouverte",
                        "Choisissez d'abord la personne ou le groupe à qui envoyer ces fichiers.")
        else:
            self._send_files(paths)

    def _on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        self._drop_paths(data)

    def _on_input_drop(self, widget, ctx, _x, _y, data, info, time_):
        if info == 1:     # fichiers : on les envoie au lieu d'insérer leur chemin dans la saisie
            widget.stop_emission_by_name("drag-data-received")
            Gtk.drag_finish(ctx, True, False, time_)
            self._drop_paths(data)
        else:
            self.drop_hint.hide()

    def _paste(self):
        """Ctrl+V : fichiers copiés depuis le gestionnaire de fichiers, ou image (capture d'écran).
        Renvoie True si le collage a été pris en charge."""
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        uris = cb.wait_for_uris() if cb.wait_is_uris_available() else None
        if not uris:
            sel = cb.wait_for_contents(Gdk.Atom.intern("x-special/gnome-copied-files", False))
            if sel and sel.get_data():
                lines = sel.get_data().decode(errors="ignore").splitlines()
                uris = [ln for ln in lines[1:] if ln.startswith("file://")]
        if uris:
            paths = [p for p in (Gio.File.new_for_uri(u).get_path() for u in uris) if p]
            if paths:
                self._send_files(paths)
                return True
        if cb.wait_is_image_available() and not cb.wait_is_text_available():
            pixbuf = cb.wait_for_image()
            if pixbuf and self._confirm_image(pixbuf):
                tmp_dir = os.path.join(self.core.home, "tmp")
                os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
                path = os.path.join(tmp_dir, time.strftime("capture-%Y%m%d-%H%M%S.png"))
                pixbuf.savev(path, "png", [], [])
                self._send_files([path], cleanup=True)
            return True
        return False

    def _confirm_image(self, pixbuf):
        dlg, box = _dialog(self, "Envoyer cette image ?", "Envoyer")
        w, h = pixbuf.get_width(), pixbuf.get_height()
        scale = min(1.0, 400 / w, 300 / h)
        preview = pixbuf.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)),
                                      GdkPixbuf.InterpType.BILINEAR)
        box.pack_start(Gtk.Image.new_from_pixbuf(preview), False, False, 0)
        box.pack_start(_label(f"{w} × {h} pixels", "subtitle", xalign=0.5), False, False, 0)
        dlg.show_all()
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        return ok

    def _on_input_key(self, _w, event):
        if not event.is_modifier:
            if event.keyval == Gdk.KEY_BackSpace and self._undo_auto():
                return True
            self._last_auto = None
        if self.mention_popover.handle_key(event) or self.emoji_completion.handle_key(event):
            return True
        if (event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (Gdk.KEY_v, Gdk.KEY_V)
                and self.current and self._paste()):
            return True
        if event.keyval == Gdk.KEY_Escape and self._editing:
            self._cancel_edit()
            return True
        if event.keyval == Gdk.KEY_Escape and self._replying:
            self._cancel_reply()
            return True
        buf = self.input.get_buffer()
        if (event.keyval == Gdk.KEY_Up and not self._editing and self.current
                and buf.get_char_count() == 0):
            last = self._last_editable()
            if last:
                self._start_edit(last)
                return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (
                event.state & Gdk.ModifierType.SHIFT_MASK):
            self._send()
            return True
        return False

    # -- remplacements automatiques (smileys, :emoji:) ------------------------------------
    def _on_input_insert(self, buf, location, text, _length):
        """Une espace ou un retour à la ligne vient d'être tapé : convertit le smiley qui précède."""
        if self._auto_busy or text not in (" ", "\n") or not self.app.smileys_enabled():
            return
        end = location.get_offset() - len(text)
        line = buf.get_iter_at_offset(end)
        line.set_line_offset(0)
        before = buf.get_text(line, buf.get_iter_at_offset(end), False)
        found = emojis.smiley_before(before)
        if found:
            start = line.get_offset() + found[0]
            # On ne modifie pas le tampon pendant le signal d'insertion : juste après.
            GLib.idle_add(self._convert_smiley, start, before[found[0]:], found[1], text,
                          priority=GLib.PRIORITY_HIGH)

    def _convert_smiley(self, start, token, emoji, follow):
        buf = self.input.get_buffer()
        stop = start + len(token) + len(follow)
        current = buf.get_text(buf.get_iter_at_offset(start), buf.get_iter_at_offset(stop), False)
        cursor = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        if current == token + follow and cursor == stop:
            self.auto_replace(start, len(token), emoji, follow)
        return False

    def auto_replace(self, start, length, new, follow=""):
        """Remplace ``length`` caractères à partir de ``start`` par ``new`` ; Retour arrière juste après
        rétablit le texte tapé. ``follow`` : ce qui suit et reste en place (l'espace tapée)."""
        buf = self.input.get_buffer()
        begin, end = buf.get_iter_at_offset(start), buf.get_iter_at_offset(start + length)
        original = buf.get_text(begin, end, False)
        self._auto_busy = True
        try:
            buf.delete(begin, end)
            buf.insert(buf.get_iter_at_offset(start), new)
            buf.place_cursor(buf.get_iter_at_offset(start + len(new) + len(follow)))
        finally:
            self._auto_busy = False
        self._last_auto = (start, new + follow, original + follow)

    def _undo_auto(self):
        if not self._last_auto:
            return False
        start, replaced, original = self._last_auto
        self._last_auto = None
        buf = self.input.get_buffer()
        stop = start + len(replaced)
        if buf.get_has_selection() or buf.get_iter_at_mark(buf.get_insert()).get_offset() != stop:
            return False
        if buf.get_text(buf.get_iter_at_offset(start), buf.get_iter_at_offset(stop), False) != replaced:
            return False
        self._auto_busy = True
        try:
            buf.delete(buf.get_iter_at_offset(start), buf.get_iter_at_offset(stop))
            buf.insert(buf.get_iter_at_offset(start), original)
        finally:
            self._auto_busy = False
        self.emoji_completion.dismiss_here()
        self._kept_smiley = start
        return True

    def set_update_ready(self, version):
        """Une version plus récente est installée : le menu propose de redémarrer pour l'appliquer."""
        menu = self.main_menu
        for i in range(menu.get_n_items()):
            action = menu.get_item_attribute_value(i, "action", GLib.VariantType.new("s"))
            if action is not None and action.get_string() == "app.restart":
                menu.remove(i)
                menu.insert(i, f"Redémarrer pour passer en {version}", "app.restart")
                break
        self.menu_btn.get_style_context().add_class("update-ready")
        self.menu_btn.set_tooltip_text(f"QChat {version} est installé : redémarrez pour l'utiliser")

    def has_draft(self):
        """Un message est en cours de saisie ou de modification."""
        buf = self.input.get_buffer()
        return self._editing is not None or bool(
            buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip())

    def _send(self):
        if not self.current:
            return
        buf = self.input.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if self.app.smileys_enabled():
            text = text.rstrip()
            found = emojis.smiley_before(text)
            if found and found[0] != self._kept_smiley:
                text = text[:found[0]] + found[1]
        self._kept_smiley = None
        if self._editing:
            if text.strip():
                self.core.edit_text(self._editing, text)
                self._cancel_edit()
            return
        command = text.strip().lower()
        if command in WIZZ_COMMANDS:
            self._send_wizz(WIZZ_COMMANDS[command])
            buf.set_text("")
            return
        if text.strip():
            self._stick_bottom = True
            self.core.send_text(self.current, text, reply_to=self._replying)
            buf.set_text("")
            self._cancel_reply()

    def _send_wizz(self, kind):
        wait = self.core.send_wizz(self.current, kind)
        if wait:
            note = _label(f"Patientez {wait} s avant d'envoyer un nouveau {kind}.", "system", xalign=0.5)
            self._add_row(note, Gtk.Align.CENTER)
            row = note.get_parent()
            GLib.timeout_add(4000, lambda: row.get_parent() and row.destroy() and False)
            return
        self._stick_bottom = True
        shake_widget(self.msg_scroll)

    # -- modification des messages --------------------------------------------
    # -- réponse à un message ----------------------------------------------------
    def _start_reply(self, msg_id):
        m = self.core.store.get_message(msg_id)
        if not m or m.conv != self.current or m.kind == "system" or m.deleted is not None:
            return
        self._cancel_edit()
        self._replying = msg_id
        who = "vous-même" if m.outgoing else self._sender_name(m.sender_id)
        self.reply_title.set_text(f"↩ Réponse à {who}")
        self.reply_orig.set_text(self._excerpt(m))
        self.reply_bar.set_no_show_all(False)
        self.reply_bar.show_all()
        self.input.grab_focus()

    def _cancel_reply(self):
        if self._replying is None:
            return
        self._replying = None
        self.reply_bar.hide()
        self.reply_bar.set_no_show_all(True)

    # -- suppression -------------------------------------------------------------
    def _confirm_delete(self, msg_id):
        m = self.core.store.get_message(msg_id)
        if not m or not m.outgoing or m.deleted is not None:
            return
        what = "ce fichier" if m.kind == "file" else "ce message"
        if _confirm(self, f"Supprimer {what} ?",
                    "Il sera remplacé par « Message supprimé par son auteur » chez vous et chez vos "
                    "destinataires. Cette action est définitive.", "Supprimer"):
            self.core.delete_message(msg_id)

    def on_deleted(self, msg_id, incoming=False):
        if self._editing == msg_id:
            self._cancel_edit()
        if self._replying == msg_id:
            self._cancel_reply()
        self._rerender(msg_id)
        for rid in list(self._replies.get(msg_id, ())):
            self._rerender(rid)
        self.refresh_sidebar()

    # -- copier ------------------------------------------------------------------
    def _copy_text(self, msg_id):
        m = self.core.store.get_message(msg_id)
        if m and m.kind == "text" and m.deleted is None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(m.text, -1)

    def _on_window_key(self, _w, event):
        """Ctrl+C copie le texte surligné dans une bulle (les bulles ne prennent pas le focus,
        le raccourci arriverait sinon à la zone de saisie, qui n'a rien de sélectionné)."""
        if not (event.state & Gdk.ModifierType.CONTROL_MASK
                and event.keyval in (Gdk.KEY_c, Gdk.KEY_C, Gdk.KEY_Insert)):
            return False
        focus = self.get_focus()
        if isinstance(focus, Gtk.TextView) and focus.get_buffer().get_has_selection():
            return False
        if isinstance(focus, Gtk.Editable) and focus.get_selection_bounds():
            return False
        lbl = self._sel_label
        if lbl is None or lbl.get_parent() is None:
            return False
        ok, start, end = lbl.get_selection_bounds()
        if not ok or start == end:
            return False
        text = lbl.get_text()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text[min(start, end):max(start, end)], -1)
        return True

    def _start_edit(self, msg_id):
        m = self.core.store.get_message(msg_id)
        if not m or not m.outgoing or m.kind != "text" or m.conv != self.current or m.deleted is not None:
            return
        self._cancel_reply()
        self._editing = msg_id
        self.edit_orig.set_text(" ".join(m.text.split()))
        self.edit_bar.set_no_show_all(False)
        self.edit_bar.show_all()
        buf = self.input.get_buffer()
        buf.set_text(m.text)
        buf.place_cursor(buf.get_end_iter())
        self.input.grab_focus()

    def _cancel_edit(self):
        if self._editing is None:
            return
        self._editing = None
        self.edit_bar.hide()
        self.edit_bar.set_no_show_all(True)
        self.input.get_buffer().set_text("")

    def _last_editable(self):
        for m in reversed(self.core.store.messages(self.current, limit=50)):
            if m.outgoing and m.kind == "text" and m.deleted is None:
                return m.id
        return None

    def on_edited(self, msg_id, incoming=False):
        m = self.core.store.get_message(msg_id)
        entry = self._text_labels.get(msg_id)
        if m and entry:
            self._rerender(msg_id)
        for rid in list(self._replies.get(msg_id, ())):
            self._rerender(rid)
        if self._replying == msg_id and m:
            self.reply_orig.set_text(self._excerpt(m))
        self.refresh_sidebar()

    def _show_history(self, msg_id):
        history = self.core.store.message_history(msg_id)
        if not history:
            return
        dlg, box = _dialog(self, "Historique des modifications")
        dlg.set_default_size(440, -1)
        dlg.get_style_context().add_class("qchat")
        versions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        last = len(history) - 1
        for i, (text, ts) in enumerate(reversed(history)):
            n = last - i
            what = "Version actuelle" if n == last else ("Original" if n == 0 else f"Version {n + 1}")
            when = time.strftime("%d/%m/%Y à %H:%M", time.localtime(ts))
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ctx = item.get_style_context()
            ctx.add_class("history-version")
            if n == last:
                ctx.add_class("current")
            verb = "envoyé" if n == 0 else "modifié"
            item.pack_start(_label(f"{what} · {verb} le {when}", "meta"), False, False, 0)
            body = _label(text, wrap=True, selectable=True)
            body.set_max_width_chars(55)
            body.set_can_focus(False)
            item.pack_start(body, False, False, 0)
            versions.pack_start(item, False, False, 0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_propagate_natural_height(True)
        sw.set_max_content_height(420)
        sw.add(versions)
        box.pack_start(sw, True, True, 0)
        dlg.show_all()
        dlg.connect("response", lambda d, _r: d.destroy())

    def _on_infobar(self, _bar, response):
        if not self.current or not self.current.startswith("c:"):
            return
        cid = int(self.current[2:])
        if response == 1:
            self.core.accept_pending_key(cid)
        elif response == 2:
            self.core.reject_pending_key(cid)

    # -- événements du cœur --------------------------------------------------
    def on_message(self, m, incoming):
        visible = self.current == m.conv and self.is_active() and self.get_visible()
        if self.current == m.conv:
            self._append_message(m)
        if visible:
            self.core.mark_read(m.conv)
        if incoming and not visible and not self.core.muted_until(m.conv):
            sender = self._sender_name(m.sender_id)
            title = sender
            if m.conv.startswith("g:"):
                g = self.core.store.get_group(m.conv[2:])
                title = f"{sender} — {g.name}" if g else sender
                if self.core.identity.vk_hex in m.mentions:
                    title = f"{sender} vous a mentionné" + (f" — {g.name}" if g else "")
            body = m.text
            if m.kind == "file":
                att = self.core.store.get_attachment(m.id)
                body = f"🖼 Image : {m.text}" if att and att.is_image else f"📎 Fichier : {m.text}"
            self.app.notifier.notify(title, body, m.conv)
            self.set_urgency_hint(True)
        self.refresh_sidebar()

    def on_status(self, msg_id, status):
        meta = self._status_labels.get(msg_id)
        m = self.core.store.get_message(msg_id) if meta else None
        if m:
            self._set_meta(meta, m)

    def _on_active_changed(self, *_a):
        if self.is_active():
            self.app.maybe_show_whats_new()
            self.set_urgency_hint(False)
            if self.current:
                self.core.mark_read(self.current)
                self.refresh_sidebar()
            # QChat est sous les yeux : plus de notification pour ce qui est déjà lu (pastille du dock)
            self.app.notifier.withdraw_read(self.app._has_unread)

    def _on_close(self, *_a):
        self.hide()
        if self.core.store.get_setting("background_hint") != "1":
            self.core.store.set_setting("background_hint", "1")
            self.app.notifier.notify("QChat reste actif en arrière-plan",
                                     "Vous continuerez à recevoir les notifications. "
                                     "Utilisez le menu ☰ › Quitter pour fermer complètement.", "",
                                     transient=True)
        return True

    # -- dialogues ----------------------------------------------------------
    def show_details(self):
        if not self.current:
            return
        kind, ident = self.current.split(":", 1)
        if kind == "g":
            GroupDialog(self, ident).run_dialog()
        else:
            ContactDialog(self, int(ident)).run_dialog()

    def add_contact_dialog(self):
        dlg, box = _dialog(self, "Ajouter un contact", "Ajouter")
        name = Gtk.Entry(placeholder_text="Nom (ex. Marie Dupont)", activates_default=True)
        box.pack_start(_label("Nom"), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(_label("Adresses IP (une par ligne)"), False, False, 0)
        ips = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE)
        ips.get_style_context().add_class("mono")
        box.pack_start(_scrolled(ips, 90), True, True, 0)
        box.pack_start(_label("Une personne peut avoir plusieurs IP (Wi-Fi, Ethernet…). "
                              f"Port facultatif : 10.0.0.12:{DEFAULT_PORT}. Les autres IP de la "
                              "personne seront ajoutées automatiquement à la première connexion.",
                              "subtitle", wrap=True), False, False, 0)
        error = _label("", "warning", wrap=True)
        box.pack_start(error, False, False, 0)
        dlg.show_all()
        while dlg.run() == Gtk.ResponseType.OK:
            buf = ips.get_buffer()
            lines = [ln for ln in buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                     .replace(",", "\n").split() if ln.strip()]
            endpoints = [netutil.parse_endpoint(ln, DEFAULT_PORT) for ln in lines]
            bad = [ln for ln, ep in zip(lines, endpoints) if ep is None]
            if not name.get_text().strip():
                error.set_text("Indiquez un nom.")
            elif not lines:
                error.set_text("Indiquez au moins une adresse IP.")
            elif bad:
                error.set_text("Adresse invalide : " + ", ".join(bad))
            elif len({p for _, p in endpoints}) > 1:
                error.set_text("Toutes les adresses d'une personne doivent utiliser le même port.")
            else:
                cid = self.core.add_contact(name.get_text(), endpoints)
                dlg.destroy()
                self.open_conv(f"c:{cid}")
                return
        dlg.destroy()

    def new_group_dialog(self):
        dlg, box = _dialog(self, "Nouveau groupe", "Créer")
        name = Gtk.Entry(placeholder_text="Nom du groupe", activates_default=True)
        box.pack_start(name, False, False, 0)
        box.pack_start(_label("Membres"), False, False, 0)
        picker = MemberPicker(self.core, exclude=())
        box.pack_start(picker, True, True, 0)
        error = _label("", "warning")
        box.pack_start(error, False, False, 0)
        dlg.show_all()
        while dlg.run() == Gtk.ResponseType.OK:
            if not name.get_text().strip():
                error.set_text("Indiquez un nom de groupe.")
            elif not picker.selected():
                error.set_text("Choisissez au moins un membre.")
            else:
                gid = self.core.create_group(name.get_text(), picker.selected())
                dlg.destroy()
                self.open_conv(f"g:{gid}")
                return
        dlg.destroy()

    def identity_dialog(self):
        dlg, box = _dialog(self, "Mon identité", "Enregistrer")
        dlg.get_style_context().add_class("qchat")
        picker = AvatarPicker(dlg, self.core, self.core.name or "?", self.core.avatar_path("me"),
                              "Votre photo est envoyée à tous vos contacts.")
        box.pack_start(picker, False, False, 0)
        name = Gtk.Entry(text=self.core.name, activates_default=True)
        name.connect("changed", lambda e: picker.set_name(e.get_text()))
        box.pack_start(_label("Nom affiché"), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(_label("Empreinte de ma clé (à comparer avec vos interlocuteurs)"), False, False, 0)
        box.pack_start(_label(self.core.identity.fingerprint, "mono", selectable=True), False, False, 0)
        ifaces = "\n".join(f"{ip}  ({n})" for n, ip, _ in netutil.local_interfaces()) or "aucune"
        box.pack_start(_label(f"Mes adresses IP (port {self.core.port})"), False, False, 0)
        box.pack_start(_label(ifaces, "mono", selectable=True), False, False, 0)
        box.pack_start(_label(f"Données : {self.core.home}", "subtitle", wrap=True, selectable=True),
                       False, False, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            if name.get_text().strip():
                self.core.set_name(name.get_text())
            if picker.choice is not None:
                self.core.set_avatar(picker.choice or None)
            self.refresh_sidebar()
        dlg.destroy()


def prepare_avatar(path, size=256):
    """Lit une image, la recadre en carré centré, la réduit et la renvoie en JPEG (bytes).
    Lève GLib.Error si le fichier n'est pas une image lisible."""
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    pb = pb.apply_embedded_orientation() or pb
    w, h = pb.get_width(), pb.get_height()
    side = min(w, h)
    pb = pb.new_subpixbuf((w - side) // 2, (h - side) // 2, side, side)
    for px, quality in ((size, 88), (size, 75), (192, 70), (160, 60)):
        # Le JPEG n'a pas de transparence : on compose sur un fond blanc opaque (RGB sans alpha).
        img = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, px, px)
        img.fill(0xFFFFFFFF)
        pb.composite(img, 0, 0, px, px, 0, 0, px / side, px / side, GdkPixbuf.InterpType.HYPER, 255)
        ok, data = img.save_to_bufferv("jpeg", ["quality"], [str(quality)])
        if ok and len(data) <= MAX_AVATAR:
            return bytes(data)
    raise GLib.Error("Image trop complexe pour être utilisée comme photo.")


class AvatarPicker(Gtk.Box):
    """Aperçu rond + « Choisir une photo… » / « Retirer ». ``choice`` : None (inchangée),
    b"" (retirée) ou les octets de la nouvelle photo."""

    def __init__(self, dialog, core, name, current, hint):
        super().__init__(spacing=14, margin_bottom=6)
        self.dialog, self.core, self.name = dialog, core, name
        self.current = current
        self.choice = None
        self._preview = None
        self.slot = Gtk.Box(valign=Gtk.Align.CENTER)
        self.pack_start(self.slot, False, False, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        buttons = Gtk.Box(spacing=6)
        choose = Gtk.Button(label="Choisir une photo…")
        choose.connect("clicked", lambda _b: self._choose())
        buttons.pack_start(choose, False, False, 0)
        self.remove_btn = Gtk.Button(label="Retirer")
        self.remove_btn.connect("clicked", lambda _b: self._set(b""))
        buttons.pack_start(self.remove_btn, False, False, 0)
        side.pack_start(buttons, False, False, 0)
        side.pack_start(_label(hint, "subtitle", wrap=True), False, False, 0)
        self.pack_start(side, True, True, 0)
        self.connect("destroy", lambda *_a: self._cleanup())
        self._render()

    def set_name(self, name):
        self.name = name.strip() or "?"
        self._render()

    def _image(self):
        if self.choice is None:
            return self.current
        return self._preview if self.choice else None

    def _render(self):
        for child in self.slot.get_children():
            self.slot.remove(child)
        self.slot.add(_avatar(self.name, large=True, image=self._image()))
        self.slot.show_all()
        self.remove_btn.set_sensitive(self._image() is not None)

    def _cleanup(self):
        if self._preview:
            try:
                os.unlink(self._preview)
            except OSError:
                pass
            self._preview = None

    def _set(self, data):
        self._cleanup()
        self.choice = data
        if data:
            tmp_dir = os.path.join(self.core.home, "tmp")
            os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
            fd, self._preview = tempfile.mkstemp(suffix=".jpg", dir=tmp_dir)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        self._render()

    def _choose(self):
        chooser = Gtk.FileChooserNative(title="Choisir une photo", transient_for=self.dialog,
                                        action=Gtk.FileChooserAction.OPEN, accept_label="Choisir")
        flt = Gtk.FileFilter()
        flt.set_name("Images")
        flt.add_pixbuf_formats()
        chooser.add_filter(flt)
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pictures:
            chooser.set_current_folder(pictures)
        path = chooser.get_filename() if chooser.run() == Gtk.ResponseType.ACCEPT else None
        chooser.destroy()
        if not path:
            return
        try:
            self._set(prepare_avatar(path))
        except GLib.Error as e:
            dlg = Gtk.MessageDialog(transient_for=self.dialog, modal=True,
                                    message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.CLOSE,
                                    text="Impossible d'utiliser cette image")
            dlg.format_secondary_text(e.message)
            dlg.run()
            dlg.destroy()


class WhatsNewDialog(Gtk.Dialog):
    """Résumé des nouveautés (CHANGELOG.md), version par version, la plus récente en haut."""

    def __init__(self, parent, releases, since=None):
        super().__init__(title="Nouveautés", transient_for=parent, modal=True, use_header_bar=True)
        self.set_default_size(560, -1)
        self.get_style_context().add_class("qchat")
        box = self.get_content_area()
        box.set_spacing(10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        title = _label(f"Bienvenue dans QChat {__version__}", "update-title")
        box.pack_start(title, False, False, 0)
        if since:
            count = len(releases)
            intro = (f"Voici ce qui a changé depuis la {since}"
                     + (f", en {count} versions." if count > 1 else "."))
        else:
            intro = "Toutes les évolutions de QChat, de la plus récente à la plus ancienne."
        box.pack_start(_label(intro, "subtitle", wrap=True), False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=4)
        for r in releases:
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            head = Gtk.Box(spacing=8)
            head.pack_start(_label(f"Version {r.version}", "news-version"), False, False, 0)
            if r.date:
                head.pack_start(_label(_news_date(r.date), "subtitle"), False, False, 0)
            section.pack_start(head, False, False, 0)
            for item in r.items:
                line = Gtk.Box(spacing=8)
                bullet = _label("•", "news-bullet")
                bullet.set_valign(Gtk.Align.START)
                line.pack_start(bullet, False, False, 0)
                text = Gtk.Label(xalign=0, wrap=True, selectable=False)
                text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                text.set_max_width_chars(70)
                text.set_markup(changelog.to_markup(item))
                line.pack_start(text, True, True, 0)
                section.pack_start(line, False, False, 0)
            content.pack_start(section, False, False, 0)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        # Les textes à la ligne n'ont pas de hauteur naturelle fiable : estimation, puis défilement.
        lines = sum(2 + sum(len(item) // 68 + 1 for item in r.items) for r in releases)
        scroll.set_min_content_height(min(440, 22 * lines + 14 * len(releases)))
        scroll.add(content)
        box.pack_start(scroll, True, True, 0)
        ok = Gtk.Button(label="C'est parti !")
        ok.get_style_context().add_class("suggested-action")
        ok.set_halign(Gtk.Align.END)
        ok.connect("clicked", lambda _b: self.destroy())
        box.pack_start(ok, False, False, 0)
        self.connect("response", lambda d, _r: d.destroy())
        box.show_all()


def _news_date(date):
    """« 2026-11-24 » → « 24/11/2026 »."""
    parts = date.split("-")
    return "/".join(reversed(parts)) if len(parts) == 3 else date


class UpdateDialog(Gtk.Dialog):
    """☰ › Rechercher des mises à jour : compare la version en cours à celle du dépôt, propose
    d'installer la nouvelle (sans mot de passe grâce à la règle polkit du paquet) puis redémarre."""

    def __init__(self, app):
        super().__init__(title="Mises à jour", transient_for=app.win, modal=True, use_header_bar=True)
        self.app = app
        self.set_default_size(400, -1)
        self.get_style_context().add_class("qchat")
        box = self.get_content_area()
        box.set_spacing(12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        row = Gtk.Box(spacing=12)
        self.spinner = Gtk.Spinner()
        row.pack_start(self.spinner, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.heading = _label("", "update-title", wrap=True)
        self.detail = _label("", "subtitle", wrap=True)
        self.heading.set_max_width_chars(40)
        self.detail.set_max_width_chars(48)
        texts.pack_start(self.heading, False, False, 0)
        texts.pack_start(self.detail, False, False, 0)
        row.pack_start(texts, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        box.pack_start(self.actions, False, False, 0)
        self.connect("response", lambda d, _r: d.destroy())
        self._timer = None
        self.connect("destroy", lambda *_a: self._timer and GLib.source_remove(self._timer))

    # -- états ---------------------------------------------------------------------
    def _show(self, title, detail="", busy=False, buttons=()):
        self.heading.set_text(title)
        self.detail.set_text(detail)
        self.detail.set_visible(bool(detail))
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        for child in self.actions.get_children():
            child.destroy()
        for label, callback, primary in buttons:
            b = Gtk.Button(label=label)
            if primary:
                b.get_style_context().add_class("suggested-action")
            b.connect("clicked", lambda _b, cb=callback: cb())
            self.actions.pack_start(b, False, False, 0)
            b.show()
        self.actions.set_visible(bool(buttons))
        self.resize(400, 1)

    def _close_button(self, label="Fermer"):
        return (label, self.destroy, False)

    def run_check(self):
        self.show_all()
        self._show("Recherche des mises à jour…", busy=True)
        pending = updater.pending_update()
        if pending:      # déjà installée (par le minuteur ou à la main) : il suffit de redémarrer
            self._offer_restart(pending)
            return

        def work():
            try:
                latest, error = updater.latest_available(), None
            except updater.UpdateError as e:
                latest, error = None, str(e)
            GLib.idle_add(lambda: self._checked(latest, error) and False)

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, latest, error):
        if self.get_window() is None:
            return
        if error:
            self._show("Recherche impossible", error, buttons=[self._close_button()])
        elif not updater.is_newer(latest):
            self._show("QChat est à jour", f"Vous utilisez la dernière version, la {__version__}.",
                       buttons=[self._close_button()])
        elif not updater.can_install():
            self._show(f"QChat {latest} est disponible",
                       f"Vous avez la {__version__}. Cette installation de QChat ne se met pas à jour "
                       "d'elle-même : installez le paquet depuis le dépôt apt.",
                       buttons=[self._close_button()])
        else:
            self._show(f"QChat {latest} est disponible",
                       f"Vous avez la {__version__}. L'installation prend quelques secondes, puis QChat "
                       "redémarre ; vos conversations sont conservées.",
                       buttons=[self._close_button("Plus tard"),
                                ("Installer et redémarrer", lambda: self._install(latest), True)])

    def _install(self, version):
        self._show(f"Installation de QChat {version}…", "Téléchargement depuis le dépôt de QChat.", busy=True)

        def work():     # dans un fil : le bureau peut demander un mot de passe sans figer QChat
            try:
                updater.start_install()
                error = None
            except updater.UpdateError as e:
                error = str(e)
            GLib.idle_add(lambda: self._install_started(version, error) and False)

        threading.Thread(target=work, daemon=True).start()

    def _install_started(self, version, error):
        if self.get_window() is None:
            return
        if error:
            self._show("Installation impossible", error, buttons=[self._close_button()])
            return
        self._started = time.time()
        self._timer = GLib.timeout_add(1000, self._poll, version)

    def _poll(self, version):
        state, result = updater.install_state()
        if state in ("activating", "active", "reloading") and time.time() - self._started < 900:
            return True
        self._timer = None
        installed = updater.pending_update()
        if installed:
            self._show(f"QChat {installed} est installé", "Redémarrage…", busy=True)
            GLib.timeout_add(800, lambda: self.app._restart() and False)
        else:
            self._show("La mise à jour n'a pas abouti",
                       "Détail : sudo journalctl -u qchat-update.service"
                       + ("" if result == "success" else f" (résultat : {result})"),
                       buttons=[self._close_button()])
        return False

    def _offer_restart(self, version):
        self._show(f"QChat {version} est déjà installé",
                   f"Vous utilisez encore la {__version__} : redémarrez pour passer à la nouvelle version.",
                   buttons=[self._close_button("Plus tard"),
                            ("Redémarrer", self.app._restart, True)])


def custom_emoji_image(path):
    """(octets, aperçu) pour un emoji personnalisé : un GIF animé léger est gardé tel quel, sinon
    l'image est réduite à 128 px au plus et enregistrée en PNG. ValueError si c'est impossible."""
    try:
        if os.path.getsize(path) > 40 * 1024 * 1024:
            raise ValueError("Ce fichier est trop lourd.")
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ValueError(e.strerror or str(e)) from e
    if gifdecode.is_gif(raw) and len(raw) <= MAX_CEMOJI:
        anim, still = gifdecode.load(path)
        if anim is not None:
            return raw, still
    try:
        pb = load_pixbuf(path)
    except GLib.Error as e:
        raise ValueError("Ce fichier n'est pas une image lisible.") from e
    pb = pb.apply_embedded_orientation() or pb
    for side in (128, 96, 64, 48):
        small = pb.scale_simple(*fit(pb.get_width(), pb.get_height(), side, side), GdkPixbuf.InterpType.HYPER)
        ok, data = small.save_to_bufferv("png", [], [])
        if ok and len(data) <= MAX_CEMOJI:
            return bytes(data), small
    raise ValueError("Cette image est trop détaillée pour un emoji.")


def ask_emoji_name(parent, preview, name, action):
    """Demande le raccourci d'un emoji personnalisé ; None si annulé."""
    dlg = Gtk.Dialog(title="Emoji personnalisé", transient_for=parent, modal=True, use_header_bar=True)
    dlg.add_button("Annuler", Gtk.ResponseType.CANCEL)
    ok = dlg.add_button(action, Gtk.ResponseType.OK)
    ok.get_style_context().add_class("suggested-action")
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.get_style_context().add_class("qchat")
    area = dlg.get_content_area()
    row = Gtk.Box(spacing=16, margin=18)
    if preview is not None:
        w, h = fit(preview.get_width(), preview.get_height(), 72, 72, upscale=True)
        row.pack_start(Gtk.Image.new_from_pixbuf(preview.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)),
                       False, False, 0)
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
    col.pack_start(_label("Nom de l'emoji"), False, False, 0)
    entry = Gtk.Entry(text=name, activates_default=True, max_length=32, width_chars=24)
    col.pack_start(entry, False, False, 0)
    hint = _label("", "meta")
    col.pack_start(hint, False, False, 0)
    entry.connect("changed", lambda e: hint.set_text(f"Recherche et anciennes versions : :{cemoji_name(e.get_text())}:"))
    entry.emit("changed")
    row.pack_start(col, True, True, 0)
    area.pack_start(row, True, True, 0)
    area.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    return text if resp == Gtk.ResponseType.OK and text.strip() else None


class EmojiPopover(Gtk.Popover):
    """Tous les emoji, rangés par catégorie, avec recherche en français et emoji récents.
    Un clic insère l'emoji dans la zone de saisie, à l'emplacement du curseur."""

    RECENT_MAX = 24

    def __init__(self, win, on_pick=None, accept=None):
        super().__init__()
        self.win, self.core = win, win.core
        self.on_pick = on_pick          # par défaut : insertion dans la zone de saisie
        self.accept = accept            # filtre optionnel (réactions : emoji acceptés par le protocole)
        self.get_style_context().add_class("emoji-popover")
        self._built = False
        self._sections = []         # (titre, flowbox, en-tête, onglet)
        self.custom_flow = self.custom_header = None
        self._words = []            # mots recherchés (normalisés)
        win._emoji_pickers.append(self)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        box.set_size_request(-1, 420)
        self.search = Gtk.SearchEntry(placeholder_text="Rechercher un emoji")
        self.search.connect("search-changed", lambda _e: self._filter())
        self.search.connect("activate", lambda _e: self._insert_first())
        box.pack_start(self.search, False, False, 0)
        self.tabs = Gtk.Box(spacing=0, homogeneous=True)
        self.tabs.get_style_context().add_class("emoji-tabs")
        box.pack_start(self.tabs, False, False, 0)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.scroll.add(self.list)
        overlay = Gtk.Overlay()
        overlay.add(self.scroll)
        self.empty = _label("Aucun emoji trouvé", "subtitle", xalign=0.5)
        self.empty.set_halign(Gtk.Align.CENTER)
        self.empty.set_valign(Gtk.Align.CENTER)
        self.empty.set_no_show_all(True)
        overlay.add_overlay(self.empty)
        overlay.set_overlay_pass_through(self.empty, True)
        box.pack_start(overlay, True, True, 0)
        self.hint = _label("", "emoji-hint")
        self.hint.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(self.hint, False, False, 0)
        self.add(box)
        box.show_all()
        self.connect("show", lambda *_a: self._on_show())

    # -- construction (au premier affichage : ~1 900 emoji) -----------------------
    def _on_show(self):
        if not self._built or getattr(self, "_steps", None) is not None:
            self._build()
        self._fill_recent()
        self.search.set_text("")
        self._filter()
        self.scroll.get_vadjustment().set_value(0)
        self.search.grab_focus()

    # -- emojis personnalisés -----------------------------------------------------------
    def refresh_custom(self):
        """(Re)remplit la section « Emojis personnalisés », en fin de liste."""
        if self.custom_flow is None:
            return                      # pas encore construit : rempli au premier affichage
        for child in self.custom_flow.get_children():
            child.destroy()
        add = Gtk.FlowBoxChild(tooltip_text="Créer un emoji à partir d'une image")
        plus = Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        plus.get_style_context().add_class("emoji-add")
        add.add(plus)
        add.token, add.name = None, ""
        add.connect("enter-notify-event", lambda *_a: self.hint.set_text("Nouvel emoji à partir d'une image…")
                    and False)
        self.custom_flow.add(add)
        for token, name, _path in self.core.custom_emojis():
            art = self.win._emoji_art(token)
            child = Gtk.FlowBoxChild(tooltip_text=f":{name}:")
            ev = Gtk.EventBox()
            if isinstance(art, GdkPixbuf.Pixbuf):
                w, h = fit(art.get_width(), art.get_height(), 28, 28, upscale=True)
                ev.add(Gtk.Image.new_from_pixbuf(art.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)))
            else:
                ev.add(Gtk.Label(label=art))
            child.add(ev)
            child.token, child.name = token, name
            child.connect("enter-notify-event", lambda *_a, n=name: self.hint.set_text(f":{n}:  —  clic droit pour "
                                                                                      "renommer ou retirer") and False)
            ev.connect("button-press-event", lambda w, e, c=child: e.button == 3 and self._custom_menu(w, c))
            child.get_style_context().add_class("custom-emoji-cell")
            self.custom_flow.add(child)
        self.custom_flow.show_all()
        self.custom_note.set_visible(len(self.custom_flow.get_children()) == 1)
        self._filter()

    def _custom_visible(self, child):
        if child.token is None:
            return not self._words
        name = emojis.fold(child.name.replace("_", " "))
        return all(w in name for w in self._words)

    def _on_custom_activated(self, child):
        if child.token is None:
            self._add_custom()
        else:
            self._pick_custom(child.token)

    def _pick_custom(self, token):
        self.popdown()
        if self.on_pick:
            self.on_pick(token)
        else:
            self.win._send_sticker(token)      # depuis la zone de saisie : envoyé seul, en grand

    def _custom_menu(self, widget, child):
        pop = Gtk.Popover(relative_to=widget)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=4)
        for text, fn in (("Renommer…", lambda: self._rename_custom(child.token, child.name)),
                         ("Retirer de mes emojis", lambda: self.core.remove_custom_emoji(child.token))):
            row = Gtk.ModelButton(text=text, centered=False)
            row.connect("clicked", lambda _b, fn=fn: (pop.popdown(), fn()))
            content.pack_start(row, False, False, 0)
        pop.add(content)
        content.show_all()
        pop.popup()
        return True

    def _add_custom(self):
        self.popdown()
        chooser = Gtk.FileChooserNative(title="Choisir une image pour l'emoji", transient_for=self.win,
                                        action=Gtk.FileChooserAction.OPEN, accept_label="Choisir")
        flt = Gtk.FileFilter()
        flt.set_name("Images")
        flt.add_pixbuf_formats()
        flt.add_mime_type("image/gif")
        chooser.add_filter(flt)
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pictures:
            chooser.set_current_folder(pictures)
        path = chooser.get_filename() if chooser.run() == Gtk.ResponseType.ACCEPT else None
        chooser.destroy()
        if not path:
            return
        try:
            data, preview = custom_emoji_image(path)
        except ValueError as e:
            self.win._error("Impossible d'utiliser cette image", str(e))
            return
        name = ask_emoji_name(self.win, preview, cemoji_name(os.path.basename(path)), "Ajouter")
        if name is None:
            return
        try:
            self.core.add_custom_emoji(data, name)
        except ValueError as e:
            self.win._error("Impossible d'ajouter cet emoji", str(e))
            return
        self.popup()
        GLib.timeout_add(250, lambda: self._jump(self.custom_header) or False)

    def _rename_custom(self, token, name):
        art = self.win._emoji_art(token)
        new = ask_emoji_name(self.win, art if isinstance(art, GdkPixbuf.Pixbuf) else None, name, "Renommer")
        if new:
            self.core.rename_custom_emoji(token, new)

    def _section(self, title, icon, custom=False):
        header = _label(title, "emoji-section")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=9, max_children_per_line=9,
                           column_spacing=0, row_spacing=0,
                           activate_on_single_click=True, valign=Gtk.Align.START)
        if custom:
            flow.connect("child-activated", lambda _f, child: self._on_custom_activated(child))
            flow.set_filter_func(self._custom_visible)
        else:
            flow.connect("child-activated", lambda _f, child: self._insert(child.emoji))
            flow.set_filter_func(self._visible)
        self.list.pack_start(header, False, False, 0)
        self.list.pack_start(flow, False, False, 0)
        tab = Gtk.Button(label=icon, relief=Gtk.ReliefStyle.NONE, tooltip_text=title)
        tab.get_style_context().add_class("emoji-tab")
        tab.connect("clicked", lambda _b: self._jump(header))
        self.tabs.pack_start(tab, False, True, 0)
        self._sections.append((title, flow, header, tab))
        return flow

    def _cell(self, emoji):
        child = Gtk.FlowBoxChild()
        lbl = Gtk.Label(label=emoji.char)
        lbl.get_style_context().add_class("emoji-cell")
        child.add(lbl)
        child.emoji = emoji
        child.set_tooltip_text(emoji.name)
        child.connect("enter-notify-event", lambda *_a: self.hint.set_text(f"{emoji.char}  {emoji.name}")
                      and False)
        return child

    def prebuild(self):
        """Construit la liste par petits morceaux pendant que l'application est inactive : la première
        ouverture du sélecteur est alors immédiate (sinon ~250 ms pour ~1 900 emoji)."""
        if self._built or getattr(self, "_steps", None) is not None:
            return
        self._steps = self._build_steps()

        def step():
            if self._steps is None:
                return False
            try:
                next(self._steps)
                return True
            except StopIteration:
                return False
        GLib.idle_add(step, priority=GLib.PRIORITY_LOW)

    def _build(self):
        if self._built and getattr(self, "_steps", None) is None:
            return
        if getattr(self, "_steps", None) is None:
            self._steps = self._build_steps()
        for _ in self._steps:       # termine d'un coup (ouverture avant la fin de la préparation)
            pass

    def _build_steps(self):
        self._built = True
        self.recent_flow = self._section("Récents", "🕓")
        flows = {}
        for group, icon, title in emojis.GROUPS:
            flows[group] = self._section(title, icon)
        yield
        for i, e in enumerate(emojis.load()):
            flow = flows.get(e.group)
            if flow is not None and self._renderable(e.char) and (not self.accept or self.accept(e.char)):
                flow.add(self._cell(e))
            if i % 120 == 119:
                yield
        # Mes emojis personnalisés en fin de liste, avec leur onglet pour y sauter directement.
        self.custom_flow = self._section("Emojis personnalisés", "⭐", custom=True)
        self.custom_header = self._sections[-1][2]
        self.custom_note = _label("Cliquez sur + pour créer un emoji à partir d'une image.", "emoji-hint")
        self.custom_note.set_margin_start(4)
        self.custom_note.set_no_show_all(True)
        self.list.pack_start(self.custom_note, False, False, 0)
        self.list.show_all()
        self.tabs.show_all()
        self.refresh_custom()
        yield
        # Calcule aussi styles et tailles d'avance : la première ouverture n'a plus qu'à dessiner.
        if self.get_relative_to() is None:
            self.set_relative_to(self.win.input)
        self.get_child().get_preferred_size()
        self._steps = None

    def _renderable(self, char):
        """Écarte les emoji que la police du poste ne sait pas dessiner en un seul glyphe
        (séquences récentes affichées en plusieurs morceaux)."""
        if not hasattr(self, "_measure"):
            self._measure = self.create_pango_layout("")
            self._measure.set_font_description(Pango.FontDescription.from_string("22px"))
            self._measure.set_text("😀", -1)
            self._ref_width = self._measure.get_pixel_size()[0] or 28
        self._measure.set_text(char, -1)
        return self._measure.get_pixel_size()[0] <= self._ref_width * 1.4

    def _recent(self):
        try:
            return [c for c in json.loads(self.core.store.get_setting("emoji_recent", "[]")) if isinstance(c, str)]
        except ValueError:
            return []

    def _fill_recent(self):
        for child in self.recent_flow.get_children():
            child.destroy()
        by_char = {e.char: e for e in emojis.load()}
        for char in self._recent():
            e = by_char.get(char)
            if e and (not self.accept or self.accept(e.char)):
                self.recent_flow.add(self._cell(e))
        self.recent_flow.show_all()

    # -- recherche -------------------------------------------------------------------
    def _visible(self, child):
        words = self._words
        return not words or child.emoji.matches(words)

    def _filter(self):
        query = self.search.get_text().strip()
        self._words = emojis.fold(query).split()
        found = False
        for title, flow, header, tab in self._sections:
            flow.invalidate_filter()
            recent = title == "Récents"
            visible = self._custom_visible if flow is self.custom_flow else self._visible
            count = sum(1 for c in flow.get_children() if visible(c))
            show = count > 0 and not (recent and self._words)
            flow.set_visible(show)
            header.set_visible(show)
            tab.set_sensitive(show)
            found = found or show
        self.empty.set_visible(not found)
        self.tabs.set_visible(not self._words)
        if self.custom_flow is not None:
            self.custom_note.set_visible(not self._words and len(self.custom_flow.get_children()) == 1)
        self.hint.set_text("")

    def _first_visible(self):
        """Premier résultat : un emoji, ou la case d'un emoji personnalisé."""
        for title, flow, _h, _t in self._sections:
            if flow.get_visible() and not (title == "Récents" and self._words):
                for child in flow.get_children():
                    if flow is self.custom_flow:
                        if child.token and self._custom_visible(child):
                            return child
                    elif self._visible(child):
                        return child.emoji
        return None

    def _jump(self, header):
        alloc = header.get_allocation()
        self.scroll.get_vadjustment().set_value(alloc.y)

    # -- insertion -------------------------------------------------------------------
    def _insert_first(self):
        e = self._first_visible()
        if isinstance(e, Gtk.FlowBoxChild):
            self._pick_custom(e.token)
        elif e:
            self._insert(e)

    def remember(self, char):
        """Place ``char`` en tête des emoji récents."""
        recent = [char] + [c for c in self._recent() if c != char]
        self.core.store.set_setting("emoji_recent", json.dumps(recent[:self.RECENT_MAX]))

    def _insert(self, emoji):
        self.remember(emoji.char)
        self.popdown()
        if self.on_pick:
            self.on_pick(emoji.char)
            return
        buf = self.win.input.get_buffer()
        buf.delete_selection(True, True)
        buf.insert_at_cursor(emoji.char)
        self.win.input.grab_focus()


class MentionPopover(Gtk.Popover):
    """Suggestions « @ » dans un groupe : membres dont le nom commence par ce qui est tapé.
    ↑/↓ pour choisir, Entrée ou Tab pour insérer, Échap pour fermer."""

    MAX = 6

    def __init__(self, win):
        super().__init__(relative_to=win.input)
        self.win = win
        self.set_modal(False)
        self.set_position(Gtk.PositionType.TOP)
        self.get_style_context().add_class("mention-popover")
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.list.connect("row-activated", lambda _l, row: self._choose(row))
        self.add(self.list)
        self._start = None          # position du « @ » dans le tampon
        self._closed_at = None      # « @ » pour lequel l'utilisateur a fermé les suggestions

    def _query(self):
        """(position du @, texte tapé après) si le curseur suit un « @… », sinon None."""
        buf = self.win.input.get_buffer()
        cur = buf.get_iter_at_mark(buf.get_insert())
        line = cur.copy()
        line.set_line_offset(0)
        before = buf.get_text(line, cur, False)
        at = before.rfind("@")
        if at < 0 or (at > 0 and not before[at - 1].isspace()):
            return None
        query = before[at + 1:]
        if len(query) > 40 or "\n" in query or query.count(" ") > 2:
            return None
        return line.get_offset() + at, query

    def _members(self):
        conv = self.win.current or ""
        if not conv.startswith("g:"):
            return []
        store = self.win.core.store
        people = [store.get_contact(i) for i in store.group_member_ids(conv[2:])]
        return sorted((c for c in people if c), key=lambda c: c.name.lower())

    def update(self):
        found = self._query()
        if not found or found[0] == self._closed_at:
            if found is None:
                self._closed_at = None
            self.popdown()
            return False
        start, query = found
        q = emojis.fold(query)
        matches = [c for c in self._members()
                   if emojis.fold(c.name).startswith(q)
                   or any(emojis.fold(w).startswith(q) for w in c.name.split())]
        if not matches:
            self.popdown()
            return False
        self._start = start
        for row in self.list.get_children():
            row.destroy()
        for c in matches[:self.MAX]:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=8, margin=4)
            box.pack_start(_avatar(c.name, small=True, image=self.win.core.avatar_path(c.conv)), False, False, 0)
            box.pack_start(_label(c.name), False, False, 0)
            row.add(box)
            row.contact = c
            self.list.add(row)
        self.list.show_all()
        self.list.select_row(self.list.get_row_at_index(0))
        # Le popover pointe vers le « @ » tapé.
        buf = self.win.input.get_buffer()
        rect = self.win.input.get_iter_location(buf.get_iter_at_offset(start))
        x, y = self.win.input.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
        rect.x, rect.y = x, y
        self.set_pointing_to(rect)
        if not self.get_visible():
            self.popup()
        return False

    def handle_key(self, event):
        if not self.get_visible():
            return False
        key = event.keyval
        row = self.list.get_selected_row()
        rows = self.list.get_children()
        if key in (Gdk.KEY_Down, Gdk.KEY_Up) and rows:
            i = rows.index(row) if row in rows else 0
            i = (i + (1 if key == Gdk.KEY_Down else -1)) % len(rows)
            self.list.select_row(rows[i])
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab) and row:
            self._choose(row)
            return True
        if key == Gdk.KEY_Escape:
            self._closed_at = self._start
            self.popdown()
            return True
        return False

    def _choose(self, row):
        found = self._query()
        if not found:
            self.popdown()
            return
        start, query = found
        buf = self.win.input.get_buffer()
        begin = buf.get_iter_at_offset(start)
        end = buf.get_iter_at_offset(start + 1 + len(query))
        buf.delete(begin, end)
        buf.insert(buf.get_iter_at_offset(start), f"@{row.contact.name} ")
        self.popdown()
        self.win.input.grab_focus()


class EmojiCompletion(Gtk.Popover):
    """« :mot » dans la saisie : emoji correspondants au fil de la frappe (après 2 lettres).
    ↑/↓ pour choisir, Entrée ou Tab pour insérer, Échap pour fermer ; « :mot: » insère directement
    le premier. Rien ne se déclenche au milieu d'un mot (« 10:30 », « https:// »)."""

    MAX = 6

    def __init__(self, win):
        super().__init__(relative_to=win.input)
        self.win = win
        self.set_modal(False)
        self.set_position(Gtk.PositionType.TOP)
        self.get_style_context().add_class("mention-popover")
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.list.connect("row-activated", lambda _l, row: self._choose(row))
        self.add(self.list)
        self._start = None          # position du « : » dans le tampon
        self._closed_at = None      # « : » pour lequel l'utilisateur a fermé les suggestions

    def _before(self):
        buf = self.win.input.get_buffer()
        cur = buf.get_iter_at_mark(buf.get_insert())
        line = cur.copy()
        line.set_line_offset(0)
        return line.get_offset(), buf.get_text(line, cur, False)

    def _query(self):
        line_start, before = self._before()
        found = emojis.completion_query(before)
        return (line_start + found[0], found[1]) if found else None

    def _matches(self, query):
        picker = self.win.emoji_popover
        return emojis.complete(query, recent=picker._recent(), limit=self.MAX, accept=picker._renderable)

    def dismiss_here(self):
        """Ne plus proposer d'emoji pour le « : » sous le curseur (après une annulation)."""
        found = self._query()
        self._closed_at = found[0] if found else None
        self.popdown()

    def update(self, changed=False):
        if self.win._auto_busy:
            return False
        if changed:
            # « :feu: » : remplacé directement par le premier emoji proposé.
            line_start, before = self._before()
            closed = emojis.closed_shortcode(before)
            if closed and line_start + closed[0] != self._closed_at:
                found = self._matches(closed[1])
                if found:
                    self.popdown()
                    self.win.emoji_popover.remember(found[0].char)
                    self.win.auto_replace(line_start + closed[0], len(closed[1]) + 2, found[0].char)
                    return False
        found = self._query()
        if not found or found[0] == self._closed_at:
            if found is None:
                self._closed_at = None
            self.popdown()
            return False
        start, query = found
        matches = self._matches(query)
        if not matches:
            self.popdown()
            return False
        self._start = start
        for row in self.list.get_children():
            row.destroy()
        for e in matches:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=10, margin=3)
            char = _label(e.char, "completion-emoji")
            box.pack_start(char, False, False, 0)
            box.pack_start(_label(e.name), False, False, 0)
            row.add(box)
            row.emoji = e
            self.list.add(row)
        self.list.show_all()
        self.list.select_row(self.list.get_row_at_index(0))
        buf = self.win.input.get_buffer()
        rect = self.win.input.get_iter_location(buf.get_iter_at_offset(start))
        rect.x, rect.y = self.win.input.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
        self.set_pointing_to(rect)
        if not self.get_visible():
            self.popup()
        return False

    def handle_key(self, event):
        if not self.get_visible():
            return False
        key = event.keyval
        row = self.list.get_selected_row()
        rows = self.list.get_children()
        if key in (Gdk.KEY_Down, Gdk.KEY_Up) and rows:
            i = rows.index(row) if row in rows else 0
            i = (i + (1 if key == Gdk.KEY_Down else -1)) % len(rows)
            self.list.select_row(rows[i])
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab) and row:
            self._choose(row)
            return True
        if key == Gdk.KEY_Escape:
            self._closed_at = self._start
            self.popdown()
            return True
        return False

    def _choose(self, row):
        found = self._query()
        self.popdown()
        if not found:
            return
        start, query = found
        self.win.emoji_popover.remember(row.emoji.char)
        buf = self.win.input.get_buffer()
        self.win._auto_busy = True
        try:
            buf.insert(buf.get_iter_at_offset(start + 1 + len(query)), " ")
        finally:
            self.win._auto_busy = False
        self.win.auto_replace(start, 1 + len(query), row.emoji.char, " ")
        self.win.input.grab_focus()


class GifPopover(Gtk.Popover):
    """Sélecteur de GIF (KLIPY) : tendances au départ, recherche au fil de la frappe, clic pour envoyer.
    Les vignettes sont des images fixes légères, animées au survol."""

    COLS = 3
    CELL = 118
    CELL_H = 92

    def __init__(self, win):
        super().__init__()
        self.win, self.core = win, win.core
        self.get_style_context().add_class("gif-popover")
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qchat-gifs")
        self._gen = 0              # résultats d'une recherche périmée ignorés
        self._timer = None
        self._query = None
        self._page = 1
        self._loading = False
        self._has_next = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        box.set_size_request(self.COLS * (self.CELL + 6) + 30, 430)
        self.search = Gtk.SearchEntry(placeholder_text="Rechercher sur KLIPY")
        self.search.connect("search-changed", lambda _e: self._schedule_search())
        self.search.connect("activate", lambda _e: self._send_first())
        box.pack_start(self.search, False, False, 0)
        self.flow = Gtk.FlowBox(homogeneous=True, selection_mode=Gtk.SelectionMode.NONE,
                                min_children_per_line=self.COLS, max_children_per_line=self.COLS,
                                column_spacing=4, row_spacing=4, valign=Gtk.Align.START)
        self.flow.connect("child-activated", lambda _f, child: self._choose(child.item))
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.add(self.flow)
        self.scroll.connect("edge-reached", lambda _s, pos: pos == Gtk.PositionType.BOTTOM
                            and self._load_more())
        overlay = Gtk.Overlay()
        overlay.add(self.scroll)
        self.status = _label("", "subtitle", xalign=0.5, wrap=True)
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_valign(Gtk.Align.CENTER)
        self.status.set_max_width_chars(36)
        self.status.set_no_show_all(True)
        overlay.add_overlay(self.status)
        overlay.set_overlay_pass_through(self.status, True)
        box.pack_start(overlay, True, True, 0)
        box.pack_start(_label("Propulsé par KLIPY", "gif-credit", xalign=1.0), False, False, 0)
        self.add(box)
        box.show_all()
        self.connect("show", lambda *_a: self._on_show())
        win.connect("destroy", lambda *_a: self._pool.shutdown(wait=False, cancel_futures=True))

    @property
    def customer_id(self):
        """Identifiant anonyme et aléatoire de ce poste, demandé par KLIPY (aucune donnée personnelle)."""
        cid = self.core.store.get_setting("gif_customer_id", "")
        if not cid:
            cid = uuid_hex() + uuid_hex()
            self.core.store.set_setting("gif_customer_id", cid)
        return cid

    # -- recherche -------------------------------------------------------------
    def _on_show(self):
        self.search.grab_focus()
        if self._query is None or not self.flow.get_children():
            self._run_search()

    def _schedule_search(self):
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(350, self._run_search)

    def _clear(self):
        self._gen += 1
        for child in self.flow.get_children():
            self.flow.remove(child)
            child.destroy()

    def _set_status(self, text):
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def _run_search(self):
        self._timer = None
        self._query = self.search.get_text().strip()
        self._page = 1
        self._has_next = False
        self._clear()
        self.scroll.get_vadjustment().set_value(0)
        self._fetch()
        return False

    def _load_more(self):
        if not self._loading and self._has_next and self.flow.get_children():
            self._fetch()

    def _fetch(self):
        gen, query, page, customer = self._gen, self._query, self._page, self.customer_id
        self._loading = True
        if page == 1:
            self._set_status("Chargement…")

        def work():
            try:
                (results, has_next), error = gifs.search(query, page, customer), None
            except gifs.GifError as e:
                results, has_next, error = [], False, str(e)
            GLib.idle_add(lambda: self._on_results(gen, results, has_next, error) and False)

        self._pool.submit(work)

    def _on_results(self, gen, results, has_next, error):
        if gen != self._gen:
            return
        self._loading = False
        if error:
            self._set_status(error)
            return
        self._page += 1
        self._has_next = has_next
        if not results and not self.flow.get_children():
            self._set_status(f"Aucun GIF pour « {self._query} »." if self._query else "Aucun GIF.")
            return
        self._set_status("")
        for item in results:
            self._add_cell(gen, item)

    # -- vignettes -------------------------------------------------------------
    def _size(self, _item):
        return self.CELL, self.CELL_H

    def _add_cell(self, gen, item):
        w, h = self._size(item)
        child = Gtk.FlowBoxChild()
        child.item = item
        child.set_tooltip_text(item["title"])
        ev = Gtk.EventBox(halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        ev.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        frame = Gtk.Stack()          # « still » : image fixe ; « anim » : GIF animé (au survol)
        frame.set_size_request(w, h)
        frame.get_style_context().add_class("gif-cell")
        ev.add(frame)
        child.add(ev)
        self.flow.add(child)
        child.show_all()
        ev.connect("enter-notify-event", lambda *_a: self._animate(gen, item, frame))
        ev.connect("leave-notify-event", lambda _w, e: e.detail == Gdk.NotifyType.INFERIOR
                   or frame.get_child_by_name("still") is None
                   or frame.set_visible_child_name("still"))
        url, kind = (item["still"], "image") if item["still"] else (item["preview"], "gif")

        def work():
            try:
                data = gifs.download(url, gifs.MAX_PREVIEW, kind=kind)
            except gifs.GifError:
                return
            GLib.idle_add(lambda: self._put(gen, frame, data, w, h, "still") and False)

        self._pool.submit(work)

    def _put(self, gen, frame, data, w, h, name):
        if gen != self._gen or frame.get_parent() is None or frame.get_child_by_name(name):
            return
        anim, still = _animation_from_bytes(data)
        if anim:
            img = AnimatedImage(anim, None, transform=lambda pb: cover(pb, w, h))
        elif still:
            img = Gtk.Image.new_from_pixbuf(cover(still, w, h))
        else:
            return
        frame.add_named(img, name)
        img.show()
        if name == "anim" or frame.get_child_by_name("anim") is None:
            frame.set_visible_child_name(name)

    def _animate(self, gen, item, frame):
        if frame.get_child_by_name("anim"):
            frame.set_visible_child_name("anim")
            return
        if not item["preview"] or getattr(frame, "loading", False):
            return
        frame.loading = True
        w, h = self._size(item)

        def work():
            try:
                data = gifs.download(item["preview"], gifs.MAX_PREVIEW)
            except gifs.GifError:
                return
            GLib.idle_add(lambda: self._put(gen, frame, data, w, h, "anim") and False)

        self._pool.submit(work)

    def _send_first(self):
        children = self.flow.get_children()
        if children:
            self._choose(children[0].item)

    def _choose(self, item):
        self.popdown()
        self.win._send_gif(item)


class MemberPicker(Gtk.ScrolledWindow):
    """Liste de contacts à cocher. Seuls les contacts dont la clé est connue sont sélectionnables."""

    def __init__(self, core, exclude):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.set_min_content_height(200)
        self.set_shadow_type(Gtk.ShadowType.IN)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6)
        self._checks = {}
        contacts = [c for c in core.store.contacts() if c.id not in exclude]
        for c in contacts:
            cb = Gtk.CheckButton(label=c.name)
            if not c.vk:
                cb.set_sensitive(False)
                cb.set_label(f"{c.name} (jamais joint)")
                cb.set_tooltip_text("Sa clé de sécurité est inconnue tant qu'il n'a pas été joint une fois.")
            self._checks[c.id] = cb
            box.pack_start(cb, False, False, 0)
        if not contacts:
            box.pack_start(_label("Aucun contact disponible.", "subtitle"), False, False, 0)
        self.add(box)

    def selected(self):
        return [cid for cid, cb in self._checks.items() if cb.get_active()]


class ScanDialog:
    """Découverte active : sonde une plage d'IP et liste les postes où QChat répond."""

    def __init__(self, win):
        self.win, self.core = win, win.core
        self.cancel = None
        self.closed = False
        self.rows = {}      # vk -> (label sous-titre, boîte d'action)
        dlg, box = _dialog(win, "Découverte du réseau")
        dlg.set_modal(False)
        dlg.set_default_size(560, 520)
        self.dlg = dlg
        box.pack_start(_label("Plages à analyser (réseau, plage ou IP, séparés par des virgules)"),
                       False, False, 0)
        line = Gtk.Box(spacing=6)
        nets = netutil.local_networks()
        self.entry = Gtk.Entry(text=", ".join(str(n) for n in nets),
                               placeholder_text="ex. 10.102.0.0/23, 10.102.4.1-50")
        self.entry.connect("activate", self._toggle)
        self.button = Gtk.Button(label="Analyser")
        self.button.get_style_context().add_class("suggested-action")
        self.button.connect("clicked", self._toggle)
        line.pack_start(self.entry, True, True, 0)
        line.pack_start(self.button, False, False, 0)
        box.pack_start(line, False, False, 0)
        self.progress = Gtk.ProgressBar(show_text=True, text="")
        box.pack_start(self.progress, False, False, 0)
        self.error = _label("", "warning", wrap=True)
        box.pack_start(self.error, False, False, 0)

        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.placeholder = _label("Lancez l'analyse pour trouver les postes où QChat est ouvert.",
                                  "subtitle", xalign=0.5, wrap=True)
        self.placeholder.set_margin_top(30)
        self.placeholder.show()
        self.results.set_placeholder(self.placeholder)
        box.pack_start(_scrolled(self.results, 260), True, True, 0)
        box.pack_start(_label("Seuls les postes où QChat est lancé répondent. L'analyse est discrète : "
                              "rien ne s'affiche chez les personnes trouvées.", "subtitle", wrap=True),
                       False, False, 0)
        dlg.connect("response", lambda *_a: self._close())
        dlg.connect("destroy", lambda *_a: self._stop())
        dlg.show_all()
        if nets:
            self._start()

    # -- analyse ------------------------------------------------------------
    def _toggle(self, *_a):
        if self.cancel:
            self._stop()
        else:
            self._start()

    def _start(self):
        try:
            hosts = netutil.parse_targets(self.entry.get_text())
        except ValueError as e:
            self.error.set_text(str(e))
            return
        if not hosts:
            self.error.set_text("Indiquez au moins une plage d'adresses.")
            return
        self.error.set_text("")
        self.cancel = cancel = threading.Event()
        self.button.set_label("Arrêter")
        self.button.get_style_context().remove_class("suggested-action")
        self.progress.set_fraction(0)
        self.progress.set_text(f"0 / {len(hosts)} adresses")
        self.placeholder.set_text("Analyse en cours…")

        def idle(fn):
            return lambda *a: GLib.idle_add(lambda: fn(cancel, *a) and False)

        threading.Thread(target=self._run, daemon=True,
                         args=(hosts, cancel, idle(self._on_progress), idle(self._on_found))).start()

    def _run(self, hosts, cancel, on_progress, on_found):
        self.core.scan(hosts, on_progress=on_progress, on_found=on_found, cancel=cancel)
        GLib.idle_add(lambda: self._finished(cancel) and False)

    def _stop(self):
        if self.cancel:
            self.cancel.set()
            self._finished(self.cancel)

    def _close(self):
        self._stop()
        self.closed = True
        self.dlg.destroy()

    def _finished(self, cancel):
        if self.closed or cancel is not self.cancel:
            return
        self.cancel = None
        self.button.set_label("Analyser")
        self.button.get_style_context().add_class("suggested-action")
        n = len(self.rows)
        self.progress.set_text(f"Terminé — {n} poste{'s' if n > 1 else ''} trouvé{'s' if n > 1 else ''}"
                               if not cancel.is_set() else "Analyse interrompue")
        self.placeholder.set_text("Aucun poste QChat trouvé sur ces adresses.")

    def _on_progress(self, cancel, done, total):
        if cancel is self.cancel:
            self.progress.set_fraction(done / total)
            self.progress.set_text(f"{done} / {total} adresses")

    # -- résultats ------------------------------------------------------------
    def _on_found(self, _cancel, info):
        if self.closed:
            return
        vk = info["vk"]
        if vk in self.rows:     # même personne trouvée sur une autre interface
            sub, _ = self.rows[vk]
            known = sub.get_text().split(" · ")[0].split(", ")
            ips = list(dict.fromkeys(known + info["ips"]))
            sub.set_text(f"{', '.join(ips)} · {fingerprint(vk)[:19]}")
            return
        row = Gtk.Box(spacing=10, margin=8)
        avatar = Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.DND)
        row.pack_start(avatar, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.pack_start(_label(f"<b>{GLib.markup_escape_text(info['name'])}</b>", markup=True),
                         False, False, 0)
        sub = _label(f"{', '.join(info['ips'])} · {fingerprint(vk)[:19]}", "subtitle")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.set_tooltip_text(f"Empreinte complète : {fingerprint(vk)}")
        texts.pack_start(sub, False, False, 0)
        row.pack_start(texts, True, True, 0)
        action = Gtk.Box()
        row.pack_end(action, False, False, 0)
        self.rows[vk] = (sub, action)
        self._set_action(vk, info["contact_id"])
        self.results.add(row)
        self.results.show_all()

    def _set_action(self, vk, cid):
        _, action = self.rows[vk]
        for child in action.get_children():
            action.remove(child)
        if cid:
            btn = Gtk.Button(label="Écrire")
            btn.set_tooltip_text("Déjà dans vos contacts")
            btn.connect("clicked", lambda _b: self.win.open_conv(f"c:{cid}"))
        else:
            btn = Gtk.Button(label="Ajouter")
            btn.get_style_context().add_class("suggested-action")
            btn.connect("clicked", lambda _b: self._add(vk))
        btn.set_valign(Gtk.Align.CENTER)
        action.add(btn)
        action.show_all()

    def _add(self, vk):
        cid = self.core.add_discovered(vk)
        if cid is None:     # ajouté entre-temps (annonce reçue, invitation de groupe…)
            c = self.core.store.contact_by_vk(vk)
            cid = c.id if c else None
        if cid:
            self._set_action(vk, cid)
        else:
            self.error.set_text("Ce poste n'est plus visible, relancez l'analyse.")


class ContactDialog:
    def __init__(self, win, cid):
        self.win, self.core, self.cid = win, win.core, cid

    def run_dialog(self):
        c = self.core.store.get_contact(self.cid)
        dlg, box = _dialog(self.win, "Contact", "Enregistrer")
        self.dlg = dlg
        name = Gtk.Entry(text=c.name, activates_default=True)
        box.pack_start(_label("Nom"), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(_label("Empreinte de sa clé"), False, False, 0)
        fp = fingerprint(c.vk) if c.vk else "inconnue (jamais joint)"
        box.pack_start(_label(fp, "mono", selectable=True), False, False, 0)
        box.pack_start(_label("Adresses IP"), False, False, 0)
        self.ip_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.pack_start(_scrolled(self.ip_list, 110), True, True, 0)
        add_box = Gtk.Box(spacing=6)
        self.new_ip = Gtk.Entry(placeholder_text="Ajouter une IP…")
        self.new_ip.connect("activate", self._add_ip)
        add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        add_btn.connect("clicked", self._add_ip)
        add_box.pack_start(self.new_ip, True, True, 0)
        add_box.pack_start(add_btn, False, False, 0)
        box.pack_start(add_box, False, False, 0)
        self.error = _label("", "warning")
        box.pack_start(self.error, False, False, 0)
        delete = Gtk.Button(label="Supprimer ce contact")
        delete.get_style_context().add_class("destructive-action")
        delete.connect("clicked", self._delete)
        box.pack_start(delete, False, False, 6)
        self._fill_ips()
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK and self.core.store.get_contact(self.cid):
            if name.get_text().strip() and name.get_text() != c.name:
                self.core.rename_contact(self.cid, name.get_text())
        dlg.destroy()

    def _fill_ips(self):
        for row in self.ip_list.get_children():
            self.ip_list.remove(row)
        for ip in self.core.store.ips(self.cid):
            row = Gtk.Box(spacing=6, margin=4)
            row.pack_start(_label(ip, "mono"), True, True, 0)
            rm = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
            rm.set_relief(Gtk.ReliefStyle.NONE)
            rm.set_tooltip_text("Retirer cette IP")
            rm.connect("clicked", lambda _b, ip=ip: self._remove_ip(ip))
            row.pack_end(rm, False, False, 0)
            self.ip_list.add(row)
        self.ip_list.show_all()

    def _add_ip(self, *_a):
        ip = netutil.parse_ip(self.new_ip.get_text())
        if not ip:
            self.error.set_text("Adresse IPv4 invalide.")
            return
        self.error.set_text("")
        self.new_ip.set_text("")
        self.core.add_contact_ip(self.cid, ip)
        self._fill_ips()

    def _remove_ip(self, ip):
        self.core.remove_contact_ip(self.cid, ip)
        self._fill_ips()

    def _delete(self, _b):
        c = self.core.store.get_contact(self.cid)
        if _confirm(self.dlg, f"Supprimer {c.name} ?",
                    "La conversation et les messages en attente seront effacés. "
                    "Il sera aussi retiré de vos groupes (sur ce poste uniquement).", "Supprimer"):
            self.core.delete_contact(self.cid)
            self.dlg.response(Gtk.ResponseType.CANCEL)


class GroupDialog:
    def __init__(self, win, gid):
        self.win, self.core, self.gid = win, win.core, gid

    def run_dialog(self):
        g = self.core.store.get_group(self.gid)
        dlg, box = _dialog(self.win, "Groupe", "Enregistrer")
        dlg.get_style_context().add_class("qchat")
        self.dlg = dlg
        picker = AvatarPicker(dlg, self.core, g.name, self.core.avatar_path(g.conv),
                              "La photo du groupe est visible par tous ses membres.")
        box.pack_start(picker, False, False, 0)
        name = Gtk.Entry(text=g.name, activates_default=True)
        name.connect("changed", lambda e: picker.set_name(e.get_text()))
        box.pack_start(_label("Nom du groupe"), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(_label("Membres"), False, False, 0)
        self.members = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.pack_start(_scrolled(self.members, 160), True, True, 0)
        add = Gtk.Button(label="Ajouter des membres…")
        add.connect("clicked", self._add_members)
        box.pack_start(add, False, False, 0)
        leave = Gtk.Button(label="Quitter le groupe")
        leave.get_style_context().add_class("destructive-action")
        leave.connect("clicked", self._leave)
        box.pack_start(leave, False, False, 6)
        self._fill()
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK and self.core.store.get_group(self.gid):
            if name.get_text().strip() and name.get_text() != g.name:
                self.core.rename_group(self.gid, name.get_text())
            if picker.choice is not None:
                self.core.set_group_avatar(self.gid, picker.choice or None)
        dlg.destroy()

    def _fill(self):
        for row in self.members.get_children():
            self.members.remove(row)
        entries = [("Vous", True, self.core.identity.fingerprint)]
        for cid in self.core.store.group_member_ids(self.gid):
            c = self.core.store.get_contact(cid)
            entries.append((c.name, self.core.is_online(cid), ", ".join(self.core.store.ips(cid))))
        for name, online, sub in entries:
            row = Gtk.Box(spacing=8, margin=4)
            dot = _label("●", "online" if online else "offline")
            row.pack_start(dot, False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            texts.pack_start(_label(name), False, False, 0)
            texts.pack_start(_label(sub, "subtitle"), False, False, 0)
            row.pack_start(texts, True, True, 0)
            self.members.add(row)
        self.members.show_all()

    def _add_members(self, _b):
        dlg, box = _dialog(self.dlg, "Ajouter des membres", "Ajouter")
        picker = MemberPicker(self.core, exclude=set(self.core.store.group_member_ids(self.gid)))
        box.pack_start(picker, True, True, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK and picker.selected():
            self.core.add_group_members(self.gid, picker.selected())
            self._fill()
        dlg.destroy()

    def _leave(self, _b):
        g = self.core.store.get_group(self.gid)
        if _confirm(self.dlg, f"Quitter « {g.name} » ?",
                    "Vous ne recevrez plus les messages de ce groupe et l'historique sera effacé "
                    "de ce poste.", "Quitter"):
            self.core.leave_group(self.gid)
            self.dlg.response(Gtk.ResponseType.CANCEL)


# ==========================================================================
# Application
# ==========================================================================
class QChatApp(Gtk.Application):
    def __init__(self, home=None, port=DEFAULT_PORT, discovery=True, background=False, restore=None,
                 relaunch_args=()):
        flags = Gio.ApplicationFlags.NON_UNIQUE if home else Gio.ApplicationFlags.FLAGS_NONE
        super().__init__(application_id=APP_ID, flags=flags)
        self._home, self._port, self._discovery = home, port, discovery
        self._background = background
        self._restore = restore
        self._relaunch_args = list(relaunch_args)
        self._update_version = None
        self.exit_code = None       # code de sortie imposé (75 : redémarrage par systemd)
        self.core = None
        self.win = None
        self.notifier = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        if repair_user_icon_cache():
            Gtk.IconTheme.get_default().rescan_if_needed()
        self.theme = None
        self.core = Core(home=self._home, port=self._port, discovery=self._discovery,
                         on_event=lambda name, data: GLib.idle_add(self._on_event, name, data))
        global THUMBS
        THUMBS = ThumbCache(os.path.join(self.core.home, "cache", "thumbs"), _decoder().submit)
        threading.Thread(target=THUMBS.prune, daemon=True, name="qchat-prune").start()
        # Photos rondes préparées pendant que la fenêtre se construit (premier lancement après mise à jour).
        convs = [c.conv for c in self.core.store.contacts()] + [g.conv for g in self.core.store.groups()]
        _warm_thumbnails(photos=[p for p in map(self.core.avatar_path, convs) if p], scale=_screen_scale())
        try:
            self._start_core()
        except OSError as e:
            self.core.stop()
            self.core = None
            if session.under_service():
                # Pas de fenêtre bloquante : on sort en erreur et systemd relance QChat 5 s plus tard.
                log.error("Port TCP %d indisponible (%s) : nouvel essai par systemd", self._port, e.strerror)
                self.exit_code = 1
                return
            dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                    text="Impossible de démarrer QChat")
            dlg.format_secondary_text(f"Le port TCP {self._port} est indisponible : {e.strerror}.\n"
                                      "QChat est peut-être déjà lancé par un autre utilisateur.")
            dlg.run()
            dlg.destroy()
            return
        self.theme = ThemeManager(self.core.store.get_setting("theme", "system"), self._on_theme_changed)
        theme_action = Gio.SimpleAction.new_stateful("theme", GLib.VariantType.new("s"),
                                                     GLib.Variant("s", self.theme.mode))
        theme_action.connect("change-state", self._on_theme_action)
        self.add_action(theme_action)
        smileys = Gio.SimpleAction.new_stateful("smileys", None, GLib.Variant("b", self.smileys_enabled()))
        smileys.connect("change-state", self._on_smileys_action)
        self.add_action(smileys)
        self.badge = LauncherBadge(f"{APP_ID}.desktop")
        self.notifier = Notifier(self._on_notification_clicked, self.core.store)
        self.notifier.restore(self._has_unread)
        for name, cb, accel in (
                ("quit", lambda *_a: self.quit(), "<Primary>q"),
                ("restart", lambda *_a: self._restart(), None),
                ("check-updates", lambda *_a: UpdateDialog(self).run_check(), None),
                ("whats-new", lambda *_a: self.show_whats_new(all_versions=True), None),
                ("add-contact", lambda *_a: self._ensure_window().add_contact_dialog(), "<Primary>n"),
                ("new-group", lambda *_a: self._ensure_window().new_group_dialog(), "<Primary>g"),
                ("scan", lambda *_a: ScanDialog(self._ensure_window()), "<Primary>d"),
                ("identity", lambda *_a: self._ensure_window().identity_dialog(), None)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])
        self.hold()     # reste actif fenêtre fermée, pour les notifications
        self._announce_update()
        GLib.timeout_add_seconds(UPDATE_POLL, self._check_update)

    def _start_core(self):
        """Ouvre le port ; sous le service, attend un peu qu'il se libère (ancienne session en cours
        de fermeture, par exemple) avant d'abandonner."""
        deadline = time.monotonic() + (PORT_WAIT if session.under_service() else 0)
        while True:
            try:
                self.core.start()
                return
            except OSError as e:
                if time.monotonic() >= deadline:
                    raise
                log.warning("Port TCP %d indisponible (%s), nouvel essai…", self._port, e.strerror)
                time.sleep(1)

    def do_activate(self):
        if self.core is None:
            self.quit()
            return
        win = self._ensure_window()
        pending = self.core.store.get_setting("restart_restore")
        if pending is not None:      # relancé par systemd après une mise à jour
            self.core.store.set_setting("restart_restore", None)
            try:
                state = json.loads(pending)
            except ValueError:
                state = {}
            if state.get("visible"):
                self._background = False
                self._restore = state.get("conv") or self._restore
        if self._background:
            self._background = False
            win.hide()
            return
        win.present()
        if self._restore:
            win.open_conv(self._restore)
            self._restore = None
        if not self.core.name:
            self._ask_name()

    def do_shutdown(self):
        if getattr(self, "badge", None):
            self.badge.close()
        if self.core:
            self.core.stop()
        Gtk.Application.do_shutdown(self)

    # -- mises à jour automatiques ----------------------------------------------
    def _has_unread(self, conv):
        return self.core.store.unread_count(conv) > 0

    def _announce_update(self):
        """Après une mise à jour : notification, et fenêtre « Nouveautés » au prochain affichage de
        QChat (tout de suite s'il revient à l'écran, sinon quand l'utilisateur l'ouvre)."""
        store = self.core.store
        previous = store.get_setting("last_version")
        if previous != __version__:
            store.set_setting("last_version", __version__)
            if previous and updater.is_newer(__version__, previous):
                # Plusieurs mises à jour sans ouvrir QChat : on part de la plus ancienne non présentée.
                pending = store.get_setting("whats_new_from") or previous
                if updater.is_newer(pending, previous):
                    pending = previous
                store.set_setting("whats_new_from", pending)
                news = changelog.since(pending)
                hint = " Ouvrez QChat pour voir les nouveautés." if news else ""
                self.notifier.notify("QChat a été mis à jour",
                                     f"Version {__version__} installée (précédente : {previous}).{hint}", None,
                                     transient=True)

    def show_whats_new(self, all_versions=False):
        """Fenêtre « Nouveautés » : versions arrivées depuis la dernière présentée, ou tout le journal."""
        store = self.core.store
        since = store.get_setting("whats_new_from")
        store.set_setting("whats_new_from", None)
        releases = changelog.load() if all_versions else changelog.since(since)
        if not all_versions and not releases:
            return
        WhatsNewDialog(self._ensure_window(), releases, None if all_versions else since).present()

    def maybe_show_whats_new(self):
        if self.core is not None and self.core.store.get_setting("whats_new_from"):
            GLib.idle_add(lambda: self.show_whats_new() and False)

    def _check_update(self):
        if self._update_version is None:
            self._update_version = updater.pending_update()
            if self._update_version:
                log.info("QChat %s installé : redémarrage dès que possible", self._update_version)
                if self.win is not None:
                    self.win.set_update_ready(self._update_version)
        if self._update_version and updater.auto_restart_enabled() and self._can_restart():
            self._restart()
            return False
        return True

    def _can_restart(self):
        """Pas de redémarrage pendant que l'on écrit ou qu'une boîte de dialogue est ouverte."""
        win = self.win
        others = [w for w in self.get_windows() if w is not win and w.get_visible()]
        if others:
            return False
        return win is None or not win.has_draft()

    def _restart(self):
        win = self.win
        if session.under_service():
            # Service systemd : on sort avec le code 75, systemd relance QChat aussitôt.
            visible = win is not None and win.get_visible()
            self.core.store.set_setting("restart_restore", json.dumps(
                {"visible": visible, "conv": win.current if visible else None}))
            self.exit_code = session.RESTART_EXIT_CODE
            self.quit()
            return
        args = list(self._relaunch_args)
        if win is not None and win.get_visible():
            if win.current:
                args += ["--restore", win.current]
        else:
            args.append("--background")
        updater.relaunch(args)
        self.quit()

    def _ensure_window(self):
        if self.win is None:
            self.win = MainWindow(self)
        return self.win

    def _ask_name(self):
        dlg, box = _dialog(self.win, "Bienvenue dans QChat", "Commencer")
        dlg.set_deletable(False)
        try:
            default = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0]
        except KeyError:
            default = ""
        default = default or os.environ.get("USER", "")
        box.pack_start(_label("Comment voulez-vous apparaître auprès de vos collègues ?", wrap=True),
                       False, False, 0)
        entry = Gtk.Entry(text=default, activates_default=True)
        box.pack_start(entry, False, False, 0)
        box.pack_start(_label("Vos messages sont chiffrés de bout en bout. Empreinte de votre clé :\n"
                              + self.core.identity.fingerprint, "subtitle", wrap=True), False, False, 0)
        dlg.show_all()
        dlg.run()
        self.core.set_name(entry.get_text().strip() or default or "Anonyme")
        dlg.destroy()
        self.win.refresh_sidebar()

    def smileys_enabled(self):
        return self.core is not None and self.core.store.get_setting("smileys", "1") == "1"

    def _on_smileys_action(self, action, value):
        action.set_state(value)
        self.core.store.set_setting("smileys", "1" if value.get_boolean() else "0")

    def _on_theme_action(self, action, value):
        action.set_state(value)
        self.core.store.set_setting("theme", value.get_string())
        self.theme.set_mode(value.get_string())

    def _on_theme_changed(self):
        if self.win:
            self.win.refresh_sidebar()     # couleurs insérées dans le texte (« en ligne », logo)

    def _on_notification_clicked(self, conv):
        win = self._ensure_window()
        win.present()
        if conv:
            win.open_conv(conv)

    def _show_wizz(self, contact_id, conv, kind):
        c = self.core.store.get_contact(contact_id)
        if not c or self.core.muted_until(conv):
            return      # conversation en sourdine : le wizz reste noté dans la conversation
        name = c.name
        if conv.startswith("g:"):
            g = self.core.store.get_group(conv[2:])
            name = f"{c.name} ({g.name})" if g else c.name
        popup = WizzPopup(name, kind, avatar=self.core.avatar_path(c.conv), initials=_initials(c.name),
                          on_reply=lambda: self._on_notification_clicked(conv))
        popup.set_application(self)
        popup.popup()
        play_buzz(os.path.join(GLib.get_user_cache_dir(), "qchat"))
        if self.win:
            self.win.set_urgency_hint(True)

    def _on_event(self, name, data):
        if self.win is None:
            self._ensure_window().hide()
        win = self.win
        if name == "message":
            win.on_message(data["message"], data.get("incoming", False))
        elif name == "status":
            win.on_status(data["msg_id"], data["status"])
        elif name == "reaction":
            win.on_reaction(**data)
        elif name == "custom_emojis_changed":
            win.on_custom_emojis()
        elif name == "edited":
            win.on_edited(**data)
        elif name == "deleted":
            win.on_deleted(**data)
        elif name == "receipt":
            win.on_receipt(**data)
        elif name in ("muted", "peer_mute"):
            win.refresh_sidebar()
        elif name == "wizz":
            self._show_wizz(**data)
        elif name == "key_changed":
            c = self.core.store.get_contact(data["contact_id"])
            if c:
                self.notifier.notify("Clé de sécurité modifiée",
                                     f"La clé de {c.name} a changé. Les messages sont suspendus "
                                     "jusqu'à votre validation.", c.conv)
            win.refresh_sidebar()
        elif name == "group_invite":
            g = self.core.store.get_group(data["gid"])
            c = self.core.store.get_contact(data["contact_id"])
            if g and c:
                self.notifier.notify(f"Nouveau groupe : {g.name}", f"{c.name} vous a ajouté au groupe.",
                                     g.conv)
            win.refresh_sidebar()
        else:   # contacts_changed, groups_changed, presence, discovered
            win.refresh_sidebar()
        return False
