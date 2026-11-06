#!/usr/bin/env bash
# Installation de QChat.
#   ./install.sh              installation pour l'utilisateur courant (~/.local)
#   sudo ./install.sh --system installation pour tous les utilisateurs du poste
#   ./install.sh --uninstall   (ou sudo ./install.sh --system --uninstall)
set -euo pipefail

APP_ID="fr.qchat.QChat"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=user
ACTION=install
for arg in "$@"; do
    case "$arg" in
        --system) MODE=system ;;
        --uninstall) ACTION=uninstall ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "Option inconnue : $arg" >&2; exit 1 ;;
    esac
done

if [[ $MODE == system ]]; then
    [[ $EUID -eq 0 ]] || { echo "L'installation --system doit être lancée avec sudo." >&2; exit 1; }
    LIB=/opt/qchat
    BIN=/usr/local/bin
    SHARE=/usr/share
    AUTOSTART=/etc/xdg/autostart
else
    LIB="$HOME/.local/share/qchat-app"
    BIN="$HOME/.local/bin"
    SHARE="$HOME/.local/share"
    AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
fi
DESKTOP="$SHARE/applications/$APP_ID.desktop"
ICON="$SHARE/icons/hicolor/scalable/apps/qchat.svg"

refresh_caches() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$SHARE/applications" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" || true
}

if [[ $ACTION == uninstall ]]; then
    rm -rf "$LIB" "$BIN/qchat" "$DESKTOP" "$ICON" "$AUTOSTART/$APP_ID.desktop"
    refresh_caches
    echo "QChat désinstallé (les conversations restent dans ~/.local/share/qchat)."
    exit 0
fi

# --- Dépendances ---------------------------------------------------------
missing=()
python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null \
    || missing+=(python3-gi gir1.2-gtk-3.0)
python3 -c 'import nacl.signing' 2>/dev/null || missing+=(python3-nacl)
if ((${#missing[@]})); then
    if [[ $EUID -eq 0 ]] && command -v apt-get >/dev/null; then
        apt-get install -y "${missing[@]}"
    else
        echo "Dépendances manquantes. Installez-les avec :" >&2
        echo "    sudo apt install ${missing[*]}" >&2
        exit 1
    fi
fi

# --- Fichiers ------------------------------------------------------------
mkdir -p "$LIB" "$BIN" "$(dirname "$DESKTOP")" "$(dirname "$ICON")" "$AUTOSTART"
rm -rf "$LIB/qchat"
cp -r "$SRC/qchat" "$LIB/"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
install -m 644 "$SRC/data/qchat.svg" "$ICON"

cat > "$BIN/qchat" <<EOF
#!/bin/sh
export PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m qchat "\$@"
EOF
chmod 755 "$BIN/qchat"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=QChat
GenericName=Messagerie interne
Comment=Discussion chiffrée de bout en bout sur le réseau local
Exec=$BIN/qchat
Icon=qchat
Terminal=false
Categories=Network;InstantMessaging;Chat;
Keywords=chat;messagerie;réseau;lan;
StartupNotify=true
StartupWMClass=$APP_ID
X-GNOME-UsesNotifications=true
EOF

# Démarrage automatique en arrière-plan à l'ouverture de session,
# pour recevoir les messages et notifications sans ouvrir la fenêtre.
sed -e "s|^Exec=.*|Exec=$BIN/qchat --background|" "$DESKTOP" > "$AUTOSTART/$APP_ID.desktop"
echo "X-GNOME-Autostart-enabled=true" >> "$AUTOSTART/$APP_ID.desktop"

refresh_caches

# --- Pare-feu ------------------------------------------------------------
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    if [[ $EUID -eq 0 ]]; then
        ufw allow 45455/tcp comment QChat >/dev/null
        ufw allow 45454/udp comment "QChat découverte" >/dev/null
        echo "Pare-feu : ports 45455/tcp et 45454/udp ouverts."
    else
        echo "ATTENTION : le pare-feu ufw est actif. Ouvrez les ports avec :"
        echo "    sudo ufw allow 45455/tcp && sudo ufw allow 45454/udp"
    fi
fi

echo "QChat installé. Lancez-le depuis le menu des applications ou avec la commande « qchat »."
[[ $MODE == user && ":$PATH:" != *":$BIN:"* ]] && echo "(Ajoutez $BIN à votre PATH pour la commande « qchat ».)"
exit 0
