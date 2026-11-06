"""Tests d'intégration : plusieurs instances QChat dans le même processus.

Lancement : python3 -m unittest discover -s tests -v
"""

import os
import socket
import tempfile
import threading
import time
import unittest

from qchat import crypto
from qchat import core
from qchat.core import Connection, Core


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Node:
    def __init__(self, name):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.core = Core(home=self.tmp.name, port=free_port(), discovery=False,
                         on_event=lambda n, d: self.events.append((n, d)))
        self.core.set_name(name)
        self.core.start()

    @property
    def vk(self):
        return self.core.identity.vk_hex

    def contact(self, other):
        return self.core.store.contact_by_vk(other.vk)

    def texts(self, conv):
        return [m.text for m in self.core.store.messages(conv) if m.kind == "text"]

    def close(self):
        self.core.stop()
        self.tmp.cleanup()


class CryptoTest(unittest.TestCase):
    def test_handshake_and_channel(self):
        a = crypto.Identity(crypto.SigningKey.generate())
        b = crypto.Identity(crypto.SigningKey.generate())
        ha = crypto.Handshake(a, {"name": "A"}, initiator=True)
        hb = crypto.Handshake(b, {"name": "B"}, initiator=False)
        resp = hb.respond(ha.initiate())
        hb.on_confirm(ha.on_response(resp))
        ca, cb = ha.channel(), hb.channel()
        self.assertEqual(ha.peer["vk"], b.vk_hex)
        self.assertEqual(hb.peer["vk"], a.vk_hex)
        ct = ca.encrypt(b"bonjour")
        self.assertNotIn(b"bonjour", ct)
        self.assertEqual(cb.decrypt(ct), b"bonjour")
        self.assertEqual(ca.decrypt(cb.encrypt(b"salut")), b"salut")
        # rejeu d'une trame : refusé (compteur de nonce)
        with self.assertRaises(crypto.ProtocolError):
            cb.decrypt(ct)

    def test_tampered_hello_rejected(self):
        a = crypto.Identity(crypto.SigningKey.generate())
        b = crypto.Identity(crypto.SigningKey.generate())
        hello = crypto.Handshake(a, {"name": "A"}, initiator=True).initiate()
        hello["body"]["name"] = "Usurpateur"
        with self.assertRaises(crypto.ProtocolError):
            crypto.Handshake(b, {}, initiator=False).respond(hello)

    def test_mitm_key_substitution_rejected(self):
        """Un intermédiaire qui remplace la clé éphémère casse la signature."""
        a = crypto.Identity(crypto.SigningKey.generate())
        b = crypto.Identity(crypto.SigningKey.generate())
        ha = crypto.Handshake(a, {}, initiator=True)
        hb = crypto.Handshake(b, {}, initiator=False)
        init = ha.initiate()
        init["body"]["eph"] = crypto.PrivateKey.generate().public_key.encode().hex()
        with self.assertRaises(crypto.ProtocolError):
            hb.respond(init)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.c = Node("Alice"), Node("Bob"), Node("Chloé")

    def tearDown(self):
        for n in (self.a, self.b, self.c):
            n.close()

    def add(self, src, dst):
        cid = src.core.add_contact(dst.core.name, [("127.0.0.1", dst.core.port)])
        self.assertTrue(wait_for(lambda: src.contact(dst) is not None), "clé non récupérée")
        return cid

    def test_add_by_ip_and_direct_message(self):
        cid = self.add(self.a, self.b)
        self.assertEqual(self.a.contact(self.b).id, cid)
        msg = self.a.core.send_text(f"c:{cid}", "Bonjour Bob")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None))
        conv_b = self.b.contact(self.a).conv
        self.assertTrue(wait_for(lambda: self.b.texts(conv_b) == ["Bonjour Bob"]))
        self.assertTrue(wait_for(lambda: self.a.core.store.get_message(msg.id).status == "delivered"))
        # réponse dans l'autre sens, sans que Bob ait rien configuré
        self.b.core.send_text(conv_b, "Salut Alice")
        self.assertTrue(wait_for(lambda: self.a.texts(f"c:{cid}") == ["Bonjour Bob", "Salut Alice"]))
        self.assertTrue(any(n == "message" and d.get("incoming") for n, d in self.b.events))

    def test_contact_has_multiple_ips(self):
        cid = self.a.core.add_contact("Bob", [("127.0.0.1", self.b.core.port),
                                              ("127.0.0.2", self.b.core.port)])
        self.assertTrue(wait_for(lambda: self.a.contact(self.b) is not None))
        self.assertIn("127.0.0.2", self.a.core.store.ips(cid))
        self.assertIn("127.0.0.1", self.a.core.store.ips(cid))

    def test_offline_queue(self):
        cid = self.add(self.a, self.b)
        port = self.b.core.port
        home = self.b.core.home
        self.b.core.stop()
        self.a.core.send_text(f"c:{cid}", "Message différé")
        time.sleep(0.5)
        self.assertEqual(self.a.core.store.messages(f"c:{cid}")[-1].status, "pending")
        # Bob redémarre (même identité, même port)
        self.b.core = Core(home=home, port=port, discovery=False)
        self.b.core.start()
        self.a.core.request_flush([cid])
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.texts(self.b.contact(self.a).conv) == ["Message différé"]))

    def test_key_change_detected(self):
        cid = self.add(self.a, self.b)
        port = self.b.core.port
        self.b.close()
        self.b = Node("Imposteur")   # nouvelle clé...
        self.b.core.stop()
        self.b.core = Core(home=self.b.tmp.name, port=port, discovery=False)  # ...sur la même IP:port
        self.b.core.start()
        self.a.core.send_text(f"c:{cid}", "secret")
        self.assertTrue(wait_for(lambda: self.a.core.store.get_contact(cid).pending_vk == self.b.vk))
        time.sleep(0.5)
        received = [m for c in self.b.core.store.contacts() for m in self.b.core.store.messages(c.conv)
                    if m.kind == "text"]
        self.assertEqual(received, [], "aucun message ne doit atteindre l'imposteur")
        # Alice accepte la nouvelle clé : le message part
        self.a.core.accept_pending_key(cid)
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.texts(self.b.contact(self.a).conv) == ["secret"]))

    def test_group_create_add_member_and_chat(self):
        b_cid = self.add(self.a, self.b)
        gid = self.a.core.create_group("Projet", [b_cid])
        self.assertTrue(wait_for(lambda: self.b.core.store.get_group(gid) is not None))
        self.a.core.send_text(f"g:{gid}", "Premier message")
        self.assertTrue(wait_for(lambda: self.b.texts(f"g:{gid}") == ["Premier message"]))

        # Bob ajoute Chloé, qu'Alice ne connaît pas encore
        c_cid = self.add(self.b, self.c)
        self.b.core.add_group_members(gid, [c_cid])
        self.assertTrue(wait_for(lambda: self.c.core.store.get_group(gid) is not None))
        self.assertTrue(wait_for(lambda: self.a.contact(self.c) is not None
                                 and self.a.contact(self.c).id in self.a.core.store.group_member_ids(gid)))
        members_c = {self.c.core.store.get_contact(i).vk for i in self.c.core.store.group_member_ids(gid)}
        self.assertEqual(members_c, {self.a.vk, self.b.vk})

        # Chloé écrit : Alice reçoit sans l'avoir jamais ajoutée elle-même
        self.c.core.send_text(f"g:{gid}", "Coucou le groupe")
        self.assertTrue(wait_for(lambda: "Coucou le groupe" in self.a.texts(f"g:{gid}")))
        self.assertTrue(wait_for(lambda: "Coucou le groupe" in self.b.texts(f"g:{gid}")))

        # Bob quitte le groupe
        self.b.core.leave_group(gid)
        self.assertTrue(wait_for(
            lambda: self.a.contact(self.b).id not in self.a.core.store.group_member_ids(gid)))
        self.assertIsNone(self.b.core.store.get_group(gid))

    def test_non_member_group_message_ignored(self):
        b_cid = self.add(self.a, self.b)
        gid = self.a.core.create_group("Privé", [b_cid])
        self.assertTrue(wait_for(lambda: self.b.core.store.get_group(gid) is not None))
        a_cid = self.add(self.c, self.a)
        self.c.core.store.enqueue(a_cid, {"t": "msg", "id": "x1", "ts": time.time(),
                                          "gid": gid, "text": "intrus"}, "x1")
        self.c.core.request_flush([a_cid])
        time.sleep(1)
        self.assertNotIn("intrus", self.a.texts(f"g:{gid}"))

    def _file(self, name, data):
        path = os.path.join(self.a.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _received_files(self, node, conv):
        out = []
        for m in node.core.store.messages(conv):
            if m.kind == "file":
                att = node.core.store.get_attachment(m.id)
                with open(att.path, "rb") as f:
                    out.append((att.name, att.mime, f.read()))
        return out

    def test_send_large_file(self):
        cid = self.add(self.a, self.b)
        data = os.urandom(3 * 1024 * 1024 + 123)     # plusieurs blocs
        msg = self.a.core.send_file(f"c:{cid}", self._file("rapport.pdf", data))
        self.assertTrue(wait_for(lambda: self.a.core.store.get_message(msg.id).status == "delivered"))
        conv_b = self.b.contact(self.a).conv
        self.assertEqual(self._received_files(self.b, conv_b), [("rapport.pdf", "application/pdf", data)])
        self.assertTrue(any(n == "message" and d.get("incoming") and d["message"].kind == "file"
                            for n, d in self.b.events))

    def test_file_in_group_and_name_sanitized(self):
        b_cid = self.add(self.a, self.b)
        c_cid = self.add(self.a, self.c)
        gid = self.a.core.create_group("Photos", [b_cid, c_cid])
        self.assertTrue(wait_for(lambda: self.c.core.store.get_group(gid) is not None
                                 and self.b.core.store.get_group(gid) is not None))
        png = b"\x89PNG\r\n\x1a\n" + os.urandom(1000)
        self.a.core.send_file(f"g:{gid}", self._file("photo.png", png), name="../../.bashrc.png")
        for node in (self.b, self.c):
            self.assertTrue(wait_for(lambda n=node: self._received_files(n, f"g:{gid}")))
            name, mime, data = self._received_files(node, f"g:{gid}")[0]
            self.assertEqual((name, mime, data), ("bashrc.png", "image/png", png))
            att = node.core.store.get_attachment(node.core.store.messages(f"g:{gid}")[-1].id)
            self.assertTrue(att.path.startswith(node.core.files_dir + os.sep))

    def test_image_without_extension_detected(self):
        cid = self.add(self.a, self.b)
        png = b"\x89PNG\r\n\x1a\n" + os.urandom(500)
        name = "Image générée 1_ class=_ImageTransparencyBackdrop_"
        msg = self.a.core.send_file(f"c:{cid}", self._file(name, png))
        self.assertEqual(msg.text, name + ".png")
        self.assertEqual(self.a.core.store.get_attachment(msg.id).mime, "image/png")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self._received_files(self.b, self.b.contact(self.a).conv)))
        self.assertEqual(self._received_files(self.b, self.b.contact(self.a).conv),
                         [(name + ".png", "image/png", png)])
        # contenu inconnu : reste un fichier générique, nom inchangé
        self.assertEqual(core.detect_file(self._file("notes", b"bonjour"), "notes"),
                         ("notes", "application/octet-stream"))

    def test_long_filename_keeps_extension(self):
        cid = self.add(self.a, self.b)
        long_name = ("Image générée 1_ class=_ImageTransparencyBackdrop-jTfEPD block h-full "
                     "image-transparency-backdrop w-auto object-contain_.png")
        emoji_name = "😀" * 100 + ".tar.gz"
        png = b"\x89PNG\r\n\x1a\n" + os.urandom(300)
        self.a.core.send_file(f"c:{cid}", self._file("a.png", png), name=long_name)
        self.a.core.send_file(f"c:{cid}", self._file("b.tgz", b"archive"), name=emoji_name)
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and len(self._received_files(self.b, self.b.contact(self.a).conv)) == 2))
        (n1, m1, d1), (n2, m2, _) = self._received_files(self.b, self.b.contact(self.a).conv)
        self.assertEqual((n1, m1, d1), (long_name, "image/png", png))   # nom intact
        self.assertTrue(n2.endswith(".tar.gz") and n2.startswith("😀"))
        self.assertLessEqual(len(n2.encode()), core.MAX_FILENAME_BYTES)

    def test_corrupted_file_rejected(self):
        cid = self.add(self.a, self.b)
        msg = self.a.core.send_file(f"c:{cid}", self._file("a.txt", b"contenu original"))
        # altère la copie locale après calcul de l'empreinte (simulation de corruption)
        with open(self.a.core.store.get_attachment(msg.id).path, "wb") as f:
            f.write(b"contenu modifie!")
        time.sleep(1)
        self.assertEqual(self.a.core.store.get_message(msg.id).status, "pending")
        self.assertTrue(self.b.contact(self.a) is None or
                        not self._received_files(self.b, self.b.contact(self.a).conv))
        self.assertEqual([f for f in os.listdir(self.b.core.files_dir) if f.startswith(".part")], [])

    def test_file_too_large_refused(self):
        cid = self.add(self.a, self.b)
        path = self._file("big.bin", b"")
        with open(path, "wb") as f:
            f.truncate(core.MAX_FILE + 1)
        with self.assertRaises(ValueError):
            self.a.core.send_file(f"c:{cid}", path)

    def test_network_scan(self):
        found, progress = [], []
        self.a.core.scan(["127.0.0.1", "127.0.0.2"], port=self.b.core.port,
                         on_progress=lambda d, t: progress.append((d, t)), on_found=found.append)
        self.assertEqual(progress[-1], (2, 2))
        # 127.0.0.2 mène aussi à la boucle locale : Bob répond sur ses deux IP
        self.assertEqual({f["vk"] for f in found}, {self.b.vk})
        self.assertLessEqual({"127.0.0.1", "127.0.0.2"}, set(self.a.core.discovered[self.b.vk]["ips"]))
        self.assertEqual(found[0]["name"], "Bob")
        self.assertIsNone(found[0]["contact_id"])
        self.assertIn(self.b.vk, self.a.core.discovered)
        # la sonde est silencieuse : rien n'est créé chez Bob
        time.sleep(0.3)
        self.assertEqual(self.b.core.store.contacts(), [])
        # ajout depuis les résultats puis échange
        cid = self.a.core.add_discovered(self.b.vk)
        self.a.core.store.set_port(cid, self.b.core.port)
        self.a.core.send_text(f"c:{cid}", "Trouvé par la découverte")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None and
                                 self.b.texts(self.b.contact(self.a).conv) == ["Trouvé par la découverte"]))
        # un second scan reconnaît le contact existant
        found.clear()
        self.a.core.scan(["127.0.0.1"], port=self.b.core.port, on_found=found.append)
        self.assertEqual(found[0]["contact_id"], cid)

    def test_wire_traffic_is_encrypted(self):
        """Le texte ne doit jamais apparaître en clair sur le réseau."""
        captured = bytearray()
        target = ("127.0.0.1", self.b.core.port)
        proxy = socket.socket()
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(4)
        proxy_port = proxy.getsockname()[1]

        def pipe(src, dst):
            try:
                while data := src.recv(65536):
                    captured.extend(data)
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                dst.close()

        def serve():
            while True:
                try:
                    client, _ = proxy.accept()
                except OSError:
                    return
                upstream = socket.create_connection(target)
                threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
                threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        cid = self.a.core.add_contact("Bob", [("127.0.0.1", proxy_port)])
        self.assertTrue(wait_for(lambda: self.a.contact(self.b) is not None))
        self.a.core.store.set_port(cid, proxy_port)   # forcer le passage par le proxy
        self.a.core.send_text(f"c:{cid}", "MOT-DE-PASSE-ULTRA-SECRET")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None and
                                 self.b.texts(self.b.contact(self.a).conv)))
        proxy.close()
        self.assertNotIn(b"MOT-DE-PASSE-ULTRA-SECRET", bytes(captured))
        self.assertGreater(len(captured), 0)


if __name__ == "__main__":
    unittest.main()
