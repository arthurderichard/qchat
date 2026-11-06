"""Interface GTK 3 de QChat."""

import logging
import os
import pwd
import shutil
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import netutil  # noqa: E402
from .core import DEFAULT_PORT, Core, sniff_mime  # noqa: E402
from .crypto import fingerprint  # noqa: E402
from .notify import Notifier  # noqa: E402

log = logging.getLogger("qchat")

APP_ID = "fr.qchat.QChat"

CSS = b"""
.bubble { padding: 6px 10px; border-radius: 12px; }
.bubble.in { background-color: alpha(@theme_fg_color, 0.08); }
.bubble.out { background-color: @theme_selected_bg_color; color: @theme_selected_fg_color; }
.meta { font-size: smaller; opacity: 0.7; }
.sender { font-weight: bold; font-size: smaller; }
.system { font-style: italic; opacity: 0.6; font-size: smaller; }
.day { font-weight: bold; opacity: 0.5; font-size: smaller; }
.badge { background-color: @theme_selected_bg_color; color: @theme_selected_fg_color;
         border-radius: 10px; padding: 0 7px; font-size: smaller; font-weight: bold; }
.online { color: #2ec27e; }
.offline { color: alpha(@theme_fg_color, 0.25); }
.warning { color: #e5a50a; }
.section { font-weight: bold; font-size: smaller; opacity: 0.55; padding: 10px 10px 3px 10px; }
.subtitle { font-size: smaller; opacity: 0.7; }
.chat-title { font-weight: bold; font-size: larger; }
.mono { font-family: monospace; }
.file-name { font-weight: bold; }
.file-btn { padding: 2px; min-height: 0; min-width: 0; }
"""


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


def _human_size(n):
    for unit in ("octets", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return f"{n} {unit}" if unit == "octets" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


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
    def __init__(self, section, key, name, subtitle, online=None, unread=0, warn=False):
        super().__init__()
        self.section, self.key = section, key
        box = Gtk.Box(spacing=8, margin=6, margin_start=10)
        dot = _label("●" if online is not None else "#", xalign=0.5)
        if warn:
            dot.set_text("⚠")
            dot.get_style_context().add_class("warning")
        elif online is not None:
            dot.get_style_context().add_class("online" if online else "offline")
        box.pack_start(dot, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = _label(name)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        if unread:
            title.set_markup(f"<b>{GLib.markup_escape_text(name)}</b>")
        sub = _label(subtitle, "subtitle")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        texts.pack_start(title, False, False, 0)
        texts.pack_start(sub, False, False, 0)
        box.pack_start(texts, True, True, 0)
        if unread:
            box.pack_end(_label(str(unread), "badge", xalign=0.5), False, False, 0)
        self.add(box)


SECTIONS = {"group": "GROUPES", "contact": "CONTACTS", "peer": "SUR LE RÉSEAU (non ajoutés)"}


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
        hb = Gtk.HeaderBar(show_close_button=True, title="QChat")
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
        main_menu.append("Quitter", "app.quit")
        menu_btn = Gtk.MenuButton(menu_model=main_menu)
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(menu_btn)

        paned = Gtk.Paned(position=290)
        self.add(paned)

        # Barre latérale
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar.connect("row-activated", self._on_row_activated)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroll.add(self.sidebar)
        paned.pack1(side_scroll, resize=False, shrink=False)

        # Zone de discussion
        self.stack = Gtk.Stack()
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=10)
        img = Gtk.Image.new_from_icon_name("user-available-symbolic", Gtk.IconSize.DIALOG)
        img.set_pixel_size(64)
        img.set_opacity(0.4)
        empty.pack_start(img, False, False, 0)
        empty.pack_start(_label("Sélectionnez une conversation", "chat-title", xalign=0.5), False, False, 0)
        empty.pack_start(_label("Cliquez sur 🔍 pour découvrir les collègues du réseau,\n"
                                "ou ajoutez-en un par son adresse IP avec le bouton +.",
                                "subtitle", xalign=0.5), False, False, 0)
        self.stack.add_named(empty, "empty")

        chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=8, margin=8, margin_start=14)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.chat_title = _label("", "chat-title")
        self.chat_sub = _label("", "subtitle")
        self.chat_sub.set_ellipsize(Pango.EllipsizeMode.END)
        titles.pack_start(self.chat_title, False, False, 0)
        titles.pack_start(self.chat_sub, False, False, 0)
        top.pack_start(titles, True, True, 0)
        info_btn = Gtk.Button.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON)
        info_btn.set_tooltip_text("Détails")
        info_btn.connect("clicked", lambda _b: self.show_details())
        top.pack_end(info_btn, False, False, 0)
        chat.pack_start(top, False, False, 0)
        chat.pack_start(Gtk.Separator(), False, False, 0)

        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, no_show_all=True)
        self.infobar_label = _label("", wrap=True)
        self.infobar.get_content_area().add(self.infobar_label)
        self.infobar.add_button("Accepter la nouvelle clé", 1)
        self.infobar.add_button("C'est quelqu'un d'autre", 2)
        self.infobar.connect("response", self._on_infobar)
        chat.pack_start(self.infobar, False, False, 0)

        self.msg_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.msg_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.msg_scroll.add(self.msg_list)
        vadj = self.msg_scroll.get_vadjustment()
        vadj.connect("changed", self._on_adj_changed)
        vadj.connect("value-changed", self._on_adj_value)
        chat.pack_start(self.msg_scroll, True, True, 0)
        chat.pack_start(Gtk.Separator(), False, False, 0)

        bottom = Gtk.Box(spacing=6, margin=8)
        self.input = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.input.set_left_margin(6)
        self.input.set_right_margin(6)
        self.input.set_top_margin(6)
        self.input.set_bottom_margin(6)
        self.input.connect("key-press-event", self._on_input_key)
        in_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        in_scroll.set_shadow_type(Gtk.ShadowType.IN)
        in_scroll.set_propagate_natural_height(True)
        in_scroll.set_max_content_height(140)
        in_scroll.add(self.input)
        attach = Gtk.Button.new_from_icon_name("mail-attachment-symbolic", Gtk.IconSize.BUTTON)
        attach.set_tooltip_text("Joindre des fichiers ou des images\n"
                                "(ou glisser-déposer, ou Ctrl+V pour coller une capture d'écran)")
        attach.set_valign(Gtk.Align.END)
        attach.connect("clicked", lambda _b: self._pick_files())
        bottom.pack_start(attach, False, False, 0)
        bottom.pack_start(in_scroll, True, True, 0)
        send = Gtk.Button.new_from_icon_name("document-send-symbolic", Gtk.IconSize.BUTTON)
        send.set_tooltip_text("Envoyer (Entrée) — Maj+Entrée pour un retour à la ligne")
        send.get_style_context().add_class("suggested-action")
        send.set_valign(Gtk.Align.END)
        send.connect("clicked", lambda _b: self._send())
        bottom.pack_start(send, False, False, 0)
        chat.pack_start(bottom, False, False, 0)
        self.stack.add_named(chat, "chat")
        chat.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        chat.drag_dest_add_uri_targets()
        chat.connect("drag-data-received", self._on_drop)

        paned.pack2(self.stack, resize=True, shrink=False)
        self.show_all()

    # -- barre latérale -----------------------------------------------------
    def _sidebar_header(self, row, before):
        if before is None or before.section != row.section:
            row.set_header(_label(SECTIONS[row.section], "section"))
        else:
            row.set_header(None)

    def _contact_subtitle(self, c):
        if c.pending_vk:
            return "⚠ Clé de sécurité modifiée"
        ips = ", ".join(self.core.store.ips(c.id)) or "aucune IP"
        if not c.vk:
            return f"Jamais joint — {ips}"
        return ips

    def refresh_sidebar(self):
        store = self.core.store
        for row in self.sidebar.get_children():
            self.sidebar.remove(row)

        def last_ts(conv):
            m = store.last_message(conv)
            return m.ts if m else 0

        total_unread = 0
        groups = sorted(store.groups(), key=lambda g: -last_ts(g.conv))
        for g in groups:
            unread = store.unread_count(g.conv)
            total_unread += unread
            n = len(store.group_member_ids(g.gid)) + 1
            self.sidebar.add(ConvRow("group", g.conv, g.name, f"{n} membres", unread=unread))
        contacts = sorted(store.contacts(), key=lambda c: (-last_ts(c.conv), c.name.lower()))
        for c in contacts:
            unread = store.unread_count(c.conv)
            total_unread += unread
            self.sidebar.add(ConvRow("contact", c.conv, c.name, self._contact_subtitle(c),
                                     online=self.core.is_online(c.id), unread=unread,
                                     warn=bool(c.pending_vk)))
        for vk, peer in sorted(self.core.discovered.items(), key=lambda kv: kv[1]["name"].lower()):
            self.sidebar.add(ConvRow("peer", vk, peer["name"], ", ".join(peer["ips"]), online=True))
        self.sidebar.show_all()

        for row in self.sidebar.get_children():
            if row.key == self.current:
                self.sidebar.select_row(row)
        title = f"QChat ({total_unread})" if total_unread else "QChat"
        self.header.set_title(title)
        self.header.set_subtitle(self.core.name)
        self.set_title(title)
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
        if kind == "g":
            g = self.core.store.get_group(ident)
            names = [self.core.store.get_contact(i).name for i in self.core.store.group_member_ids(ident)]
            self.chat_title.set_text(g.name)
            self.chat_sub.set_text("Vous, " + ", ".join(sorted(names, key=str.lower)))
        else:
            c = self.core.store.get_contact(int(ident))
            self.chat_title.set_text(c.name)
            state = "en ligne" if self.core.is_online(c.id) else "hors ligne"
            if not c.vk:
                state = "jamais joint — les messages partiront dès qu'il sera joignable"
            self.chat_sub.set_text(f"{state} · {', '.join(self.core.store.ips(c.id))}")
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
            self._add_row(_label(day, "day", xalign=0.5), Gtk.Align.FILL)
        if m.kind == "system":
            self._add_row(_label(m.text, "system", xalign=0.5, wrap=True), Gtk.Align.FILL)
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
        self._set_meta(meta, m.ts, m.status if m.outgoing else None)
        bubble.pack_start(meta, False, False, 0)
        if m.outgoing:
            self._status_labels[m.id] = (meta, m.ts)
        self._add_row(bubble, Gtk.Align.END if m.outgoing else Gtk.Align.START)

    @staticmethod
    def _set_meta(label, ts, status):
        txt = time.strftime("%H:%M", time.localtime(ts))
        if status == "pending":
            txt += "  🕓"
            label.set_tooltip_text("En attente : le destinataire n'est pas joignable")
        elif status == "delivered":
            txt += "  ✓"
            label.set_tooltip_text("Remis")
        elif status == "failed":
            txt += "  ⚠"
            label.set_tooltip_text("Échec : le fichier à envoyer a disparu")
        label.set_text(txt)

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
            ev = Gtk.EventBox(tooltip_text=f"{att.name} — cliquer pour ouvrir")
            ev.add(Gtk.Image.new_from_pixbuf(thumb))
            ev.connect("button-press-event", lambda *_a: self._open_file(att.path) or True)
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
                ("document-open-symbolic", "Ouvrir", lambda: self._open_file(att.path)),
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

    def _save_file_as(self, att):
        chooser = Gtk.FileChooserNative(title="Enregistrer le fichier", transient_for=self,
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

    def _send_files(self, paths, cleanup=False):
        """Envoie en arrière-plan (la copie d'un gros fichier ne bloque pas l'interface)."""
        conv = self.current
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

    def _on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        paths = [Gio.File.new_for_uri(u).get_path() for u in data.get_uris() or []]
        self._send_files([p for p in paths if p])

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
        if text.strip():
            self._stick_bottom = True
            self.core.send_text(self.current, text)
            buf.set_text("")

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
        entry = self._status_labels.get(msg_id)
        if entry:
            self._set_meta(entry[0], entry[1], status)

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
        name = Gtk.Entry(text=self.core.name, activates_default=True)
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
        if dlg.run() == Gtk.ResponseType.OK and name.get_text().strip():
            self.core.set_name(name.get_text())
            self.refresh_sidebar()
        dlg.destroy()


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
        self.dlg = dlg
        name = Gtk.Entry(text=g.name, activates_default=True)
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
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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

    def _on_notification_clicked(self, conv):
        win = self._ensure_window()
        win.present()
        if conv:
            win.open_conv(conv)

    def _on_event(self, name, data):
        if self.win is None:
            self._ensure_window().hide()
        win = self.win
        if name == "message":
            win.on_message(data["message"], data.get("incoming", False))
        elif name == "status":
            win.on_status(data["msg_id"], data["status"])
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
