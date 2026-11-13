"""Persistance locale (SQLite) : contacts, IP, groupes, messages, file d'envoi."""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vk TEXT UNIQUE,                 -- clé publique Ed25519 (NULL tant que jamais joint)
    port INTEGER NOT NULL,
    pending_vk TEXT,                -- nouvelle clé vue sur une de ses IP, à valider
    pending_ip TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_ips (
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    ip TEXT NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (contact_id, ip)
);
CREATE TABLE IF NOT EXISTS groups (
    gid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    gid TEXT NOT NULL REFERENCES groups(gid) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (gid, contact_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conv TEXT NOT NULL,             -- 'c:<contact_id>' ou 'g:<gid>'
    sender_id INTEGER,              -- NULL = moi
    kind TEXT NOT NULL DEFAULT 'text',   -- 'text', 'file' ou 'system'
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,           -- 'pending', 'delivered', 'received'
    unread INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv, ts);
CREATE TABLE IF NOT EXISTS attachments (
    msg_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    path TEXT NOT NULL              -- copie locale du fichier
);
CREATE TABLE IF NOT EXISTS reactions (
    msg_id TEXT NOT NULL,
    reactor TEXT NOT NULL,          -- clé publique de la personne (la mienne incluse)
    emoji TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (msg_id, reactor, emoji)
);
CREATE TABLE IF NOT EXISTS message_edits (
    msg_id TEXT NOT NULL,
    text TEXT NOT NULL,             -- version remplacée
    ts REAL NOT NULL,               -- date de la modification qui l'a remplacée
    PRIMARY KEY (msg_id, ts)
);
CREATE TABLE IF NOT EXISTS avatars (
    owner TEXT PRIMARY KEY,         -- 'me', 'vk:<clé publique>' ou 'g:<gid>'
    sha TEXT NOT NULL,              -- SHA-256 de l'image
    path TEXT NOT NULL,             -- copie locale
    ts REAL NOT NULL                -- date du changement (le plus récent l'emporte, pour les groupes)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    msg_id TEXT,
    payload TEXT NOT NULL,
    created REAL NOT NULL
);
"""


@dataclass
class Contact:
    id: int
    name: str
    vk: str
    port: int
    pending_vk: str
    pending_ip: str

    @property
    def conv(self):
        return f"c:{self.id}"


@dataclass
class Group:
    gid: str
    name: str

    @property
    def conv(self):
        return f"g:{self.gid}"


@dataclass
class Attachment:
    msg_id: str
    name: str
    size: int
    mime: str
    path: str

    @property
    def is_image(self):
        return self.mime.startswith("image/")


@dataclass
class Message:
    id: str
    conv: str
    sender_id: int
    kind: str
    text: str
    ts: float
    status: str
    edited: float = None        # date de la dernière modification, None si jamais modifié
    reply_to: str = None        # identifiant du message auquel celui-ci répond
    deleted: float = None       # date de suppression par son auteur, None sinon
    mentions: list = None       # clés publiques des personnes mentionnées (@Nom), groupes seulement

    def __post_init__(self):
        if isinstance(self.mentions, str):
            try:
                self.mentions = [v for v in json.loads(self.mentions) if isinstance(v, str)]
            except ValueError:
                self.mentions = []
        self.mentions = self.mentions or []

    @property
    def outgoing(self):
        return self.sender_id is None and self.kind in ("text", "file")


class Store:
    def __init__(self, path: str):
        new = not os.path.exists(path)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if new:
            os.chmod(path, 0o600)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self):
        """Colonnes ajoutées après la création des premières bases."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}
        for name, decl in (("reply_to", "TEXT"), ("deleted", "REAL"), ("mentions", "TEXT")):
            if name not in cols:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")

    def close(self):
        with self._lock:
            self._db.close()

    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # -- réglages -----------------------------------------------------------
    def get_setting(self, key, default=None):
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0][0] if rows else default

    def set_setting(self, key, value):
        self._x("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    # -- contacts -----------------------------------------------------------
    _C = "SELECT id, name, vk, port, pending_vk, pending_ip FROM contacts"

    def add_contact(self, name, port, vk=None, ips=(), manual=False) -> int:
        with self._lock:
            cid = self._x("INSERT INTO contacts (name, vk, port, created) VALUES (?, ?, ?, ?)",
                          (name, vk, port, time.time())).lastrowid
            for ip in ips:
                self.add_ip(cid, ip, manual=manual)
            return cid

    def get_contact(self, cid):
        rows = self._q(self._C + " WHERE id=?", (cid,))
        return Contact(*rows[0]) if rows else None

    def contact_by_vk(self, vk):
        rows = self._q(self._C + " WHERE vk=?", (vk,))
        return Contact(*rows[0]) if rows else None

    def contacts(self):
        return [Contact(*r) for r in self._q(self._C + " ORDER BY name COLLATE NOCASE")]

    def unkeyed_contacts_with_ip(self, ip, port):
        rows = self._q(self._C + " WHERE vk IS NULL AND port=? AND id IN "
                       "(SELECT contact_id FROM contact_ips WHERE ip=?)", (port, ip))
        return [Contact(*r) for r in rows]

    def rename_contact(self, cid, name):
        self._x("UPDATE contacts SET name=? WHERE id=?", (name, cid))

    def set_port(self, cid, port):
        self._x("UPDATE contacts SET port=? WHERE id=?", (port, cid))

    def set_vk(self, cid, vk):
        self._x("UPDATE contacts SET vk=?, pending_vk=NULL, pending_ip=NULL WHERE id=?", (vk, cid))

    def set_pending_key(self, cid, vk, ip):
        self._x("UPDATE contacts SET pending_vk=?, pending_ip=? WHERE id=?", (vk, ip, cid))

    def clear_pending_key(self, cid):
        self._x("UPDATE contacts SET pending_vk=NULL, pending_ip=NULL WHERE id=?", (cid,))

    def _delete_conv_messages(self, conv):
        self._x("DELETE FROM reactions WHERE msg_id IN (SELECT id FROM messages WHERE conv=?)", (conv,))
        self._x("DELETE FROM message_edits WHERE msg_id IN (SELECT id FROM messages WHERE conv=?)", (conv,))
        self._x("DELETE FROM messages WHERE conv=?", (conv,))

    def delete_contact(self, cid):
        with self._lock:
            self._delete_conv_messages(f"c:{cid}")
            self._x("DELETE FROM contacts WHERE id=?", (cid,))

    def merge_contacts(self, src, dst):
        """Fusionne le contact ``src`` dans ``dst`` (même personne, même clé)."""
        with self._lock:
            self._x("INSERT OR IGNORE INTO contact_ips SELECT ?, ip, manual, last_seen "
                    "FROM contact_ips WHERE contact_id=?", (dst, src))
            self._x("UPDATE messages SET conv=? WHERE conv=?", (f"c:{dst}", f"c:{src}"))
            self._x("UPDATE messages SET sender_id=? WHERE sender_id=?", (dst, src))
            self._x("UPDATE outbox SET contact_id=? WHERE contact_id=?", (dst, src))
            self._x("INSERT OR IGNORE INTO group_members SELECT gid, ? FROM group_members "
                    "WHERE contact_id=?", (dst, src))
            self._x("DELETE FROM contacts WHERE id=?", (src,))

    # -- IP -----------------------------------------------------------------
    def ips(self, cid):
        return [r[0] for r in self._q(
            "SELECT ip FROM contact_ips WHERE contact_id=? ORDER BY last_seen DESC, manual DESC, ip",
            (cid,))]

    def add_ip(self, cid, ip, manual=False, seen=False):
        with self._lock:
            self._x("INSERT OR IGNORE INTO contact_ips (contact_id, ip, manual) VALUES (?, ?, ?)",
                    (cid, ip, int(manual)))
            if manual:
                self._x("UPDATE contact_ips SET manual=1 WHERE contact_id=? AND ip=?", (cid, ip))
            if seen:
                self._x("UPDATE contact_ips SET last_seen=? WHERE contact_id=? AND ip=?",
                        (time.time(), cid, ip))

    def remove_ip(self, cid, ip):
        self._x("DELETE FROM contact_ips WHERE contact_id=? AND ip=?", (cid, ip))

    def move_ip(self, ip, port, to_cid):
        """L'IP a été réattribuée (DHCP) : on la retire aux autres contacts du même port."""
        with self._lock:
            self._x("DELETE FROM contact_ips WHERE ip=? AND contact_id!=? AND contact_id IN "
                    "(SELECT id FROM contacts WHERE port=?)", (ip, to_cid, port))
            self.add_ip(to_cid, ip, seen=True)

    # -- groupes ------------------------------------------------------------
    def create_group(self, gid, name, member_ids):
        with self._lock:
            self._x("INSERT INTO groups VALUES (?, ?, ?)", (gid, name, time.time()))
            self.add_group_members(gid, member_ids)

    def get_group(self, gid):
        rows = self._q("SELECT gid, name FROM groups WHERE gid=?", (gid,))
        return Group(*rows[0]) if rows else None

    def groups(self):
        return [Group(*r) for r in self._q("SELECT gid, name FROM groups ORDER BY name COLLATE NOCASE")]

    def rename_group(self, gid, name):
        self._x("UPDATE groups SET name=? WHERE gid=?", (name, gid))

    def group_member_ids(self, gid):
        return [r[0] for r in self._q("SELECT contact_id FROM group_members WHERE gid=?", (gid,))]

    def add_group_members(self, gid, member_ids):
        with self._lock:
            for cid in member_ids:
                self._x("INSERT OR IGNORE INTO group_members VALUES (?, ?)", (gid, cid))

    def remove_group_member(self, gid, cid):
        self._x("DELETE FROM group_members WHERE gid=? AND contact_id=?", (gid, cid))

    def delete_group(self, gid):
        with self._lock:
            self._delete_conv_messages(f"g:{gid}")
            self._x("DELETE FROM groups WHERE gid=?", (gid,))

    # -- photos ---------------------------------------------------------------
    def get_avatar(self, owner):
        """(sha, chemin, date) de la photo de ``owner``, ou None."""
        rows = self._q("SELECT sha, path, ts FROM avatars WHERE owner=?", (owner,))
        return rows[0] if rows else None

    def set_avatar(self, owner, sha, path, ts):
        self._x("INSERT OR REPLACE INTO avatars VALUES (?, ?, ?, ?)", (owner, sha, path, ts))

    def delete_avatar(self, owner):
        self._x("DELETE FROM avatars WHERE owner=?", (owner,))

    # -- messages -----------------------------------------------------------
    _M = ("SELECT id, conv, sender_id, kind, text, ts, status, "
          "(SELECT MAX(e.ts) FROM message_edits e WHERE e.msg_id = messages.id), reply_to, deleted, "
          "mentions FROM messages")

    def add_message(self, msg_id, conv, sender_id, text, ts, status, kind="text", unread=False,
                    reply_to=None, mentions=None):
        """Insère un message ; renvoie None si déjà connu (doublon)."""
        try:
            self._x("INSERT INTO messages (id, conv, sender_id, kind, text, ts, status, unread, reply_to, "
                    "mentions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (msg_id, conv, sender_id, kind, text, ts, status, int(unread), reply_to,
                     json.dumps(mentions) if mentions else None))
        except sqlite3.IntegrityError:
            return None
        return Message(msg_id, conv, sender_id, kind, text, ts, status, reply_to=reply_to,
                       mentions=list(mentions or []))

    def delete_message(self, msg_id, ts):
        """Supprime le contenu d'un message (texte, versions, réactions, pièce jointe) en gardant sa
        place dans la conversation. Renvoie le chemin de la pièce jointe à effacer, '' s'il n'y en a
        pas, ou None si le message est inconnu ou déjà supprimé."""
        with self._lock:
            m = self.get_message(msg_id)
            if not m or m.deleted is not None or m.kind == "system":
                return None
            att = self.get_attachment(msg_id)
            self._x("UPDATE messages SET text='', deleted=? WHERE id=?", (ts, msg_id))
            self._x("DELETE FROM message_edits WHERE msg_id=?", (msg_id,))
            self._x("DELETE FROM reactions WHERE msg_id=?", (msg_id,))
            self._x("DELETE FROM attachments WHERE msg_id=?", (msg_id,))
            self._x("DELETE FROM outbox WHERE msg_id=?", (msg_id,))
            return att.path if att else ""

    def messages(self, conv, limit=500):
        rows = self._q(self._M + " WHERE conv=? ORDER BY ts DESC LIMIT ?", (conv, limit))
        return [Message(*r) for r in reversed(rows)]

    def get_message(self, msg_id):
        rows = self._q(self._M + " WHERE id=?", (msg_id,))
        return Message(*rows[0]) if rows else None

    def set_message_status(self, msg_id, status):
        self._x("UPDATE messages SET status=? WHERE id=?", (status, msg_id))

    def last_message(self, conv):
        rows = self._q(self._M + " WHERE conv=? ORDER BY ts DESC LIMIT 1", (conv,))
        return Message(*rows[0]) if rows else None

    def unread_count(self, conv):
        return self._q("SELECT COUNT(*) FROM messages WHERE conv=? AND unread=1", (conv,))[0][0]

    def mark_read(self, conv):
        self._x("UPDATE messages SET unread=0 WHERE conv=? AND unread=1", (conv,))

    # -- modifications ----------------------------------------------------------
    def edit_message(self, msg_id, text, ts):
        """Remplace le texte d'un message en archivant l'ancien ; renvoie True si appliqué.
        Une modification plus ancienne que la dernière connue est ignorée."""
        with self._lock:
            m = self.get_message(msg_id)
            if (not m or m.deleted is not None or m.text == text
                    or (m.edited is not None and ts <= m.edited)):
                return False
            try:
                self._x("INSERT INTO message_edits VALUES (?, ?, ?)", (msg_id, m.text, ts))
            except sqlite3.IntegrityError:
                return False
            self._x("UPDATE messages SET text=? WHERE id=?", (text, msg_id))
            return True

    def message_history(self, msg_id):
        """Versions successives du message, de l'originale à l'actuelle : [(texte, date)].
        La date est celle de l'envoi pour l'originale, puis celle de chaque modification."""
        m = self.get_message(msg_id)
        if not m:
            return []
        rows = self._q("SELECT text, ts FROM message_edits WHERE msg_id=? ORDER BY ts", (msg_id,))
        dates = [m.ts] + [ts for _t, ts in rows]
        texts = [t for t, _ts in rows] + [m.text]
        return list(zip(texts, dates))

    # -- réactions ---------------------------------------------------------------
    def set_reaction(self, msg_id, reactor, emoji, on):
        """Ajoute ou retire une réaction ; renvoie True si l'état a changé."""
        if on:
            cur = self._x("INSERT OR IGNORE INTO reactions VALUES (?, ?, ?, ?)",
                          (msg_id, reactor, emoji, time.time()))
        else:
            cur = self._x("DELETE FROM reactions WHERE msg_id=? AND reactor=? AND emoji=?",
                          (msg_id, reactor, emoji))
        return cur.rowcount > 0

    def has_reaction(self, msg_id, reactor, emoji):
        return bool(self._q("SELECT 1 FROM reactions WHERE msg_id=? AND reactor=? AND emoji=?",
                            (msg_id, reactor, emoji)))

    def reactions(self, msg_id):
        """Liste de (emoji, clé de la personne), dans l'ordre d'arrivée."""
        return self._q("SELECT emoji, reactor FROM reactions WHERE msg_id=? ORDER BY ts", (msg_id,))

    # -- pièces jointes ---------------------------------------------------------
    def add_attachment(self, msg_id, name, size, mime, path):
        self._x("INSERT OR REPLACE INTO attachments VALUES (?, ?, ?, ?, ?)",
                (msg_id, name, size, mime, path))

    def get_attachment(self, msg_id):
        rows = self._q("SELECT msg_id, name, size, mime, path FROM attachments WHERE msg_id=?", (msg_id,))
        return Attachment(*rows[0]) if rows else None

    def attachment_paths(self, conv):
        return [r[0] for r in self._q("SELECT a.path FROM attachments a JOIN messages m ON m.id = a.msg_id "
                                      "WHERE m.conv=?", (conv,))]

    # -- file d'envoi -------------------------------------------------------
    def enqueue(self, cid, payload: dict, msg_id=None):
        self._x("INSERT INTO outbox (contact_id, msg_id, payload, created) VALUES (?, ?, ?, ?)",
                (cid, msg_id, json.dumps(payload), time.time()))

    def outbox_for(self, cid):
        return [(r[0], r[1], json.loads(r[2])) for r in self._q(
            "SELECT id, msg_id, payload FROM outbox WHERE contact_id=? ORDER BY id", (cid,))]

    def outbox_done(self, outbox_id, msg_id):
        """Retire un élément ; renvoie True si ``msg_id`` n'a plus aucun destinataire en attente."""
        with self._lock:
            self._x("DELETE FROM outbox WHERE id=?", (outbox_id,))
            if msg_id is None:
                return False
            left = self._q("SELECT COUNT(*) FROM outbox WHERE msg_id=?", (msg_id,))[0][0]
            return left == 0

    def contacts_needing_delivery(self):
        return [r[0] for r in self._q(
            "SELECT DISTINCT contact_id FROM outbox UNION SELECT id FROM contacts WHERE vk IS NULL")]
