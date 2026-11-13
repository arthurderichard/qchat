"""Cœur réseau de QChat : pair-à-pair, sans serveur central.

* TCP (port 45455 par défaut) : sessions chiffrées de bout en bout
  (voir ``crypto.Handshake``) pour livrer messages et opérations de groupe.
* UDP (port 45454) : annonces signées diffusées sur chaque interface, pour
  découvrir les pairs et associer automatiquement toutes les IP d'une même
  personne (Wi-Fi, Ethernet, ...).

Toutes les méthodes publiques sont appelables depuis n'importe quel thread ;
les événements sont remontés via ``on_event(nom, données)`` depuis des threads
d'arrière-plan.
"""

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import netutil
from .crypto import PROTO, Handshake, Identity, ProtocolError, is_valid_vk, open_signed, sign_object
from .store import Store

log = logging.getLogger("qchat")

DEFAULT_PORT = 45455
DISCOVERY_PORT = 45454
ANNOUNCE_INTERVAL = 30
RETRY_INTERVAL = 20
ONLINE_TIMEOUT = 75
MAX_AVATAR = 64 * 1024          # photo de profil ou de groupe (déjà réduite par l'interface)
AVATAR_RETRY = 300              # délai avant de redemander une photo à un contact
WIZZ_KINDS = ("wizz", "poke")
WIZZ_COOLDOWN = 30              # anti-spam : un wizz/poke toutes les 30 s (envoi et affichage)
WIZZ_MAX_AGE = 120              # un wizz reçu en retard (destinataire hors ligne) ne s'affiche plus
ANNOUNCE_MAX_AGE = 15 * 60
MAX_FRAME = 256 * 1024
MAX_TEXT = 16000
MAX_FILE = 200 * 1024 * 1024
FILE_CHUNK = 192 * 1024
MAX_NAME = 64
CONNECT_TIMEOUT = 3
SCAN_CONNECT_TIMEOUT = 0.7
SCAN_WORKERS = 128
SCAN_KEEP = 10 * 60
IO_TIMEOUT = 15


def default_home():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "qchat")


def _clean_name(name, fallback="Inconnu"):
    if not isinstance(name, str):
        return fallback
    name = " ".join(name.split())[:MAX_NAME]
    return name or fallback


def _clean_ips(ips):
    if not isinstance(ips, list):
        return []
    return [ip for ip in (netutil.parse_ip(x) if isinstance(x, str) else None for x in ips[:16]) if ip]


MAX_FILENAME_BYTES = 200     # Linux limite un nom à 255 octets ; marge pour les doublons éventuels
_SUFFIX = re.compile(r"(\.[A-Za-z0-9]{1,10}){1,2}$")


def _truncate_bytes(text, limit):
    return text.encode()[:max(limit, 0)].decode(errors="ignore")


def safe_filename(name):
    """Nom de fichier sans chemin ni caractère dangereux, raccourci si besoin
    en conservant l'extension (« .png », « .tar.gz »...)."""
    name = os.path.basename(str(name).replace("\\", "/"))
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '/\0')
    name = name.strip().lstrip(".")
    if len(name.encode()) > MAX_FILENAME_BYTES:
        match = _SUFFIX.search(name)
        suffix = match.group(0) if match and match.start() > 0 else ""
        stem = name[:len(name) - len(suffix)]
        name = _truncate_bytes(stem, MAX_FILENAME_BYTES - len(suffix.encode())).rstrip() + suffix
    return name or "fichier"


# Signatures (premiers octets) des formats courants, pour les fichiers sans extension
# (ex. image glissée depuis un navigateur).
_MAGIC = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (8, b"WEBP", "image/webp"),
    (0, b"BM", "image/bmp"),
    (0, b"II*\x00", "image/tiff"),
    (0, b"MM\x00*", "image/tiff"),
    (4, b"ftypavif", "image/avif"),
    (4, b"ftypheic", "image/heic"),
    (4, b"ftypmif1", "image/heic"),
    (0, b"%PDF-", "application/pdf"),
)
_EXTENSIONS = {"image/jpeg": ".jpg", "image/avif": ".avif", "image/heic": ".heic"}


def sniff_mime(path):
    """Type MIME déduit du contenu du fichier, ou None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    for offset, magic, mime in _MAGIC:
        if head[offset:offset + len(magic)] == magic:
            return mime
    return None


def _sniff_bytes(data):
    """Type MIME d'une image d'après ses premiers octets (photos de profil), ou None."""
    for offset, magic, mime in _MAGIC:
        if mime.startswith("image/") and data[offset:offset + len(magic)] == magic:
            return mime
    return None


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def valid_emoji(emoji):
    """Une réaction est un emoji court : pas de texte, d'espace ni de caractère de contrôle."""
    return (isinstance(emoji, str) and 0 < len(emoji) <= 16
            and all(ord(ch) >= 0x2000 and ch.isprintable() or ch in "\u200d\ufe0f" for ch in emoji))


def detect_file(path, name):
    """Renvoie (nom, type MIME). Si l'extension ne dit rien, le type est déduit du contenu
    et l'extension correspondante est ajoutée au nom."""
    mime = mimetypes.guess_type(name)[0]
    if mime:
        return name, mime
    mime = sniff_mime(path)
    if not mime:
        return name, "application/octet-stream"
    ext = _EXTENSIONS.get(mime) or mimetypes.guess_extension(mime) or ""
    return safe_filename(name + ext), mime


_MSG_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _clean_port(port):
    return port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT


class Connection:
    """Socket TCP avec trames préfixées par leur longueur et canal chiffré."""

    def __init__(self, sock):
        self.sock = sock
        self.channel = None
        self.peer = None

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def send_raw(self, data: bytes):
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def recv_raw(self) -> bytes:
        (n,) = struct.unpack(">I", self._recv_exact(4))
        if n > MAX_FRAME:
            raise ProtocolError("trame trop grande")
        return self._recv_exact(n)

    def send_json(self, obj):
        self.send_raw(json.dumps(obj).encode())

    def recv_json(self):
        try:
            return json.loads(self.recv_raw())
        except (ValueError, UnicodeDecodeError) as e:
            raise ProtocolError("JSON invalide") from e

    def send(self, obj):
        self.send_raw(self.channel.encrypt(json.dumps(obj).encode()))

    def send_blob(self, data: bytes):
        self.send_raw(self.channel.encrypt(data))

    def recv_blob(self) -> bytes:
        return self.channel.decrypt(self.recv_raw())

    def recv(self):
        try:
            obj = json.loads(self.channel.decrypt(self.recv_raw()))
        except (ValueError, UnicodeDecodeError) as e:
            raise ProtocolError("JSON invalide") from e
        if not isinstance(obj, dict):
            raise ProtocolError("objet attendu")
        return obj

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Core:
    def __init__(self, home=None, port=DEFAULT_PORT, discovery=True, on_event=None):
        self.home = home or default_home()
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.identity = Identity.load_or_create(os.path.join(self.home, "identity.key"))
        self.store = Store(os.path.join(self.home, "qchat.db"))
        self.files_dir = os.path.join(self.home, "files")
        os.makedirs(self.files_dir, mode=0o700, exist_ok=True)
        self.avatars_dir = os.path.join(self.home, "avatars")
        os.makedirs(self.avatars_dir, mode=0o700, exist_ok=True)
        self._avatar_asked = {}   # contact_id -> (sha demandé, date)
        self._wizz_sent = 0.0     # date de mon dernier wizz/poke, toutes conversations confondues
        self._wizz_seen = {}      # contact_id -> date du dernier wizz affiché
        self.port = port
        self.discovery_enabled = discovery
        self.on_event = on_event or (lambda name, data: None)
        self.last_seen = {}       # contact_id -> horodatage
        self.discovered = {}      # vk -> {name, ips, port, seen} (pairs non ajoutés)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qchat-send")
        self._flush_lock = threading.Lock()
        self._flushing = set()
        self._flush_again = set()
        self._threads = []
        self._tcp = None
        self._udp = None

    # ======================================================================
    # Cycle de vie
    # ======================================================================
    @property
    def name(self):
        return self.store.get_setting("name", "")

    def set_name(self, name):
        self.store.set_setting("name", _clean_name(name, "Anonyme"))
        self._wake.set()

    def start(self):
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("0.0.0.0", self.port))
        self._tcp.listen(32)
        self._spawn(self._accept_loop)
        if self.discovery_enabled:
            try:
                self._udp = self._open_udp()
                self._spawn(self._discovery_loop)
            except OSError as e:
                log.warning("Découverte UDP indisponible : %s", e)
        self._spawn(self._scheduler_loop)
        log.info("QChat à l'écoute sur le port %d (empreinte %s)", self.port, self.identity.fingerprint)

    def stop(self):
        self._stop.set()
        self._wake.set()
        for s in (self._tcp, self._udp):
            if s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for t in self._threads:
            t.join(timeout=2)
        self.store.close()

    def _spawn(self, target, *args):
        t = threading.Thread(target=target, args=args, daemon=True, name=f"qchat-{target.__name__}")
        t.start()
        self._threads.append(t)

    def _emit(self, name, **data):
        try:
            self.on_event(name, data)
        except Exception:
            log.exception("Erreur dans le gestionnaire d'événement %s", name)

    def _profile(self):
        mine = self.store.get_avatar("me")
        return {"name": self.name or "Anonyme", "ips": netutil.local_ips(), "port": self.port,
                "avatar": mine[0] if mine else ""}

    # ======================================================================
    # Photos de profil et de groupe
    # ======================================================================
    @staticmethod
    def _avatar_owner_of(contact):
        return f"vk:{contact.vk}" if contact and contact.vk else None

    def avatar_path(self, conv_or_owner):
        """Chemin de la photo de 'me', d'un contact ('c:<id>') ou d'un groupe ('g:<gid>'), ou None."""
        owner = conv_or_owner
        if owner.startswith("c:"):
            owner = self._avatar_owner_of(self.store.get_contact(int(owner[2:])))
            if owner is None:
                return None
        row = self.store.get_avatar(owner)
        return row[1] if row and os.path.isfile(row[1]) else None

    def _store_avatar(self, owner, data, ts):
        """Enregistre une photo (déjà validée) ; renvoie son SHA-256."""
        sha = hashlib.sha256(data).hexdigest()
        old = self.store.get_avatar(owner)
        tag = hashlib.sha256(owner.encode()).hexdigest()[:16]
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(_sniff_bytes(data), ".jpg")
        path = os.path.join(self.avatars_dir, f"{tag}-{sha[:16]}{ext}")
        tmp = path + ".part"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self.store.set_avatar(owner, sha, path, ts)
        if old and old[1] != path:
            _unlink(old[1])
        return sha

    def _drop_avatar(self, owner):
        old = self.store.get_avatar(owner)
        if old:
            self.store.delete_avatar(owner)
            _unlink(old[1])
        return bool(old)

    @staticmethod
    def _valid_avatar(data):
        return isinstance(data, bytes) and 0 < len(data) <= MAX_AVATAR and _sniff_bytes(data) is not None

    @staticmethod
    def _decode_avatar(sha, data):
        """Photo reçue : bytes valides et conformes à leur empreinte, ou None."""
        if not (isinstance(sha, str) and isinstance(data, str) and len(data) <= MAX_AVATAR * 4 // 3 + 4):
            return None
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            return None
        ok = Core._valid_avatar(raw) and hashlib.sha256(raw).hexdigest() == sha
        return raw if ok else None

    def _my_avatar_payload(self):
        row = self.store.get_avatar("me")
        payload = {"t": "avatar", "id": uuid.uuid4().hex, "sha": row[0] if row else ""}
        if row:
            try:
                with open(row[1], "rb") as f:
                    payload["data"] = base64.b64encode(f.read()).decode()
            except OSError:
                payload["sha"] = ""
        return payload

    def set_avatar(self, data):
        """Change ma photo de profil (bytes d'une image PNG/JPEG…) ; None la retire.
        Elle est envoyée à tous mes contacts, y compris ceux hors ligne (file d'envoi)."""
        if data is None:
            if not self._drop_avatar("me"):
                return False
        elif self._valid_avatar(data):
            self._store_avatar("me", data, time.time())
        else:
            raise ValueError("image invalide ou trop lourde")
        payload = self._my_avatar_payload()
        targets = [c.id for c in self.store.contacts() if c.vk]
        for cid in targets:
            self.store.enqueue(cid, payload)
        self._emit("avatars_changed")
        self.request_flush(targets)
        return True

    def _group_avatar_card(self, gid):
        row = self.store.get_avatar(f"g:{gid}")
        if not row:
            return None
        try:
            with open(row[1], "rb") as f:
                return {"sha": row[0], "data": base64.b64encode(f.read()).decode(), "ts": row[2]}
        except OSError:
            return None

    def set_group_avatar(self, gid, data):
        """Change la photo d'un groupe (n'importe quel membre peut le faire) ; None la retire."""
        if not self.store.get_group(gid):
            return False
        ts = time.time()
        owner = f"g:{gid}"
        row = self.store.get_avatar(owner)
        ts = max(ts, (row[2] + 0.001) if row else 0)
        if data is None:
            if not row:
                return False
            self._drop_avatar(owner)
            # Marqueur de retrait daté, pour qu'une ancienne photo reçue plus tard ne revienne pas.
            self.store.set_setting(f"gavatar_removed:{gid}", str(ts))
            payload = {"t": "gavatar", "id": uuid.uuid4().hex, "gid": gid, "sha": "", "ts": ts}
            self._system(owner, "Vous avez retiré la photo du groupe.")
        elif self._valid_avatar(data):
            sha = self._store_avatar(owner, data, ts)
            payload = {"t": "gavatar", "id": uuid.uuid4().hex, "gid": gid, "sha": sha, "ts": ts,
                       "data": base64.b64encode(data).decode()}
            self._system(owner, "Vous avez changé la photo du groupe.")
        else:
            raise ValueError("image invalide ou trop lourde")
        members = self.store.group_member_ids(gid)
        for cid in members:
            self.store.enqueue(cid, payload)
        self._emit("avatars_changed")
        self.request_flush(members)
        return True

    def _apply_group_avatar(self, gid, sha, data, ts):
        """Photo de groupe reçue : appliquée si plus récente que celle connue. Renvoie True si changée."""
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not isinstance(sha, str):
            return False
        ts = min(float(ts), time.time() + 60)
        owner = f"g:{gid}"
        row = self.store.get_avatar(owner)
        removed = float(self.store.get_setting(f"gavatar_removed:{gid}", "0") or 0)
        if (row and ts <= row[2]) or ts <= removed:
            return False
        if sha == "":
            if not row:
                return False
            self._drop_avatar(owner)
            self.store.set_setting(f"gavatar_removed:{gid}", str(ts))
            return True
        raw = self._decode_avatar(sha, data)
        if raw is None or (row and row[0] == sha):
            return False
        self._store_avatar(owner, raw, ts)
        return True

    def _check_peer_avatar(self, cid, info):
        """Hello / annonce signés : si la photo annoncée diffère de celle connue, on la demande."""
        sha = info.get("avatar")
        c = self.store.get_contact(cid)
        owner = self._avatar_owner_of(c)
        if not isinstance(sha, str) or owner is None:
            return          # version sans photos de profil
        row = self.store.get_avatar(owner)
        if sha == "":
            if row and self._drop_avatar(owner):
                self._emit("avatars_changed")
            return
        if row and row[0] == sha:
            return
        asked = self._avatar_asked.get(cid)
        if asked and asked[0] == sha and time.time() - asked[1] < AVATAR_RETRY:
            return
        self._avatar_asked[cid] = (sha, time.time())
        self.store.enqueue(cid, {"t": "avatar?", "id": uuid.uuid4().hex})
        self.request_flush([cid])

    # ======================================================================
    # API publique (appelée par l'interface)
    # ======================================================================
    def is_online(self, cid):
        return time.time() - self.last_seen.get(cid, 0) < ONLINE_TIMEOUT

    def add_contact(self, name, endpoints):
        """``endpoints`` : liste de (ip, port). Renvoie l'id du contact."""
        port = endpoints[0][1] if endpoints else self.port
        cid = self.store.add_contact(_clean_name(name), port, ips=[ip for ip, _ in endpoints], manual=True)
        self._emit("contacts_changed")
        self.request_flush([cid])   # récupère sa clé publique tout de suite
        return cid

    def add_discovered(self, vk):
        peer = self.discovered.pop(vk, None)
        if not peer or self.store.contact_by_vk(vk):
            return None
        cid = self.store.add_contact(peer["name"], peer["port"], vk=vk, ips=peer["ips"])
        self.last_seen[cid] = peer["seen"]
        self._emit("contacts_changed")
        return cid

    def scan(self, hosts, port=DEFAULT_PORT, on_progress=None, on_found=None, cancel=None):
        """Sonde activement ``hosts`` (liste d'IP) à la recherche de postes QChat.

        Pour chaque poste qui répond, on obtient son identité signée (nom, clé, IP)
        sans finaliser la session : rien n'est créé ni affiché chez la personne sondée.
        Les pairs inconnus sont ajoutés à ``discovered`` ; les IP des contacts connus sont
        mises à jour. Bloquant : à appeler depuis un thread.
        """
        own = set(netutil.local_ips())
        hosts = [h for h in hosts if h not in own]
        cancel = cancel or threading.Event()

        def probe(ip):
            if cancel.is_set() or self._stop.is_set():
                return None
            return self._probe(ip, port)

        done = 0
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="qchat-scan") as pool:
            futures = {pool.submit(probe, ip): ip for ip in hosts}
            for fut in as_completed(futures):
                done += 1
                peer = fut.result()
                if peer:
                    info = self._record_scan(peer, futures[fut])
                    if on_found:
                        on_found(info)
                if on_progress:
                    on_progress(done, len(hosts))
                if cancel.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

    def _probe(self, ip, port):
        try:
            sock = socket.create_connection((ip, port), timeout=SCAN_CONNECT_TIMEOUT)
        except OSError:
            return None
        sock.settimeout(CONNECT_TIMEOUT)
        conn = Connection(sock)
        try:
            hs = Handshake(self.identity, self._profile(), initiator=True)
            conn.send_json(hs.initiate())
            hs.on_response(conn.recv_json())   # réponse signée : identité authentique
            return hs.peer                     # on n'envoie pas la confirmation
        except (OSError, EOFError, ProtocolError):
            return None
        finally:
            conn.close()

    def _record_scan(self, peer, ip):
        vk = peer["vk"]
        ips = _clean_ips(peer.get("ips"))
        if ip not in ips:
            ips.insert(0, ip)
        info = {"vk": vk, "name": _clean_name(peer.get("name")), "ips": ips,
                "port": _clean_port(peer.get("port")), "contact_id": None}
        c = self.store.contact_by_vk(vk)
        if c:
            self._learn(c.id, peer, ip)
            info["contact_id"] = c.id
            self._emit("contacts_changed")
            return info
        prev = self.discovered.get(vk)
        if prev:
            info["ips"] = list(dict.fromkeys(ips + prev["ips"]))
        self.discovered[vk] = {"name": info["name"], "ips": info["ips"], "port": info["port"],
                               "seen": time.time(), "scanned": True}
        self._emit("discovered")
        return info

    def add_contact_ip(self, cid, ip):
        self.store.add_ip(cid, ip, manual=True)
        self._emit("contacts_changed")
        self.request_flush([cid])

    def remove_contact_ip(self, cid, ip):
        self.store.remove_ip(cid, ip)
        self._emit("contacts_changed")

    def rename_contact(self, cid, name):
        self.store.rename_contact(cid, _clean_name(name))
        self._emit("contacts_changed")

    def delete_contact(self, cid):
        self._delete_files(f"c:{cid}")
        self.store.delete_contact(cid)
        self.last_seen.pop(cid, None)
        self._emit("contacts_changed")
        self._emit("groups_changed")

    def accept_pending_key(self, cid):
        c = self.store.get_contact(cid)
        if not c or not c.pending_vk:
            return
        other = self.store.contact_by_vk(c.pending_vk)
        if other:
            self.store.merge_contacts(other.id, cid)
        self.store.set_vk(cid, c.pending_vk)
        self._system(c.conv, "Nouvelle clé de sécurité acceptée.")
        self._emit("contacts_changed")
        self.request_flush([cid])

    def reject_pending_key(self, cid):
        """La nouvelle clé appartient à quelqu'un d'autre : on retire l'IP concernée."""
        c = self.store.get_contact(cid)
        if not c or not c.pending_vk:
            return
        if c.pending_ip:
            self.store.remove_ip(cid, c.pending_ip)
        self.store.clear_pending_key(cid)
        self._emit("contacts_changed")

    def _targets(self, conv, payload):
        kind, ident = conv.split(":", 1)
        if kind == "g":
            payload["gid"] = ident
            return self.store.group_member_ids(ident)
        return [int(ident)]

    def send_text(self, conv, text):
        text = text.strip()[:MAX_TEXT]
        if not text:
            return None
        msg_id = uuid.uuid4().hex
        ts = time.time()
        payload = {"t": "msg", "id": msg_id, "ts": ts, "text": text}
        targets = self._targets(conv, payload)
        msg = self.store.add_message(msg_id, conv, None, text, ts,
                                     "pending" if targets else "delivered")
        for cid in targets:
            self.store.enqueue(cid, payload, msg_id)
        self._emit("message", message=msg)
        self.request_flush(targets)
        return msg

    def send_file(self, conv, path, name=None):
        """Partage un fichier (image ou autre) dans une conversation.

        Le fichier est copié dans le dossier de QChat : l'envoi peut reprendre plus tard
        (destinataire hors ligne) même si l'original est déplacé. Lève ValueError si le
        fichier est illisible ou trop gros.
        """
        if not os.path.isfile(path):
            raise ValueError(f"« {os.path.basename(path)} » n'est pas un fichier.")
        size = os.path.getsize(path)
        if size > MAX_FILE:
            raise ValueError(f"« {os.path.basename(path)} » dépasse la taille maximale "
                             f"({MAX_FILE // (1024 * 1024)} Mo).")
        msg_id = uuid.uuid4().hex
        name, mime = detect_file(path, safe_filename(name or os.path.basename(path)))
        dest_dir = os.path.join(self.files_dir, msg_id)
        os.makedirs(dest_dir, mode=0o700)
        dest = os.path.join(dest_dir, name)
        digest = hashlib.sha256()
        with open(path, "rb") as src, open(dest, "wb") as dst:
            while chunk := src.read(FILE_CHUNK):
                digest.update(chunk)
                dst.write(chunk)
        ts = time.time()
        payload = {"t": "file", "id": msg_id, "ts": ts, "name": name, "size": size, "mime": mime,
                   "sha256": digest.hexdigest()}
        targets = self._targets(conv, payload)
        msg = self.store.add_message(msg_id, conv, None, name, ts,
                                     "pending" if targets else "delivered", kind="file")
        self.store.add_attachment(msg_id, name, size, mime, dest)
        for cid in targets:
            self.store.enqueue(cid, payload, msg_id)
        self._emit("message", message=msg)
        self.request_flush(targets)
        return msg

    def react(self, msg_id, emoji):
        """Ajoute ma réaction ``emoji`` à un message, ou la retire si elle y est déjà."""
        m = self.store.get_message(msg_id)
        if not m or m.kind == "system" or not valid_emoji(emoji):
            return
        me = self.identity.vk_hex
        on = not self.store.has_reaction(msg_id, me, emoji)
        self.store.set_reaction(msg_id, me, emoji, on)
        payload = {"t": "react", "id": uuid.uuid4().hex, "ref": msg_id, "emoji": emoji, "on": on}
        targets = self._targets(m.conv, payload)
        for cid in targets:
            self.store.enqueue(cid, payload)
        self._emit("reaction", msg_id=msg_id, emoji=emoji, on=on, incoming=False)
        self.request_flush(targets)

    def edit_text(self, msg_id, text):
        """Modifie un de mes messages texte ; l'ancienne version reste dans l'historique."""
        m = self.store.get_message(msg_id)
        text = text.strip()[:MAX_TEXT]
        if not m or not m.outgoing or m.kind != "text" or not text or text == m.text:
            return False
        ts = max(time.time(), (m.edited or m.ts) + 0.001)
        self.store.edit_message(msg_id, text, ts)
        payload = {"t": "edit", "id": uuid.uuid4().hex, "ref": msg_id, "text": text, "ts": ts}
        targets = self._targets(m.conv, payload)
        for cid in targets:
            self.store.enqueue(cid, payload)
        self._emit("edited", msg_id=msg_id, incoming=False)
        self.request_flush(targets)
        return True

    def send_wizz(self, conv, kind="wizz"):
        """Envoie un wizz (ou un poke) : une fenêtre animée s'ouvre chez le destinataire.

        Renvoie le nombre de secondes à attendre si le dernier wizz est trop récent, sinon 0.
        """
        kind = kind if kind in WIZZ_KINDS else "wizz"
        now = time.time()
        wait = self._wizz_sent + WIZZ_COOLDOWN - now
        if wait > 0:
            return max(1, int(wait + 0.999))
        self._wizz_sent = now
        payload = {"t": "wizz", "id": uuid.uuid4().hex, "ts": now, "kind": kind}
        targets = self._targets(conv, payload)
        for cid in targets:
            self.store.enqueue(cid, payload)
        self._system(conv, f"Vous avez envoyé un {kind}.")
        self.request_flush(targets)
        return 0

    def _on_wizz(self, cid, obj):
        kind, ts = obj.get("kind"), obj.get("ts")
        if kind not in WIZZ_KINDS:
            kind = "wizz"
        gid = obj.get("gid")
        if gid is not None:
            if not isinstance(gid, str) or cid not in self.store.group_member_ids(gid):
                return
            conv = f"g:{gid}"
        else:
            conv = f"c:{cid}"
        c = self.store.get_contact(cid)
        if not c:
            return
        now = time.time()
        fresh = (isinstance(ts, (int, float)) and not isinstance(ts, bool)
                 and now - ts <= WIZZ_MAX_AGE)
        self._system(conv, f"{c.name} vous a envoyé un {kind}.")
        if fresh and now - self._wizz_seen.get(cid, 0) >= WIZZ_COOLDOWN:
            self._wizz_seen[cid] = now
            self._emit("wizz", contact_id=cid, conv=conv, kind=kind)

    def _delete_files(self, conv):
        for path in self.store.attachment_paths(conv):
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def create_group(self, name, member_ids):
        gid = uuid.uuid4().hex
        name = _clean_name(name, "Groupe")
        member_ids = [cid for cid in member_ids if self._keyed(cid)]
        self.store.create_group(gid, name, member_ids)
        self._system(f"g:{gid}", "Vous avez créé le groupe.")
        self._send_group_invite(gid, member_ids, member_ids)
        self._emit("groups_changed")
        return gid

    def add_group_members(self, gid, new_ids):
        new_ids = [cid for cid in new_ids if self._keyed(cid)
                   and cid not in self.store.group_member_ids(gid)]
        if not new_ids:
            return
        self.store.add_group_members(gid, new_ids)
        names = ", ".join(self.store.get_contact(c).name for c in new_ids)
        self._system(f"g:{gid}", f"Vous avez ajouté {names}.")
        self._send_group_invite(gid, self.store.group_member_ids(gid), new_ids)
        self._emit("groups_changed")

    def rename_group(self, gid, name):
        name = _clean_name(name, "Groupe")
        self.store.rename_group(gid, name)
        self._send_group_invite(gid, self.store.group_member_ids(gid), [])
        self._emit("groups_changed")

    def leave_group(self, gid):
        members = self.store.group_member_ids(gid)
        for cid in members:
            self.store.enqueue(cid, {"t": "leave", "gid": gid})
        self._delete_files(f"g:{gid}")
        self._drop_avatar(f"g:{gid}")
        self.store.delete_group(gid)
        self._emit("groups_changed")
        self.request_flush(members)

    # ======================================================================
    # Groupes (interne)
    # ======================================================================
    def _keyed(self, cid):
        c = self.store.get_contact(cid)
        return bool(c and c.vk)

    def _member_card(self, cid):
        c = self.store.get_contact(cid)
        return {"vk": c.vk, "name": c.name, "ips": self.store.ips(cid), "port": c.port}

    def _send_group_invite(self, gid, recipients, added):
        g = self.store.get_group(gid)
        members = [self._member_card(cid) for cid in self.store.group_member_ids(gid) if self._keyed(cid)]
        members.append({"vk": self.identity.vk_hex, **self._profile()})
        payload = {"t": "group", "gid": gid, "name": g.name, "members": members,
                   "added": [self.store.get_contact(cid).vk for cid in added]}
        card = self._group_avatar_card(gid) if added else None
        if card:
            payload["avatar"] = card        # les nouveaux membres reçoivent la photo du groupe
        for cid in recipients:
            self.store.enqueue(cid, payload)
        self.request_flush(recipients)

    def _contact_from_card(self, card):
        """Retrouve ou crée le contact décrit par une fiche de membre de groupe."""
        if not isinstance(card, dict) or not isinstance(card.get("vk"), str):
            return None
        vk = card["vk"]
        if vk == self.identity.vk_hex or not is_valid_vk(vk):
            return None
        c = self.store.contact_by_vk(vk)
        if c:
            for ip in _clean_ips(card.get("ips")):
                self.store.add_ip(c.id, ip)
            return c.id
        self.discovered.pop(vk, None)
        return self.store.add_contact(_clean_name(card.get("name")), _clean_port(card.get("port")),
                                      vk=vk, ips=_clean_ips(card.get("ips")))

    def _system(self, conv, text):
        msg = self.store.add_message(uuid.uuid4().hex, conv, None, text, time.time(), "delivered",
                                     kind="system")
        self._emit("message", message=msg)

    # ======================================================================
    # Envoi
    # ======================================================================
    def request_flush(self, cids):
        for cid in set(cids):
            with self._flush_lock:
                if cid in self._flushing:
                    self._flush_again.add(cid)
                    continue
                self._flushing.add(cid)
            try:
                self._pool.submit(self._flush_worker, cid)
            except RuntimeError:     # arrêt en cours
                return

    def _flush_worker(self, cid):
        while not self._stop.is_set():
            try:
                self._flush(cid)
            except Exception:
                log.exception("Échec de livraison vers le contact %s", cid)
            with self._flush_lock:
                if cid in self._flush_again:
                    self._flush_again.discard(cid)
                    continue
                self._flushing.discard(cid)
                return

    def _flush(self, cid):
        contact = self.store.get_contact(cid)
        if not contact:
            return
        items = self.store.outbox_for(cid)
        if contact.vk and not items:
            return
        for ip in self.store.ips(cid):
            if self._stop.is_set():
                return
            if ip == contact.pending_ip:     # clé inattendue à cette IP : en attente de décision
                continue
            conn = self._dial(ip, contact.port)
            if not conn:
                continue
            try:
                target = self._resolve_outgoing(cid, ip, conn.peer)
                if target is None:
                    continue
                for outbox_id, msg_id, payload in self.store.outbox_for(target):
                    if payload.get("t") == "file":
                        if not self._send_file_data(conn, msg_id, payload):
                            self.store.outbox_done(outbox_id, msg_id)
                            continue
                    else:
                        conn.send(payload)
                    reply = conn.recv()
                    if reply.get("t") != "ack" or reply.get("ref") != payload.get("id"):
                        raise ProtocolError("accusé de réception inattendu")
                    if self.store.outbox_done(outbox_id, msg_id):
                        self.store.set_message_status(msg_id, "delivered")
                        self._emit("status", msg_id=msg_id, status="delivered")
                conn.send({"t": "bye"})
                return
            except (OSError, EOFError, ProtocolError) as e:
                log.info("Livraison interrompue vers %s : %s", ip, e)
            finally:
                conn.close()

    def _send_file_data(self, conn, msg_id, payload):
        att = self.store.get_attachment(msg_id)
        if not att or not os.path.isfile(att.path):
            log.warning("Pièce jointe %s introuvable, envoi abandonné", msg_id)
            self.store.set_message_status(msg_id, "failed")
            self._emit("status", msg_id=msg_id, status="failed")
            return False
        conn.send(payload)
        with open(att.path, "rb") as f:
            while chunk := f.read(FILE_CHUNK):
                conn.send_blob(chunk)
        return True

    def _dial(self, ip, port):
        try:
            sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        except OSError:
            return None
        sock.settimeout(IO_TIMEOUT)
        conn = Connection(sock)
        try:
            hs = Handshake(self.identity, self._profile(), initiator=True)
            conn.send_json(hs.initiate())
            conn.send_json(hs.on_response(conn.recv_json()))
            conn.channel = hs.channel()
            conn.peer = hs.peer
            return conn
        except (OSError, EOFError, ProtocolError) as e:
            log.info("Poignée de main échouée avec %s:%d : %s", ip, port, e)
            conn.close()
            return None

    def _resolve_outgoing(self, cid, ip, peer):
        """Vérifie que la clé trouvée à ``ip`` est bien celle du contact composé.

        Renvoie l'id du contact à qui livrer, ou None si la clé ne correspond pas.
        """
        contact = self.store.get_contact(cid)
        vk = peer["vk"]
        other = self.store.contact_by_vk(vk)
        if contact.vk is None:
            if other:                       # doublon d'un contact déjà connu
                self.store.merge_contacts(cid, other.id)
                cid = other.id
            else:                           # premier contact : on retient la clé (TOFU)
                self.store.set_vk(cid, vk)
                self._system(contact.conv, "Clé de sécurité enregistrée. Les échanges sont chiffrés "
                                           "de bout en bout.")
            self.discovered.pop(vk, None)
            self._learn(cid, peer, ip)
            self._emit("contacts_changed")
            return cid
        if contact.vk == vk:
            if contact.pending_vk:
                # La bonne clé répond sur une autre IP : l'IP suspecte a été réattribuée.
                self.store.remove_ip(cid, contact.pending_ip)
                self.store.clear_pending_key(cid)
                self._emit("contacts_changed")
            self._learn(cid, peer, ip)
            return cid
        # Une autre clé répond à cette IP.
        if other:
            self.store.move_ip(ip, contact.port, other.id)
            self._learn(other.id, peer, ip)
            self.request_flush([other.id])
        else:
            self.store.set_pending_key(cid, vk, ip)
            self._emit("key_changed", contact_id=cid)
        self._emit("contacts_changed")
        return None

    def _learn(self, cid, info, src_ip=None):
        """Met à jour IP, port et présence d'un contact à partir d'un hello/annonce signé."""
        was_online = self.is_online(cid)
        for ip in _clean_ips(info.get("ips")):
            self.store.add_ip(cid, ip)
        if src_ip:
            self.store.add_ip(cid, src_ip, seen=True)
        self.store.set_port(cid, _clean_port(info.get("port")))
        self.last_seen[cid] = time.time()
        self._check_peer_avatar(cid, info)
        if not was_online:
            self._emit("presence", contact_id=cid, online=True)

    # ======================================================================
    # Réception
    # ======================================================================
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, addr = self._tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._handle_incoming, args=(sock, addr[0]), daemon=True).start()

    def _handle_incoming(self, sock, src_ip):
        sock.settimeout(IO_TIMEOUT)
        conn = Connection(sock)
        try:
            hs = Handshake(self.identity, self._profile(), initiator=False)
            conn.send_json(hs.respond(conn.recv_json()))
            hs.on_confirm(conn.recv_json())
            conn.channel = hs.channel()
            cid = self._resolve_incoming(hs.peer, src_ip)
            while True:
                obj = conn.recv()
                if obj.get("t") == "bye":
                    break
                if obj.get("t") == "file":
                    self._receive_file(cid, conn, obj)   # lit aussi les blocs qui suivent
                else:
                    try:
                        self._process(cid, obj)
                    except Exception:
                        log.exception("Erreur de traitement d'un message entrant")
                conn.send({"t": "ack", "ref": obj.get("id")})
        except (OSError, EOFError, ProtocolError) as e:
            log.debug("Connexion entrante de %s terminée : %s", src_ip, e)
        finally:
            conn.close()

    def _resolve_incoming(self, peer, src_ip):
        vk = peer["vk"]
        c = self.store.contact_by_vk(vk)
        if c:
            cid = c.id
        else:
            port = _clean_port(peer.get("port"))
            pending = self.store.unkeyed_contacts_with_ip(src_ip, port)
            if pending:     # contact ajouté par IP, qui nous écrit avant qu'on l'ait joint
                cid = pending[0].id
                self.store.set_vk(cid, vk)
            else:
                cid = self.store.add_contact(_clean_name(peer.get("name")), port, vk=vk)
            self.discovered.pop(vk, None)
            self._emit("contacts_changed")
        self._learn(cid, peer, src_ip)
        self.request_flush([cid])   # il est joignable : on en profite pour vider sa file
        return cid

    def _process(self, cid, obj):
        t = obj.get("t")
        if t == "msg":
            self._on_msg(cid, obj)
        elif t == "group":
            self._on_group(cid, obj)
        elif t == "leave":
            self._on_leave(cid, obj)
        elif t == "react":
            self._on_react(cid, obj)
        elif t == "edit":
            self._on_edit(cid, obj)
        elif t == "avatar":
            self._on_avatar(cid, obj)
        elif t == "avatar?":
            self.store.enqueue(cid, self._my_avatar_payload())
            self.request_flush([cid])
        elif t == "gavatar":
            self._on_group_avatar(cid, obj)
        elif t == "wizz":
            self._on_wizz(cid, obj)

    def _on_msg(self, cid, obj):
        msg_id, text, ts = obj.get("id"), obj.get("text"), obj.get("ts")
        if not (isinstance(msg_id, str) and 0 < len(msg_id) <= 64 and isinstance(text, str)):
            return
        if not isinstance(ts, (int, float)):
            ts = time.time()
        ts = min(ts, time.time() + 60)
        gid = obj.get("gid")
        if gid is not None:
            if not isinstance(gid, str) or cid not in self.store.group_member_ids(gid):
                log.info("Message de groupe ignoré (expéditeur non membre)")
                return
            conv = f"g:{gid}"
        else:
            conv = f"c:{cid}"
        msg = self.store.add_message(msg_id, conv, cid, text[:MAX_TEXT], ts, "received", unread=True)
        if msg:
            self._emit("message", message=msg, incoming=True)

    def _receive_file(self, cid, conn, meta):
        msg_id, name, size, sha = meta.get("id"), meta.get("name"), meta.get("size"), meta.get("sha256")
        if not (isinstance(msg_id, str) and _MSG_ID.fullmatch(msg_id) and isinstance(name, str)
                and isinstance(size, int) and 0 <= size <= MAX_FILE and isinstance(sha, str)):
            raise ProtocolError("métadonnées de fichier invalides")
        gid = meta.get("gid")
        if gid is not None:
            accept = isinstance(gid, str) and cid in self.store.group_member_ids(gid)
            conv = f"g:{gid}"
        else:
            accept = True
            conv = f"c:{cid}"
        accept = accept and self.store.get_message(msg_id) is None
        tmp = os.path.join(self.files_dir, f".part-{uuid.uuid4().hex}")
        digest = hashlib.sha256()
        received = 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                while received < size:
                    blob = conn.recv_blob()
                    if not blob or received + len(blob) > size:
                        raise ProtocolError("taille de fichier incohérente")
                    received += len(blob)
                    digest.update(blob)
                    if accept:
                        f.write(blob)
            if digest.hexdigest() != sha:
                raise ProtocolError("empreinte du fichier incorrecte")
            if not accept:
                return
            # Type redéduit localement : on ne se fie pas à celui annoncé par l'expéditeur.
            name, mime = detect_file(tmp, safe_filename(name))
            dest_dir = os.path.join(self.files_dir, msg_id)
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        ts = meta.get("ts")
        ts = min(ts, time.time() + 60) if isinstance(ts, (int, float)) else time.time()
        msg = self.store.add_message(msg_id, conv, cid, name, ts, "received", kind="file", unread=True)
        if msg:
            self.store.add_attachment(msg_id, name, size, mime, dest)
            self._emit("message", message=msg, incoming=True)

    def _on_group(self, cid, obj):
        gid, name, members = obj.get("gid"), obj.get("name"), obj.get("members")
        if not (isinstance(gid, str) and 0 < len(gid) <= 64 and isinstance(members, list)):
            return
        sender = self.store.get_contact(cid)
        group = self.store.get_group(gid)
        if group is None:
            # Invitation : on rejoint le groupe avec la liste complète des membres.
            ids = {cid}
            ids.update(m for m in (self._contact_from_card(card) for card in members[:256]) if m)
            self.store.create_group(gid, _clean_name(name, "Groupe"), ids)
            card = obj.get("avatar")
            if isinstance(card, dict):
                self._apply_group_avatar(gid, card.get("sha"), card.get("data"), card.get("ts"))
            self._system(f"g:{gid}", f"{sender.name} vous a ajouté au groupe.")
            self._emit("contacts_changed")
            self._emit("groups_changed")
            self._emit("group_invite", gid=gid, contact_id=cid)
            return
        current = self.store.group_member_ids(gid)
        if cid not in current:
            return
        if isinstance(name, str) and _clean_name(name) != group.name:
            self.store.rename_group(gid, _clean_name(name))
            self._system(f"g:{gid}", f"{sender.name} a renommé le groupe en « {_clean_name(name)} ».")
        added_vks = set(obj.get("added") or [])
        new_names = []
        for card in members[:256]:
            mid = self._contact_from_card(card)
            if mid and mid not in current:
                self.store.add_group_members(gid, [mid])
                current.append(mid)
                if card.get("vk") in added_vks:
                    new_names.append(self.store.get_contact(mid).name)
        if new_names:
            self._system(f"g:{gid}", f"{sender.name} a ajouté {', '.join(new_names)}.")
        self._emit("contacts_changed")
        self._emit("groups_changed")

    def _on_react(self, cid, obj):
        ref, emoji, on = obj.get("ref"), obj.get("emoji"), obj.get("on")
        if not (isinstance(ref, str) and _MSG_ID.fullmatch(ref) and valid_emoji(emoji)
                and isinstance(on, bool)):
            return
        gid = obj.get("gid")
        if gid is not None:
            if not isinstance(gid, str) or cid not in self.store.group_member_ids(gid):
                return
            conv = f"g:{gid}"
        else:
            conv = f"c:{cid}"
        m = self.store.get_message(ref)
        if m and (m.conv != conv or m.kind == "system"):
            return      # on ne réagit qu'aux messages de la conversation où l'on se trouve
        # Message pas encore reçu (groupe : réaction arrivée avant lui) : on garde la réaction.
        if self.store.set_reaction(ref, self.store.get_contact(cid).vk, emoji, on):
            self._emit("reaction", msg_id=ref, contact_id=cid, emoji=emoji, on=on, incoming=True)

    def _on_edit(self, cid, obj):
        ref, text, ts = obj.get("ref"), obj.get("text"), obj.get("ts")
        if not (isinstance(ref, str) and _MSG_ID.fullmatch(ref) and isinstance(text, str)
                and isinstance(ts, (int, float)) and not isinstance(ts, bool)):
            return
        text = text.strip()[:MAX_TEXT]
        gid = obj.get("gid")
        conv = f"g:{gid}" if gid is not None else f"c:{cid}"
        m = self.store.get_message(ref)
        # Seul l'auteur peut modifier, dans la conversation d'origine, un message texte.
        if not text or not m or m.conv != conv or m.sender_id != cid or m.kind != "text":
            return
        if self.store.edit_message(ref, text, min(ts, time.time() + 60)):
            self._emit("edited", msg_id=ref, incoming=True)

    def _on_avatar(self, cid, obj):
        owner = self._avatar_owner_of(self.store.get_contact(cid))
        sha = obj.get("sha")
        if owner is None or not isinstance(sha, str):
            return
        if sha == "":
            changed = self._drop_avatar(owner)
        else:
            raw = self._decode_avatar(sha, obj.get("data"))
            row = self.store.get_avatar(owner)
            changed = raw is not None and not (row and row[0] == sha)
            if changed:
                self._store_avatar(owner, raw, time.time())
        self._avatar_asked.pop(cid, None)
        if changed:
            self._emit("avatars_changed")

    def _on_group_avatar(self, cid, obj):
        gid = obj.get("gid")
        if not isinstance(gid, str) or cid not in self.store.group_member_ids(gid):
            return
        if self._apply_group_avatar(gid, obj.get("sha"), obj.get("data"), obj.get("ts")):
            who = self.store.get_contact(cid).name
            self._system(f"g:{gid}", f"{who} a retiré la photo du groupe." if obj.get("sha") == ""
                         else f"{who} a changé la photo du groupe.")
            self._emit("avatars_changed")

    def _on_leave(self, cid, obj):
        gid = obj.get("gid")
        if not isinstance(gid, str) or cid not in self.store.group_member_ids(gid):
            return
        self.store.remove_group_member(gid, cid)
        self._system(f"g:{gid}", f"{self.store.get_contact(cid).name} a quitté le groupe.")
        self._emit("groups_changed")

    # ======================================================================
    # Découverte UDP
    # ======================================================================
    def _open_udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("0.0.0.0", DISCOVERY_PORT))
        return s

    def _announcement(self, query=False):
        body = {"proto": PROTO, "vk": self.identity.vk_hex, "ts": int(time.time()), **self._profile()}
        if query:
            body["q"] = 1
        return json.dumps(sign_object(self.identity, body)).encode()

    def announce(self, query=False):
        if not self._udp or not self.name:
            return
        data = self._announcement(query)
        targets = set(netutil.broadcast_addresses())
        # Unicast vers les contacts connus : utile si la diffusion est filtrée
        # ou si la personne est sur un autre sous-réseau.
        for c in self.store.contacts():
            targets.update(self.store.ips(c.id)[:4])
        for addr in targets:
            try:
                self._udp.sendto(data, (addr, DISCOVERY_PORT))
            except OSError:
                pass

    def _discovery_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(8192)
            except OSError:
                return
            if self._stop.is_set():
                return
            if not addr:
                continue
            src_ip = addr[0]
            try:
                body = open_signed(json.loads(data))
            except (ValueError, UnicodeDecodeError, ProtocolError):
                continue
            vk = body["vk"]
            if vk == self.identity.vk_hex:
                continue
            ts = body.get("ts")
            if not isinstance(ts, int) or abs(time.time() - ts) > ANNOUNCE_MAX_AGE:
                continue
            c = self.store.contact_by_vk(vk)
            if c:
                self._learn(c.id, body, src_ip)
                if self.store.outbox_for(c.id):
                    self.request_flush([c.id])
            else:
                ips = _clean_ips(body.get("ips"))
                if src_ip not in ips:
                    ips.insert(0, src_ip)
                is_new = vk not in self.discovered
                self.discovered[vk] = {"name": _clean_name(body.get("name")), "ips": ips,
                                       "port": _clean_port(body.get("port")), "seen": time.time()}
                if is_new:
                    self._emit("discovered")
            if body.get("q"):
                try:
                    self._udp.sendto(self._announcement(), (src_ip, DISCOVERY_PORT))
                except OSError:
                    pass

    # ======================================================================
    # Tâches périodiques
    # ======================================================================
    def _scheduler_loop(self):
        next_announce = 0
        first = True
        while not self._stop.is_set():
            now = time.time()
            if now >= next_announce:
                self.announce(query=first)
                first = False
                next_announce = now + ANNOUNCE_INTERVAL
                self._expire_presence()
            self.request_flush(self.store.contacts_needing_delivery())
            self._wake.wait(RETRY_INTERVAL)
            if self._wake.is_set():
                self._wake.clear()
                next_announce = 0

    def _expire_presence(self):
        now = time.time()
        for cid, seen in list(self.last_seen.items()):
            if now - seen >= ONLINE_TIMEOUT:
                del self.last_seen[cid]
                self._emit("presence", contact_id=cid, online=False)
        stale = [vk for vk, p in self.discovered.items()
                 if now - p["seen"] > (SCAN_KEEP if p.get("scanned") else 3 * ANNOUNCE_INTERVAL)]
        for vk in stale:
            del self.discovered[vk]
        if stale:
            self._emit("discovered")
