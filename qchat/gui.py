"""Interface GTK 3 de QChat."""

import logging
import os
import pwd
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import gifs, netutil  # noqa: E402
from .core import DEFAULT_PORT, MAX_AVATAR, Core, sniff_mime  # noqa: E402
from .crypto import fingerprint  # noqa: E402
from .desktop import repair_user_icon_cache  # noqa: E402
from .notify import Notifier  # noqa: E402
from .theme import MODES, ThemeManager  # noqa: E402
from .viewer import AnimatedImage, Viewer, cover, fit, load_animation, viewable_kind  # noqa: E402
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


def _avatar(name, small=False, online=False, peer=False, image=None, large=False):
    """Pastille ronde : photo si ``image`` (chemin) est fourni, sinon initiales ; point vert de présence."""
    lbl = _label("" if image else _initials(name), "avatar", xalign=0.5)
    ctx = lbl.get_style_context()
    if small:
        ctx.add_class("small")
    if large:
        ctx.add_class("large")
    if peer:
        ctx.add_class("peer")
    if image:
        ctx.add_class("photo")
        css = Gtk.CssProvider()
        uri = Gio.File.new_for_path(image).get_uri().replace('"', "%22")
        css.load_from_data(f'label {{ background-image: url("{uri}"); }}'.encode())
        ctx.add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
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


WIZZ_COMMANDS = {"/wizz": "wizz", "/poke": "poke"}


def uuid_hex():
    return os.urandom(8).hex()


def _animation_from_bytes(data):
    """(animation, pixbuf fixe) décodés depuis des octets ; animation None si l'image est fixe."""
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error:
        return None, None
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

    def __init__(self, section, key, name, subtitle, when="", online=False, unread=0, warn=False, image=None):
        super().__init__()
        self.section, self.key = section, key
        box = Gtk.Box(spacing=10, margin=7, margin_start=8, margin_end=10)
        box.pack_start(_avatar(name, online=online, peer=section == "peer", image=image), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=6)
        title = _label(name)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_markup(f"<b>{GLib.markup_escape_text(name)}</b>")
        top.pack_start(title, True, True, 0)
        if when:
            top.pack_end(_label(when, "time"), False, False, 0)
        bottom = Gtk.Box(spacing=6)
        sub = _label(subtitle, "subtitle warning" if warn else "subtitle")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        bottom.pack_start(sub, True, True, 0)
        if unread:
            bottom.pack_end(_label(str(unread), "badge", xalign=0.5), False, False, 0)
        texts.pack_start(top, False, False, 0)
        texts.pack_start(bottom, False, False, 0)
        box.pack_start(texts, True, True, 0)
        self.add(box)


QUICK_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🎉", "✅", "👀", "🔥", "👏", "🤔"]

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
        self._text_labels = {}       # msg_id -> (texte, méta) des bulles affichées
        self._editing = None         # msg_id du message en cours de modification
        self._viewer = None          # visionneuse de pièces jointes ouverte
        self._last_day = None
        self._stick_bottom = True
        self.set_default_size(980, 640)
        self.set_icon_name("qchat")
        self._build()
        self.connect("delete-event", self._on_close)
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
        main_menu.append("Quitter", "app.quit")
        menu_btn = Gtk.MenuButton(menu_model=main_menu)
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(menu_btn)

        paned = Gtk.Paned(position=310)
        self.add(paned)

        # Barre latérale
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_header_func(self._sidebar_header)
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
        chat.pack_start(self.msg_scroll, True, True, 0)

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
        store = self.core.store
        for row in self.sidebar.get_children():
            self.sidebar.remove(row)
        last = {}

        def last_msg(conv):
            if conv not in last:
                last[conv] = store.last_message(conv)
            return last[conv]

        def last_ts(conv):
            m = last_msg(conv)
            return m.ts if m else 0

        total_unread = 0
        for g in sorted(store.groups(), key=lambda g: -last_ts(g.conv)):
            unread = store.unread_count(g.conv)
            total_unread += unread
            m = last_msg(g.conv)
            n = len(store.group_member_ids(g.gid)) + 1
            self.sidebar.add(ConvRow("group", g.conv, g.name, self._preview(m) if m else f"{n} membres",
                                     when=_short_time(m.ts) if m else "", unread=unread,
                                     image=self.core.avatar_path(g.conv)))
        for c in sorted(store.contacts(), key=lambda c: (-last_ts(c.conv), c.name.lower())):
            unread = store.unread_count(c.conv)
            total_unread += unread
            m = last_msg(c.conv)
            self.sidebar.add(ConvRow("contact", c.conv, c.name, self._contact_subtitle(c, m),
                                     when=_short_time(m.ts) if m else "", online=self.core.is_online(c.id),
                                     unread=unread, warn=bool(c.pending_vk),
                                     image=self.core.avatar_path(c.conv)))
        for vk, peer in sorted(self.core.discovered.items(), key=lambda kv: kv[1]["name"].lower()):
            self.sidebar.add(ConvRow("peer", vk, peer["name"], ", ".join(peer["ips"]), online=True))
        self.sidebar.show_all()

        for row in self.sidebar.get_children():
            if row.key == self.current:
                self.sidebar.select_row(row)
        accent = self.app.theme.palette["accent"]
        count = f" ({total_unread})" if total_unread else ""
        self.title_label.set_markup(f"<b><span foreground='{accent}'>Q</span>Chat{count}</b>")
        self.subtitle_label.set_text(self.core.name)
        self.set_title(f"QChat{count}")
        self._refresh_chat_header()

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
        self._text_labels.clear()
        self._cancel_edit()
        self._last_day = None
        for row in self.msg_list.get_children():
            self.msg_list.remove(row)
        for m in self.core.store.messages(conv):
            self._append_message(m)
        self._stick_bottom = True
        self.core.store.mark_read(conv)
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
        for child in self.chat_avatar.get_children():
            self.chat_avatar.remove(child)
        if kind == "g":
            g = self.core.store.get_group(ident)
            names = [self.core.store.get_contact(i).name for i in self.core.store.group_member_ids(ident)]
            self.chat_avatar.add(_avatar(g.name, small=True, image=self.core.avatar_path(g.conv)))
            self.chat_avatar.show_all()
            self.chat_title.set_text(g.name)
            self.chat_sub.set_text(f"{len(names) + 1} membres · Vous, "
                                   + ", ".join(sorted(names, key=str.lower)))
        else:
            c = self.core.store.get_contact(int(ident))
            online = self.core.is_online(c.id)
            self.chat_avatar.add(_avatar(c.name, small=True, online=online,
                                         image=self.core.avatar_path(c.conv)))
            self.chat_avatar.show_all()
            self.chat_title.set_text(c.name)
            if not c.vk:
                state = "jamais joint — les messages partiront dès qu'il sera joignable"
            elif online:
                state = f"<span foreground='{self.app.theme.palette['accent']}'>en ligne</span>"
            else:
                state = "hors ligne"
            self.chat_sub.set_markup(state)
            if c.pending_vk:
                self.infobar_label.set_text(
                    f"Une clé de sécurité différente a répondu à l'adresse {c.pending_ip} de {c.name}. "
                    "C'est normal si cette personne a réinstallé QChat ; sinon il peut s'agir d'un autre "
                    "poste ayant récupéré l'IP, ou d'une usurpation. Vérifiez l'empreinte avec elle.\n"
                    f"Nouvelle empreinte : {fingerprint(c.pending_vk)}")
                self.infobar.show_all()

    def _sender_name(self, sender_id):
        c = self.core.store.get_contact(sender_id) if sender_id is not None else None
        return c.name if c else "?"

    def _append_message(self, m):
        day = _day_label(m.ts)
        if day != self._last_day:
            self._last_day = day
            self._add_row(_label(day, "day", xalign=0.5), Gtk.Align.CENTER)
        if m.kind == "system":
            note = _label(m.text, "system", xalign=0.5, wrap=True)
            note.set_max_width_chars(70)
            note.set_justify(Gtk.Justification.CENTER)
            self._add_row(note, Gtk.Align.CENTER)
            return
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        ctx = bubble.get_style_context()
        ctx.add_class("bubble")
        ctx.add_class("out" if m.outgoing else "in")
        if not m.outgoing and m.conv.startswith("g:"):
            bubble.pack_start(_label(self._sender_name(m.sender_id), "sender"), False, False, 0)
        if m.kind == "file":
            bubble.pack_start(self._attachment_widget(m), False, False, 0)
        else:
            text = _label(m.text, wrap=True, selectable=True)
            text.set_max_width_chars(60)
            text.set_can_focus(False)
            bubble.pack_start(text, False, False, 0)
        meta = _label("", "meta", xalign=1.0)
        meta.set_track_visited_links(False)
        meta.connect("activate-link", lambda _l, _uri, mid=m.id: self._show_history(mid) or True)
        self._set_meta(meta, m)
        bubble.pack_start(meta, False, False, 0)
        if m.outgoing:
            self._status_labels[m.id] = meta
        if m.kind == "text":
            self._text_labels[m.id] = (text, meta)
        self._add_row(self._with_reactions(m, bubble), Gtk.Align.END if m.outgoing else Gtk.Align.START)

    # -- réactions -------------------------------------------------------------
    def _with_reactions(self, m, bubble):
        """Bulle + pastilles de réactions + bouton 😊 visible au survol (ou clic droit)."""
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        column.pack_start(bubble, False, False, 0)
        reacts = Gtk.Box(spacing=4, halign=Gtk.Align.END if m.outgoing else Gtk.Align.START)
        reacts.set_no_show_all(True)
        column.pack_start(reacts, False, False, 0)
        self._reaction_boxes[m.id] = reacts
        self._fill_reactions(m.id, reacts)

        btn = Gtk.Button.new_from_icon_name("face-smile-symbolic", Gtk.IconSize.BUTTON)
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text("Réagir")
        btn.get_style_context().add_class("react-btn")
        btn.set_opacity(0)
        btn.connect("clicked", lambda b: self._react_popover(b, m.id))
        tools = Gtk.Box(valign=Gtk.Align.CENTER)
        tools.pack_start(btn, False, False, 0)
        hover = [btn]
        if m.outgoing and m.kind == "text":
            edit = Gtk.Button.new_from_icon_name("document-edit-symbolic", Gtk.IconSize.BUTTON)
            edit.set_relief(Gtk.ReliefStyle.NONE)
            edit.set_tooltip_text("Modifier")
            edit.get_style_context().add_class("react-btn")
            edit.set_opacity(0)
            edit.connect("clicked", lambda _b: self._start_edit(m.id))
            tools.pack_start(edit, False, False, 0)
            hover.append(edit)
        line = Gtk.Box(spacing=2)
        for child in ((tools, column) if m.outgoing else (column, tools)):
            line.pack_start(child, False, False, 0)

        ev = Gtk.EventBox()
        ev.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        ev.add(line)
        ev.connect("enter-notify-event", lambda *_a: [b.set_opacity(1) for b in hover])
        ev.connect("leave-notify-event", lambda _w, e: e.detail == Gdk.NotifyType.INFERIOR
                   or [b.set_opacity(0) for b in hover])
        ev.connect("button-press-event", lambda _w, e: e.button == 3 and self._react_popover(btn, m.id))
        return ev

    def _fill_reactions(self, msg_id, box):
        for child in box.get_children():
            box.remove(child)
        me = self.core.identity.vk_hex
        by_emoji = {}
        for emoji, reactor in self.core.store.reactions(msg_id):
            by_emoji.setdefault(emoji, []).append(reactor)
        for emoji, reactors in by_emoji.items():
            names = []
            for r in reactors:
                c = None if r == me else self.core.store.contact_by_vk(r)
                names.append("Vous" if r == me else (c.name if c else "?"))
            b = Gtk.Button(label=f"{emoji} {len(reactors)}" if len(reactors) > 1 else emoji)
            ctx = b.get_style_context()
            ctx.add_class("reaction")
            if me in reactors:
                ctx.add_class("mine")
            b.set_tooltip_text(", ".join(names))
            b.connect("clicked", lambda _b, e=emoji: self.core.react(msg_id, e))
            box.pack_start(b, False, False, 0)
            b.show()
        box.set_visible(bool(by_emoji))

    def _react_popover(self, anchor, msg_id):
        pop = Gtk.Popover(relative_to=anchor)
        grid = Gtk.Grid(margin=4)
        for i, emoji in enumerate(QUICK_EMOJIS):
            b = Gtk.Button(label=emoji, relief=Gtk.ReliefStyle.NONE)
            b.get_style_context().add_class("emoji-pick")
            b.connect("clicked", lambda _b, e=emoji: (self.core.react(msg_id, e), pop.popdown()))
            grid.attach(b, i % 6, i // 6, 1, 1)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.pack_start(grid, False, False, 0)
        m = self.core.store.get_message(msg_id)
        actions = []
        if m and m.outgoing and m.kind == "text":
            actions.append(("Modifier", self._start_edit))
        if m and m.edited:
            actions.append(("Historique des modifications",
                            self._show_history))
        if actions:
            content.pack_start(Gtk.Separator(), False, False, 2)
        for text, fn in actions:
            row = Gtk.ModelButton(text=text, centered=False)
            row.connect("clicked", lambda _b, fn=fn: (pop.popdown(), fn(msg_id)))
            content.pack_start(row, False, False, 0)
        pop.add(content)
        content.show_all()
        pop.popup()
        return True

    def on_reaction(self, msg_id, contact_id=None, emoji=None, on=False, incoming=False):
        box = self._reaction_boxes.get(msg_id)
        if box:
            self._fill_reactions(msg_id, box)
        m = self.core.store.get_message(msg_id)
        visible = m and self.current == m.conv and self.is_active() and self.get_visible()
        if incoming and on and m and m.outgoing and not visible:
            c = self.core.store.get_contact(contact_id)
            self.app.notifier.notify(f"{c.name if c else '?'} a réagi {emoji}",
                                     f"à votre message : {m.text}", m.conv)

    @staticmethod
    def _set_meta(label, m):
        status = m.status if m.outgoing else None
        txt = time.strftime("%H:%M", time.localtime(m.ts))
        label.set_tooltip_text(None)
        if status == "pending":
            txt += "  🕓"
            label.set_tooltip_text("En attente : le destinataire n'est pas joignable")
        elif status == "delivered":
            txt += "  ✓"
            label.set_tooltip_text("Remis")
        elif status == "failed":
            txt += "  ⚠"
            label.set_tooltip_text("Échec : le fichier à envoyer a disparu")
        if not m.edited:
            label.set_text(txt)
            return
        when = time.strftime("%d/%m à %H:%M", time.localtime(m.edited))
        tip = f"Modifié le {when} — cliquez pour voir l'historique"
        label.set_markup(f"<a href='history' title='{GLib.markup_escape_text(tip)}'>modifié</a>  "
                         + GLib.markup_escape_text(txt))

    def _add_row(self, widget, halign):
        widget.set_halign(halign)
        widget.set_margin_start(12)
        widget.set_margin_end(12)
        widget.set_margin_top(3)
        widget.set_margin_bottom(3)
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.add(widget)
        row.show_all()
        self.msg_list.add(row)

    def _on_adj_changed(self, adj):
        if self._stick_bottom:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _on_adj_value(self, adj):
        self._stick_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 30

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
            anim = load_animation(att.path)
            if anim:        # GIF animé : l'aperçu s'anime dans la discussion
                ev.add(AnimatedImage(anim, lambda w, h: fit(w, h, 320, 240)))
            else:
                ev.add(Gtk.Image.new_from_pixbuf(thumb))
            ev.connect("button-press-event", lambda _w, e: e.button == 1 and self._view_file(m.id) or True)
            ev.connect("realize", lambda w: w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
            box.pack_start(ev, False, False, 0)
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
        cache = self.__dict__.setdefault("_thumbs", {})
        if path not in cache:
            cache[path] = None
            try:
                if os.path.getsize(path) <= 50 * 1024 * 1024:     # pas d'aperçu pour les images énormes
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 320, 240, True)
                    cache[path] = pb.apply_embedded_orientation() or pb
            except (GLib.Error, OSError):
                pass
        return cache[path]

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
        if (event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (Gdk.KEY_v, Gdk.KEY_V)
                and self.current and self._paste()):
            return True
        if event.keyval == Gdk.KEY_Escape and self._editing:
            self._cancel_edit()
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

    def _send(self):
        if not self.current:
            return
        buf = self.input.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
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
            self.core.send_text(self.current, text)
            buf.set_text("")

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
    def _start_edit(self, msg_id):
        m = self.core.store.get_message(msg_id)
        if not m or not m.outgoing or m.kind != "text" or m.conv != self.current:
            return
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
            if m.outgoing and m.kind == "text":
                return m.id
        return None

    def on_edited(self, msg_id, incoming=False):
        m = self.core.store.get_message(msg_id)
        entry = self._text_labels.get(msg_id)
        if m and entry:
            entry[0].set_text(m.text)
            self._set_meta(entry[1], m)
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
            self.core.store.mark_read(m.conv)
        if incoming and not visible:
            sender = self._sender_name(m.sender_id)
            title = sender
            if m.conv.startswith("g:"):
                g = self.core.store.get_group(m.conv[2:])
                title = f"{sender} — {g.name}" if g else sender
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
            self.set_urgency_hint(False)
            if self.current:
                self.core.store.mark_read(self.current)
                self.app.notifier.withdraw(self.current)
                self.refresh_sidebar()

    def _on_close(self, *_a):
        self.hide()
        if self.core.store.get_setting("background_hint") != "1":
            self.core.store.set_setting("background_hint", "1")
            self.app.notifier.notify("QChat reste actif en arrière-plan",
                                     "Vous continuerez à recevoir les notifications. "
                                     "Utilisez le menu ☰ › Quitter pour fermer complètement.", "")
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
    def __init__(self, home=None, port=DEFAULT_PORT, discovery=True, background=False):
        flags = Gio.ApplicationFlags.NON_UNIQUE if home else Gio.ApplicationFlags.FLAGS_NONE
        super().__init__(application_id=APP_ID, flags=flags)
        self._home, self._port, self._discovery = home, port, discovery
        self._background = background
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
        try:
            self.core.start()
        except OSError as e:
            dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                    text="Impossible de démarrer QChat")
            dlg.format_secondary_text(f"Le port TCP {self._port} est indisponible : {e.strerror}.\n"
                                      "QChat est peut-être déjà lancé par un autre utilisateur.")
            dlg.run()
            dlg.destroy()
            self.core = None
            return
        self.theme = ThemeManager(self.core.store.get_setting("theme", "system"), self._on_theme_changed)
        theme_action = Gio.SimpleAction.new_stateful("theme", GLib.VariantType.new("s"),
                                                     GLib.Variant("s", self.theme.mode))
        theme_action.connect("change-state", self._on_theme_action)
        self.add_action(theme_action)
        self.notifier = Notifier(self._on_notification_clicked)
        for name, cb, accel in (
                ("quit", lambda *_a: self.quit(), "<Primary>q"),
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

    def do_activate(self):
        if self.core is None:
            self.quit()
            return
        win = self._ensure_window()
        if self._background:
            self._background = False
            win.hide()
            return
        win.present()
        if not self.core.name:
            self._ask_name()

    def do_shutdown(self):
        if self.core:
            self.core.stop()
        Gtk.Application.do_shutdown(self)

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
        if not c:
            return
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
        elif name == "edited":
            win.on_edited(**data)
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
