#!/usr/bin/env bash
# Construit le paquet Debian de QChat : dist/qchat_<version>_all.deb
# Usage : packaging/build-deb.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="fr.qchat.QChat"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/qchat/__init__.py")"
MAINTAINER="${DEB_MAINTAINER:-Arthur Derichard <aderichard@qerys.com>}"
[[ -n $VERSION ]] || { echo "Version introuvable dans qchat/__init__.py" >&2; exit 1; }

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
ROOT="$BUILD/qchat"
DOC="$ROOT/usr/share/doc/qchat"

install -d "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/share/qchat" "$ROOT/usr/share/applications" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps" "$ROOT/etc/xdg/autostart" "$DOC"

# Code (sans caches Python)
cp -r "$SRC/qchat" "$ROOT/usr/share/qchat/"
find "$ROOT/usr/share/qchat" -name __pycache__ -prune -exec rm -rf {} +
find "$ROOT/usr/share/qchat" -type f -exec chmod 644 {} +

cat > "$ROOT/usr/bin/qchat" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/share/qchat${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m qchat "$@"
EOF
chmod 755 "$ROOT/usr/bin/qchat"

install -m 644 "$SRC/data/qchat.svg" "$ROOT/usr/share/icons/hicolor/scalable/apps/qchat.svg"

desktop() {   # $1 = arguments supplémentaires de la commande
    cat <<EOF
[Desktop Entry]
Type=Application
Name=QChat
GenericName=Messagerie interne
Comment=Discussion chiffrée de bout en bout sur le réseau local
Exec=qchat$1
Icon=qchat
Terminal=false
Categories=Network;InstantMessaging;Chat;
Keywords=chat;messagerie;réseau;lan;
StartupNotify=true
StartupWMClass=$APP_ID
X-GNOME-UsesNotifications=true
EOF
}
desktop "" > "$ROOT/usr/share/applications/$APP_ID.desktop"
# Démarrage en arrière-plan à l'ouverture de session, pour recevoir les notifications.
{ desktop " --background"; echo "X-GNOME-Autostart-enabled=true"; } > "$ROOT/etc/xdg/autostart/$APP_ID.desktop"
chmod 644 "$ROOT"/usr/share/applications/* "$ROOT"/etc/xdg/autostart/*

# Documentation Debian
install -m 644 "$SRC/README.md" "$DOC/README.md"
cat > "$DOC/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: qchat
Source: https://github.com/arthurderichard/qchat

Files: *
Copyright: 2026 Arthur Derichard
License: MIT
EOF
sed -e '1,2d' -e 's/^$/ ./' -e 's/^/ /' -e 's/^  \./ ./' "$SRC/LICENSE" >> "$DOC/copyright"
cat > "$BUILD/changelog" <<EOF
qchat ($VERSION) stable; urgency=medium

  * Version $VERSION, voir https://github.com/arthurderichard/qchat/releases

 -- $MAINTAINER  $(date -R -d "@${SOURCE_DATE_EPOCH:-$(date +%s)}")
EOF
gzip -9n < "$BUILD/changelog" > "$DOC/changelog.gz"
chmod 644 "$DOC"/*

cat > "$ROOT/DEBIAN/control" <<EOF
Package: qchat
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, python3-nacl
Recommends: librsvg2-common
Suggests: libnotify-bin
Section: net
Priority: optional
Homepage: https://github.com/arthurderichard/qchat
Description: messagerie chiffrée de bout en bout pour réseau local
 QChat est une messagerie instantanée pair-à-pair, sans serveur, pour discuter
 avec ses collègues sur le réseau interne. Les échanges sont chiffrés de bout
 en bout, les personnes sont identifiées par leurs adresses IP (plusieurs par
 personne), et l'application gère les groupes, le partage de fichiers et les
 notifications de bureau.
EOF
echo "/etc/xdg/autostart/$APP_ID.desktop" > "$ROOT/DEBIAN/conffiles"

# Caches d'icônes et du menu : Ubuntu les met à jour par déclencheurs dpkg, on le fait aussi
# explicitement pour que la nouvelle icône apparaisse à chaque installation et mise à jour.
for script in postinst postrm; do
    cat > "$ROOT/DEBIAN/$script" <<'EOF'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d /usr/share/icons/hicolor ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
    chmod 755 "$ROOT/DEBIAN/$script"
done

mkdir -p "$SRC/dist"
OUT="$SRC/dist/qchat_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT" >/dev/null
echo "$OUT"
