"""Tests d'intégration : plusieurs instances QChat dans le même processus.

Lancement : python3 -m unittest discover -s tests -v
"""

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from qchat import crypto
from qchat import core, desktop
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


class DesktopTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gtk-update-icon-cache"), "gtk-update-icon-cache absent")
    def test_stale_user_icon_cache_repaired(self):
        with tempfile.TemporaryDirectory() as data:
            base = os.path.join(data, "icons", "hicolor")
            apps = os.path.join(base, "scalable", "apps")
            os.makedirs(apps)
            svg = os.path.join(apps, "qchat.svg")
            with open(svg, "w") as f:
                f.write("<svg xmlns='http://www.w3.org/2000/svg'/>")
            subprocess.run(["gtk-update-icon-cache", "-q", "-t", "-f", base], check=True)
            self.assertFalse(desktop.repair_user_icon_cache(data))     # icône présente : rien à faire
            os.unlink(svg)                                             # suppression sans régénérer le cache
            self.assertTrue(desktop.repair_user_icon_cache(data))
            cache = os.path.join(base, "icon-theme.cache")
            self.assertFalse(os.path.exists(cache) and b"qchat" in open(cache, "rb").read())
            self.assertFalse(desktop.repair_user_icon_cache(data))     # déjà réparé

    def test_no_user_icon_cache(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertFalse(desktop.repair_user_icon_cache(data))


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

    def _reactions(self, node, msg_id):
        names = {c.vk: c.name for c in node.core.store.contacts()}
        names[node.vk] = "moi"
        return sorted((e, names.get(r, "?")) for e, r in node.core.store.reactions(msg_id))

    def test_reactions_direct(self):
        cid = self.add(self.a, self.b)
        msg = self.a.core.send_text(f"c:{cid}", "On déjeune ?")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.core.store.get_message(msg.id) is not None))
        self.b.core.react(msg.id, "👍")
        self.b.core.react(msg.id, "🎉")
        self.assertTrue(wait_for(lambda: self._reactions(self.a, msg.id) == [("🎉", "Bob"), ("👍", "Bob")]))
        self.assertTrue(any(n == "reaction" and d.get("incoming") for n, d in self.a.events))
        self.b.core.react(msg.id, "🎉")      # second clic : retire la réaction
        self.assertTrue(wait_for(lambda: self._reactions(self.a, msg.id) == [("👍", "Bob")]))
        self.assertEqual(self._reactions(self.b, msg.id), [("👍", "moi")])

    def test_reactions_group_and_isolation(self):
        b_cid = self.add(self.a, self.b)
        c_cid = self.add(self.a, self.c)
        gid = self.a.core.create_group("Équipe", [b_cid, c_cid])
        self.assertTrue(wait_for(lambda: all(n.core.store.get_group(gid) for n in (self.b, self.c))))
        msg = self.a.core.send_text(f"g:{gid}", "Réunion à 14 h")
        self.assertTrue(wait_for(lambda: self.c.core.store.get_message(msg.id) is not None))
        self.c.core.react(msg.id, "✅")
        # Bob voit la réaction de Chloé, qu'il n'a jamais ajoutée lui-même
        self.assertTrue(wait_for(lambda: self._reactions(self.b, msg.id) == [("✅", "Chloé")]))
        self.assertTrue(wait_for(lambda: self._reactions(self.a, msg.id) == [("✅", "Chloé")]))
        # Une réaction en direct ne peut pas viser un message d'un groupe
        a_cid_on_c = self.c.contact(self.a).id
        self.c.core.store.enqueue(a_cid_on_c, {"t": "react", "id": "r1", "ref": msg.id,
                                               "emoji": "💩", "on": True})
        self.c.core.request_flush([a_cid_on_c])
        time.sleep(1)
        self.assertEqual(self._reactions(self.a, msg.id), [("✅", "Chloé")])

    def test_reply_to_message(self):
        cid = self.add(self.a, self.b)
        q = self.a.core.send_text(f"c:{cid}", "On mange où ?")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.core.store.get_message(q.id) is not None))
        conv_b = self.b.contact(self.a).conv
        r = self.b.core.send_text(conv_b, "Au japonais", reply_to=q.id)
        self.assertEqual(r.reply_to, q.id)
        self.assertTrue(wait_for(lambda: self.a.core.store.get_message(r.id) is not None))
        self.assertEqual(self.a.core.store.get_message(r.id).reply_to, q.id)
        # une réponse ne peut viser qu'un message de la même conversation
        bad = self.b.core.send_text(conv_b, "Hors sujet", reply_to="inconnu")
        self.assertIsNone(bad.reply_to)

    def test_delete_message_everywhere(self):
        cid = self.add(self.a, self.b)
        msg = self.a.core.send_text(f"c:{cid}", "Oups, mauvais destinataire")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.core.store.get_message(msg.id) is not None))
        conv_b = self.b.contact(self.a).conv
        self.b.core.react(msg.id, "👍")
        self.assertTrue(wait_for(lambda: self.a.core.store.reactions(msg.id)))
        # Bob ne peut pas supprimer le message d'Alice, ni chez lui ni chez elle
        self.assertFalse(self.b.core.delete_message(msg.id))
        b_on_a = self.a.contact(self.b)
        self.b.core.store.enqueue(self.b.contact(self.a).id, {"t": "delete", "id": "d1", "ref": msg.id,
                                                              "ts": time.time()})
        self.b.core.request_flush([self.b.contact(self.a).id])
        time.sleep(1)
        self.assertIsNone(self.a.core.store.get_message(msg.id).deleted)
        self.assertTrue(self.a.core.delete_message(msg.id))
        self.assertFalse(self.a.core.delete_message(msg.id))       # déjà supprimé
        for node, conv in ((self.a, f"c:{cid}"), (self.b, conv_b)):
            self.assertTrue(wait_for(lambda n=node: n.core.store.get_message(msg.id).deleted is not None))
            m = node.core.store.get_message(msg.id)
            self.assertEqual(m.text, "")
            self.assertEqual(node.core.store.reactions(msg.id), [])
        self.assertTrue(any(n == "deleted" and d.get("incoming") for n, d in self.b.events))
        # plus de modification ni de réaction sur un message supprimé
        self.assertFalse(self.a.core.edit_text(msg.id, "Nouveau texte"))
        self.b.core.react(msg.id, "🎉")
        self.assertEqual(self.b.core.store.reactions(msg.id), [])
        self.assertIsNotNone(b_on_a)

    def test_delete_file_removes_local_copy(self):
        cid = self.add(self.a, self.b)
        src = os.path.join(self.a.tmp.name, "photo.txt")
        with open(src, "w") as f:
            f.write("contenu")
        msg = self.a.core.send_file(f"c:{cid}", src)
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.core.store.get_attachment(msg.id) is not None))
        paths = [n.core.store.get_attachment(msg.id).path for n in (self.a, self.b)]
        self.assertTrue(all(os.path.isfile(p) for p in paths))
        self.a.core.delete_message(msg.id)
        self.assertTrue(wait_for(lambda: not any(os.path.exists(p) for p in paths)))
        self.assertIsNone(self.b.core.store.get_attachment(msg.id))

    def test_wizz_direct_cooldown_and_stale(self):
        cid = self.add(self.a, self.b)
        self.assertEqual(self.a.core.send_wizz(f"c:{cid}", "poke"), 0)
        self.assertGreater(self.a.core.send_wizz(f"c:{cid}"), 0)      # anti-spam
        wizz = lambda: [d for n, d in self.b.events if n == "wizz"]   # noqa: E731
        self.assertTrue(wait_for(lambda: len(wizz()) == 1))
        a_on_b = self.b.contact(self.a)
        self.assertEqual(wizz()[0], {"contact_id": a_on_b.id, "conv": a_on_b.conv, "kind": "poke"})
        system = lambda n, conv: [m.text for m in n.core.store.messages(conv) if m.kind == "system"]  # noqa: E731
        self.assertIn("Vous avez envoyé un poke.", system(self.a, f"c:{cid}"))
        self.assertTrue(wait_for(lambda: "Alice vous a envoyé un poke." in system(self.b, a_on_b.conv)))
        # Un wizz arrivé longtemps après (destinataire hors ligne) est noté mais ne surgit pas
        self.b.core._wizz_seen.clear()
        self.a.core.store.enqueue(cid, {"t": "wizz", "id": "w-old", "ts": time.time() - 3600,
                                        "kind": "wizz"})
        self.a.core.request_flush([cid])
        self.assertTrue(wait_for(lambda: "Alice vous a envoyé un wizz." in system(self.b, a_on_b.conv)))
        self.assertEqual(len(wizz()), 1)

    def test_wizz_group(self):
        b_cid = self.add(self.a, self.b)
        c_cid = self.add(self.a, self.c)
        gid = self.a.core.create_group("Équipe", [b_cid, c_cid])
        self.assertTrue(wait_for(lambda: all(n.core.store.get_group(gid) for n in (self.b, self.c))))
        self.assertEqual(self.a.core.send_wizz(f"g:{gid}"), 0)
        # anti-spam global : 30 s avant un nouveau wizz, même vers une autre conversation
        wait = self.a.core.send_wizz(f"c:{b_cid}")
        self.assertTrue(25 <= wait <= 30, wait)
        for node in (self.b, self.c):
            self.assertTrue(wait_for(lambda: any(n == "wizz" and d["conv"] == f"g:{gid}"
                                                 for n, d in node.events)))

    def test_edit_direct_keeps_history(self):
        cid = self.add(self.a, self.b)
        msg = self.a.core.send_text(f"c:{cid}", "Réunion à 14 h")
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None
                                 and self.b.core.store.get_message(msg.id) is not None))
        self.assertTrue(self.a.core.edit_text(msg.id, "Réunion à 15 h"))
        self.assertTrue(self.a.core.edit_text(msg.id, "Réunion à 15 h 30"))
        self.assertFalse(self.a.core.edit_text(msg.id, "Réunion à 15 h 30"))     # inchangé
        self.assertTrue(wait_for(lambda: self.b.core.store.get_message(msg.id).text == "Réunion à 15 h 30"))
        for node in (self.a, self.b):
            m = node.core.store.get_message(msg.id)
            self.assertIsNotNone(m.edited)
            self.assertEqual([t for t, _ in node.core.store.message_history(msg.id)],
                             ["Réunion à 14 h", "Réunion à 15 h", "Réunion à 15 h 30"])
        self.assertTrue(any(n == "edited" and d.get("incoming") for n, d in self.b.events))
        # Bob ne peut pas modifier le message d'Alice, ni via l'API ni en forgeant un paquet
        self.assertFalse(self.b.core.edit_text(msg.id, "piraté"))
        a_on_b = self.b.contact(self.a).id
        self.b.core.store.enqueue(a_on_b, {"t": "edit", "id": "e1", "ref": msg.id,
                                           "text": "piraté", "ts": time.time() + 10})
        self.b.core.request_flush([a_on_b])
        time.sleep(1)
        self.assertEqual(self.a.core.store.get_message(msg.id).text, "Réunion à 15 h 30")

    def test_edit_group_and_stale_edit_ignored(self):
        b_cid = self.add(self.a, self.b)
        c_cid = self.add(self.a, self.c)
        gid = self.a.core.create_group("Équipe", [b_cid, c_cid])
        self.assertTrue(wait_for(lambda: all(n.core.store.get_group(gid) for n in (self.b, self.c))))
        msg = self.b.core.send_text(f"g:{gid}", "Je prends les croissants")
        self.assertTrue(wait_for(lambda: self.c.core.store.get_message(msg.id) is not None))
        self.b.core.edit_text(msg.id, "Je prends les pains au chocolat")
        for node in (self.a, self.c):
            self.assertTrue(wait_for(lambda n=node: n.core.store.get_message(msg.id).text
                                     == "Je prends les pains au chocolat"))
        # Une modification plus ancienne que la dernière connue ne remplace rien
        store = self.c.core.store
        self.assertFalse(store.edit_message(msg.id, "vieux", store.get_message(msg.id).edited - 5))
        self.assertEqual(len(store.message_history(msg.id)), 2)

    def test_viewable_kind(self):
        try:
            from qchat import viewer
        except (ImportError, ValueError) as e:
            self.skipTest(f"GTK indisponible : {e}")
        d = self.a.tmp.name
        gif = os.path.join(d, "a.gif")
        with open(gif, "wb") as f:      # GIF 1×1 minimal
            f.write(bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c000000000100"
                                  "0100000202440100") + b";")
        cases = {"a.gif": ("image/gif", None), "doc.pdf": ("application/pdf", b"%PDF-1.4\n%%EOF"),
                 "notes.txt": ("text/plain", "Réunion à 14 h\n".encode()),
                 "conf.json": ("application/json", b'{"a": 1}'),
                 "sans-ext": ("application/octet-stream", b"juste du texte"),
                 "bin.dat": ("application/octet-stream", b"\x00\x01\x02binaire"),
                 "faux.png": ("image/png", b"pas une image")}
        expected = {"a.gif": "image", "doc.pdf": "pdf", "notes.txt": "text", "conf.json": "text",
                    "sans-ext": "text", "bin.dat": None, "faux.png": None}
        for name, (mime, data) in cases.items():
            path = os.path.join(d, name)
            if data is not None:
                with open(path, "wb") as f:
                    f.write(data)
            self.assertEqual(viewer.viewable_kind(path, mime), expected[name], name)
        self.assertEqual(viewer.fit(1600, 1000, 320, 240), (320, 200))
        self.assertEqual(viewer.fit(100, 50, 320, 240), (100, 50))      # jamais agrandi

    PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e5274fb10000000049454e44ae426082")

    def _avatar_sha(self, node, conv):
        path = node.core.avatar_path(conv)
        if not path:
            return None
        import hashlib
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_profile_avatar(self):
        import hashlib
        sha = hashlib.sha256(self.PNG).hexdigest()
        cid = self.add(self.a, self.b)
        self.assertTrue(wait_for(lambda: self.b.contact(self.a) is not None))
        self.a.core.set_avatar(self.PNG)
        conv_on_b = self.b.contact(self.a).conv
        self.assertTrue(wait_for(lambda: self._avatar_sha(self.b, conv_on_b) == sha))
        self.assertTrue(any(n == "avatars_changed" for n, _ in self.b.events))
        # Chloé ajoute Alice après coup : elle obtient la photo grâce à l'empreinte du hello
        self.add(self.c, self.a)
        self.assertTrue(wait_for(lambda: self._avatar_sha(self.c, self.c.contact(self.a).conv) == sha))
        # Retrait : propagé aussi
        self.a.core.set_avatar(None)
        self.assertTrue(wait_for(lambda: self.b.core.avatar_path(conv_on_b) is None))
        self.assertIsNone(self.a.core.avatar_path("me"))
        # Image invalide refusée
        with self.assertRaises(ValueError):
            self.a.core.set_avatar(b"pas une image")
        with self.assertRaises(ValueError):
            self.a.core.set_avatar(b"\x89PNG\r\n\x1a\n" + bytes(core.MAX_AVATAR))
        # Une photo falsifiée (empreinte incorrecte) est ignorée
        b_on_a = self.a.contact(self.b).id
        self.a.core.store.enqueue(b_on_a, {"t": "avatar", "id": "x", "sha": "0" * 64,
                                           "data": __import__("base64").b64encode(self.PNG).decode()})
        self.a.core.request_flush([b_on_a])
        time.sleep(1)
        self.assertIsNone(self.b.core.avatar_path(conv_on_b))

    def test_group_avatar(self):
        import hashlib
        sha = hashlib.sha256(self.PNG).hexdigest()
        b_cid = self.add(self.a, self.b)
        c_cid = self.add(self.a, self.c)
        gid = self.a.core.create_group("Équipe", [b_cid])
        self.assertTrue(wait_for(lambda: self.b.core.store.get_group(gid) is not None))
        self.b.core.set_group_avatar(gid, self.PNG)          # n'importe quel membre
        self.assertTrue(wait_for(lambda: self._avatar_sha(self.a, f"g:{gid}") == sha))
        self.assertIn("Bob a changé la photo du groupe.", self.a.texts(f"g:{gid}") +
                      [m.text for m in self.a.core.store.messages(f"g:{gid}")])
        # Un nouveau membre reçoit la photo avec l'invitation
        self.a.core.add_group_members(gid, [c_cid])
        self.assertTrue(wait_for(lambda: self._avatar_sha(self.c, f"g:{gid}") == sha))
        # Une photo plus ancienne que la dernière connue ne la remplace pas
        old_png = self.PNG[:-12] + self.PNG[-12:]
        self.assertFalse(self.c.core._apply_group_avatar(gid, sha, "", 1.0))
        self.a.core.set_group_avatar(gid, None)
        for node in (self.b, self.c):
            self.assertTrue(wait_for(lambda n=node: n.core.avatar_path(f"g:{gid}") is None))
        self.assertFalse(self.c.core._apply_group_avatar(
            gid, sha, __import__("base64").b64encode(old_png).decode(), 2.0))

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

class FakeKlipy:
    """Serveur HTTP local imitant l'API KLIPY (aucun accès Internet dans les tests)."""

    def __init__(self, gif):
        import http.server
        import json as _json
        outer = self
        self.gif, self.requests = gif, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                base = f"http://127.0.0.1:{outer.port}"
                if self.path.startswith("/v1/"):
                    if "/refusee/" in self.path:
                        self.send_response(403); self.end_headers(); return

                    def media(name, w=100, h=80, size=1000):
                        return {"url": f"{base}/m/{name}", "width": w, "height": h, "size": size}

                    item = {"id": 1, "type": "gif", "title": "Happy Dance GIF by Studio", "file": {
                        "md": {"gif": media("md.gif", size=50_000_000)},     # trop gros : écarté
                        "sm": {"gif": media("sm.gif"), "jpg": media("sm.jpg")},
                        "xs": {"gif": media("xs.gif", 50, 40)}}}
                    ad = {"type": "ad", "content": "<pub>"}
                    evil = {"id": 2, "type": "gif", "title": "x", "file": {
                        "sm": {"gif": {"url": "https://evil.example/a.gif"},
                               "jpg": {"url": "https://evil.example/a.jpg"}}}}
                    body = _json.dumps({"result": True, "data": {"data": [item, ad, evil], "current_page": 1,
                                                                 "per_page": 24, "has_next": True}}).encode()
                    ctype = "application/json"
                elif self.path.startswith("/m/"):
                    body = outer.gif if self.path.endswith(".gif") else outer.jpg
                    ctype = "image/gif"
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.jpg = b"\xff\xd8\xff\xe0fake-jpeg"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class UpdaterTest(unittest.TestCase):
    def test_versions(self):
        from qchat import updater
        self.assertTrue(updater.is_newer("1.10.0", "1.9.3"))
        self.assertFalse(updater.is_newer("1.4.0", "1.4.0"))
        self.assertFalse(updater.is_newer("1.3.9", "1.4.0"))
        self.assertTrue(updater.is_newer("2.0", "1.99.99"))

    def test_pending_update_reads_disk(self):
        from qchat import __version__, updater
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "__init__.py")
            self.assertIsNone(updater.pending_update(d))           # fichier absent
            with open(path, "w") as f:
                f.write(f'__version__ = "{__version__}"\n')
            self.assertIsNone(updater.pending_update(d))
            with open(path, "w") as f:
                f.write('# QChat\n\n__version__ = "99.0.0"\n')
            self.assertEqual(updater.pending_update(d), "99.0.0")
        old = os.environ.pop("QCHAT_AUTO_RESTART", None)
        try:
            self.assertTrue(updater.auto_restart_enabled("/usr/share/qchat/qchat"))
            self.assertFalse(updater.auto_restart_enabled("/home/moi/src/qchat"))
        finally:
            if old is not None:
                os.environ["QCHAT_AUTO_RESTART"] = old


class GifsTest(unittest.TestCase):
    GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002"
                        "02440100") + b";"

    def setUp(self):
        from qchat import gifs
        self.gifs = gifs
        self.fake = FakeKlipy(self.GIF)
        self.old_api = gifs.API
        gifs.API = f"http://127.0.0.1:{self.fake.port}/v1"

    def tearDown(self):
        self.gifs.API = self.old_api
        self.fake.close()

    def test_search_and_download(self):
        g = self.gifs
        self.assertTrue(g.api_key())                        # clé intégrée : rien à configurer
        res, has_next = g.search("danse", 2, "poste1")
        self.assertTrue(has_next)
        self.assertEqual(len(res), 1)               # publicité et média hors KLIPY écartés
        req = self.fake.requests[-1]
        self.assertTrue(req.startswith(f"/v1/{g.api_key()}/gifs/search?"), req)
        for part in ("q=danse", "page=2", "customer_id=poste1", "content_filter=high"):
            self.assertIn(part, req)
        self.assertTrue(res[0]["url"].endswith("/m/sm.gif"))   # md trop lourd → taille inférieure
        self.assertTrue(res[0]["still"].endswith("/m/sm.jpg"))
        g.search("")
        self.assertIn("/gifs/trending?", self.fake.requests[-1])
        self.assertEqual(g.download(res[0]["url"]), self.GIF)
        self.assertEqual(g.filename(res[0]["title"]), "happy-dance.gif")
        self.assertEqual(g.filename(""), "gif.gif")
        with self.assertRaises(g.GifError):
            g.download("https://evil.example/f.gif")
        with self.assertRaises(g.GifError):
            g.download(res[0]["still"])                 # pas un GIF
        self.assertTrue(g.download(res[0]["still"], kind="image").startswith(b"\xff\xd8"))
        old = g.KLIPY_KEY
        g.KLIPY_KEY = "refusee"
        try:
            with self.assertRaises(g.GifError) as cm:
                g.search("chat")
            self.assertIn("refusé la clé", str(cm.exception))
        finally:
            g.KLIPY_KEY = old

    def test_unreachable(self):
        self.gifs.API = "http://127.0.0.1:9/v1"
        with self.assertRaises(self.gifs.GifError) as cm:
            self.gifs.search("chat")
        self.assertIn("injoignable", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
