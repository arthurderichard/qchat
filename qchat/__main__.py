"""Point d'entrée : ``python3 -m qchat``."""

import argparse
import logging
import sys

from . import __version__
from .core import DEFAULT_PORT


def _relaunch_args(argv):
    """Options à reprendre lors d'un redémarrage automatique (sans --background/--restore)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a in ("--background", "--autostart") or a.startswith("--restore="):
            continue
        elif a == "--restore":
            skip = True
        else:
            out.append(a)
    return out


def main():
    parser = argparse.ArgumentParser(prog="qchat", description="Messagerie chiffrée pour réseau local")
    parser.add_argument("--background", action="store_true",
                        help="démarrer sans afficher la fenêtre (démarrage de session)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port TCP (défaut {DEFAULT_PORT})")
    parser.add_argument("--home", help="dossier de données (profil séparé, pour tests)")
    parser.add_argument("--no-discovery", action="store_true", help="désactiver la découverte UDP")
    parser.add_argument("--debug", action="store_true", help="journal détaillé")
    parser.add_argument("--restore", metavar="CONV", help=argparse.SUPPRESS)   # après mise à jour
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)  # entrée XDG
    parser.add_argument("--version", action="version", version=f"QChat {__version__}")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from . import session
    if (args.autostart or session.under_service()) and session.system_account():
        return 0        # écran de connexion (gdm) ou autre compte système : pas de QChat
    if args.autostart and (session.autostart_disabled_by_user() or session.service_handles_session()):
        return 0        # désactivé par l'utilisateur, ou déjà lancé par le service systemd
    if session.under_service() and session.autostart_disabled_by_user():
        logging.info("Démarrage automatique désactivé par l'utilisateur")
        return 0
    from .gui import QChatApp
    app = QChatApp(home=args.home, port=args.port, discovery=not args.no_discovery,
                   background=args.background, restore=args.restore,
                   relaunch_args=_relaunch_args(sys.argv[1:]))
    status = app.run([sys.argv[0]])
    return app.exit_code if app.exit_code is not None else status


if __name__ == "__main__":
    sys.exit(main())
