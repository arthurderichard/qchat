"""Primitives cryptographiques de QChat (PyNaCl / libsodium).

Identité
    Chaque installation possède une clé de signature Ed25519 persistante.
    La clé publique (``vk``) est l'identifiant cryptographique de la personne ;
    ses adresses IP sont rattachées à cette clé.

Session (chiffrement de bout en bout avec confidentialité persistante)
    1. initiateur -> répondeur : hello{vk, eph, ...} signé par l'initiateur
    2. répondeur -> initiateur : hello{vk, eph, ...} signé par le répondeur,
       la signature couvre aussi le hello de l'initiateur
    3. initiateur -> répondeur : confirmation signée sur la transcription
    Les clés de session sont dérivées du secret X25519 entre les deux clés
    éphémères, liées à la transcription (BLAKE2b), une par direction.
    Chaque trame est chiffrée en XSalsa20-Poly1305 avec un nonce = compteur,
    ce qui interdit le rejeu, la réorganisation et la suppression de trames.
"""

import hashlib
import json
import os

import nacl.bindings
import nacl.hash
from nacl.encoding import RawEncoder
from nacl.exceptions import BadSignatureError, CryptoError
from nacl.public import PrivateKey
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey

PROTO = "qchat-v1"


class ProtocolError(Exception):
    pass


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(vk_hex: str) -> str:
    """Empreinte lisible d'une clé publique, à comparer de vive voix."""
    digest = hashlib.sha256(bytes.fromhex(vk_hex)).hexdigest()[:32].upper()
    return " ".join(digest[i:i + 4] for i in range(0, 32, 4))


def is_valid_vk(vk_hex) -> bool:
    if not isinstance(vk_hex, str) or len(vk_hex) != 64:
        return False
    try:
        VerifyKey(bytes.fromhex(vk_hex))
    except (ValueError, CryptoError):
        return False
    return True


def verify(vk_hex: str, data: bytes, sig_hex: str) -> bool:
    try:
        VerifyKey(bytes.fromhex(vk_hex)).verify(data, bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError, TypeError, CryptoError):
        return False


class Identity:
    def __init__(self, signing_key: SigningKey):
        self._sk = signing_key
        self.vk_hex = signing_key.verify_key.encode().hex()

    @classmethod
    def load_or_create(cls, path: str) -> "Identity":
        if os.path.exists(path):
            with open(path, "rb") as f:
                seed = f.read()
            if len(seed) != 32:
                raise ValueError(f"Fichier d'identité corrompu : {path}")
            return cls(SigningKey(seed))
        sk = SigningKey.generate()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(sk.encode())
        return cls(sk)

    def sign(self, data: bytes) -> str:
        return self._sk.sign(data).signature.hex()

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.vk_hex)


def sign_object(identity: Identity, obj: dict, extra: bytes = b"") -> dict:
    return {"body": obj, "sig": identity.sign(canonical(obj) + extra)}


def open_signed(msg, extra: bytes = b"") -> dict:
    """Vérifie un objet signé par la clé ``vk`` qu'il contient et renvoie son corps."""
    if not isinstance(msg, dict):
        raise ProtocolError("message signé invalide")
    body, sig = msg.get("body"), msg.get("sig")
    if not isinstance(body, dict) or not isinstance(sig, str):
        raise ProtocolError("message signé invalide")
    if body.get("proto") != PROTO:
        raise ProtocolError("version de protocole inconnue")
    vk = body.get("vk")
    if not is_valid_vk(vk):
        raise ProtocolError("clé publique invalide")
    if not verify(vk, canonical(body) + extra, sig):
        raise ProtocolError("signature invalide")
    return body


class Channel:
    """Canal chiffré symétrique, un compteur par direction."""

    def __init__(self, send_key: bytes, recv_key: bytes):
        self._send = SecretBox(send_key)
        self._recv = SecretBox(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0

    @staticmethod
    def _nonce(ctr: int) -> bytes:
        return ctr.to_bytes(SecretBox.NONCE_SIZE, "big")

    def encrypt(self, plaintext: bytes) -> bytes:
        ct = self._send.encrypt(plaintext, self._nonce(self._send_ctr)).ciphertext
        self._send_ctr += 1
        return ct

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            pt = self._recv.decrypt(ciphertext, self._nonce(self._recv_ctr))
        except CryptoError as e:
            raise ProtocolError("trame chiffrée invalide") from e
        self._recv_ctr += 1
        return pt


class Handshake:
    """Machine à états de la poignée de main (voir docstring du module)."""

    def __init__(self, identity: Identity, profile: dict, initiator: bool):
        self.identity = identity
        self.initiator = initiator
        self._eph = PrivateKey.generate()
        self._profile = profile
        self._init_msg = None
        self._resp_msg = None
        self.peer = None

    def _hello_body(self, role: str) -> dict:
        body = {"proto": PROTO, "role": role, "vk": self.identity.vk_hex,
                "eph": self._eph.public_key.encode().hex()}
        body.update(self._profile)
        return body

    def _check_peer(self, body: dict, role: str):
        if body.get("role") != role:
            raise ProtocolError("rôle inattendu")
        if body["vk"] == self.identity.vk_hex:
            raise ProtocolError("connexion à soi-même")
        eph = body.get("eph")
        if not isinstance(eph, str) or len(eph) != 64:
            raise ProtocolError("clé éphémère invalide")

    def _transcript(self) -> bytes:
        return hashlib.sha256(canonical(self._init_msg) + canonical(self._resp_msg)).digest()

    # -- initiateur ---------------------------------------------------------
    def initiate(self) -> dict:
        self._init_msg = sign_object(self.identity, self._hello_body("init"))
        return self._init_msg

    def on_response(self, msg) -> dict:
        extra = hashlib.sha256(canonical(self._init_msg)).digest()
        body = open_signed(msg, extra)
        self._check_peer(body, "resp")
        self._resp_msg = msg
        self.peer = body
        return {"confirm": self.identity.sign(b"qchat-confirm" + self._transcript())}

    # -- répondeur ----------------------------------------------------------
    def respond(self, msg) -> dict:
        body = open_signed(msg)
        self._check_peer(body, "init")
        self._init_msg = msg
        self.peer = body
        extra = hashlib.sha256(canonical(msg)).digest()
        self._resp_msg = sign_object(self.identity, self._hello_body("resp"), extra)
        return self._resp_msg

    def on_confirm(self, msg):
        sig = msg.get("confirm") if isinstance(msg, dict) else None
        if not isinstance(sig, str) or not verify(
                self.peer["vk"], b"qchat-confirm" + self._transcript(), sig):
            raise ProtocolError("confirmation invalide")

    # -- dérivation ---------------------------------------------------------
    def channel(self) -> Channel:
        peer_eph = bytes.fromhex(self.peer["eph"])
        shared = nacl.bindings.crypto_scalarmult(self._eph.encode(), peer_eph)
        if shared == bytes(32):
            raise ProtocolError("clé éphémère faible")
        th = self._transcript()

        def kdf(label: bytes) -> bytes:
            return nacl.hash.blake2b(shared, digest_size=32, key=th, person=label,
                                     encoder=RawEncoder)

        i2r, r2i = kdf(b"qchat-i2r"), kdf(b"qchat-r2i")
        return Channel(i2r, r2i) if self.initiator else Channel(r2i, i2r)
