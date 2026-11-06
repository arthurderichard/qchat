#!/usr/bin/env bash
# Ajoute des .deb à un dépôt apt « plat » signé, prêt à être servi en HTTP (GitHub Pages).
# Usage : APT_GPG_PRIVATE_KEY="$(cat cle.asc)" packaging/build-apt-repo.sh <dossier-du-depot> <paquet.deb>...
# Les versions déjà présentes dans <dossier-du-depot>/pool sont conservées.
set -euo pipefail

REPO="$1"; shift
URL="${APT_REPO_URL:-https://arthurderichard.github.io/qchat}"
: "${APT_GPG_PRIVATE_KEY:?la clé privée de signature doit être fournie dans APT_GPG_PRIVATE_KEY}"

mkdir -p "$REPO/pool"
cp "$@" "$REPO/pool/"
cd "$REPO"

GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
printf '%s\n' "$APT_GPG_PRIVATE_KEY" | gpg --batch --quiet --import
KEY="$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr/ {print $10; exit}')"

dpkg-scanpackages --multiversion pool /dev/null 2>/dev/null > Packages
gzip -9nkf Packages
RELEASE="$(mktemp)"
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=QChat \
    -o APT::FTPArchive::Release::Label=QChat \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=stable \
    -o "APT::FTPArchive::Release::Description=Dépôt apt de QChat" \
    release . > "$RELEASE"
mv "$RELEASE" Release
chmod 644 Release
gpg --batch --yes --local-user "$KEY" --digest-algo SHA512 --clearsign -o InRelease Release
gpg --batch --yes --local-user "$KEY" --digest-algo SHA512 --armor --detach-sign -o Release.gpg Release
gpg --batch --yes --export "$KEY" > qchat-archive-keyring.gpg

VERSION="$(dpkg-deb -f "$(ls -v pool/*.deb | tail -1)" Version)"
touch .nojekyll
cat > index.html <<EOF
<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QChat — dépôt apt</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}
pre{background:#f3f3f3;padding:.8rem;overflow-x:auto;border-radius:6px}
@media(prefers-color-scheme:dark){body{background:#1e1e1e;color:#ddd}pre{background:#2b2b2b}a{color:#8ab4f8}}</style>
</head><body>
<h1>QChat</h1>
<p>Messagerie chiffrée de bout en bout pour réseau local. Dernière version : <strong>$VERSION</strong>.</p>
<h2>Installation (Ubuntu, Debian et dérivées)</h2>
<pre>sudo curl -fsSLo /usr/share/keyrings/qchat-archive-keyring.gpg $URL/qchat-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/qchat-archive-keyring.gpg] $URL ./" | sudo tee /etc/apt/sources.list.d/qchat.list
sudo apt update &amp;&amp; sudo apt install qchat</pre>
<p>Les mises à jour arrivent ensuite avec <code>sudo apt upgrade</code>.</p>
<p>Empreinte de la clé de signature : <code>$KEY</code></p>
<p><a href="https://github.com/arthurderichard/qchat">Code source</a></p>
</body></html>
EOF
echo "Dépôt prêt dans $(pwd) (clé $KEY)"
