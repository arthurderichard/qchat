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
        elif a == "--background" or a.startswith("--restore="):
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
    parser.add_argument("--version", action="version", version=f"QChat {__version__}")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .gui import QChatApp
    app = QChatApp(home=args.home, port=args.port, discovery=not args.no_discovery,
                   background=args.background, restore=args.restore,
                   relaunch_args=_relaunch_args(sys.argv[1:]))
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
