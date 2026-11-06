# QChat

Messagerie instantanée **pair-à-pair** pour le réseau interne, sous Linux.
Aucun serveur : chaque poste discute directement avec les autres.

- **Chiffrement de bout en bout** de tous les échanges (et confidentialité persistante).
- **Notifications de bureau** natives (GNOME, KDE, XFCE, Cinnamon, MATE…). Un clic ouvre la conversation.
- **Groupes** : création, ajout de membres par n'importe quel membre, départ.
- **Partage d'images et de fichiers** (jusqu'à 200 Mo) dans les conversations et les groupes,
  chiffré lui aussi de bout en bout.
- **Personnes identifiées par leurs IP**, avec **plusieurs IP par personne** (Wi-Fi, Ethernet…).
- **File d'attente hors ligne** : un message à quelqu'un d'absent part automatiquement à son retour (🕓 → ✓).
- **Découverte du réseau** (🔍, Ctrl+D) : analyse une plage d'IP et liste les postes où QChat est
  lancé, avec le nom et l'empreinte de la personne ; un clic pour l'ajouter.
- **Annonces automatiques** : les collègues présents apparaissent aussi seuls dans « Sur le réseau ».

## Installation

### Avec apt (recommandé)

Pour Ubuntu 22.04 et plus récent, Debian 12 et plus récent, et leurs dérivées (Linux Mint, Pop!_OS…).
Le dépôt est signé : apt refuse tout paquet qui n'a pas été publié par le projet.

**1. Ajouter la clé de signature du dépôt**

```bash
sudo curl -fsSLo /usr/share/keyrings/qchat-archive-keyring.gpg https://arthurderichard.github.io/qchat/qchat-archive-keyring.gpg
```

Facultatif, pour vérifier la clé — l'empreinte affichée doit être
`58FB 7C5A 3A56 A870 274E  FF6B 26DE DB67 9AE3 DF57` :

```bash
gpg --show-keys /usr/share/keyrings/qchat-archive-keyring.gpg
```

**2. Ajouter le dépôt QChat aux sources d'apt**

```bash
echo "deb [signed-by=/usr/share/keyrings/qchat-archive-keyring.gpg] https://arthurderichard.github.io/qchat ./" | sudo tee /etc/apt/sources.list.d/qchat.list
```

**3. Installer**

```bash
sudo apt update
sudo apt install qchat
```

QChat apparaît dans le menu des applications et se lance automatiquement en arrière-plan à
l'ouverture de session suivante. Pour le démarrer tout de suite : `qchat`.

**Mises à jour** : elles arrivent avec les mises à jour habituelles du système
(`sudo apt update && sudo apt upgrade`, ou le gestionnaire de mises à jour).

**Désinstallation** :

```bash
sudo apt remove qchat
sudo rm /etc/apt/sources.list.d/qchat.list /usr/share/keyrings/qchat-archive-keyring.gpg
```

Vos conversations restent dans `~/.local/share/qchat` ; supprimez ce dossier pour tout effacer.

**Sans ajouter le dépôt** : téléchargez `qchat_X.Y.Z_all.deb` depuis les
[Releases](https://github.com/arthurderichard/qchat/releases) puis
`sudo apt install ./qchat_X.Y.Z_all.deb` (pas de mises à jour automatiques dans ce cas).

**En cas de problème**
- `NO_PUBKEY` ou « n'est pas signé » : refaites l'étape 1 (clé absente ou incomplète).
- Deux entrées QChat dans le menu : une ancienne installation par `install.sh` est présente,
  retirez-la avec `./install.sh --uninstall` (ou `sudo ./install.sh --system --uninstall`).
- Les collègues ne vous voient pas : vérifiez le pare-feu (voir [Réseau](#réseau)).

### Depuis les sources

Dépendances (Ubuntu/Debian) : `sudo apt install python3-gi gir1.2-gtk-3.0 python3-nacl`

```bash
./install.sh                 # pour l'utilisateur courant
sudo ./install.sh --system   # pour tous les utilisateurs du poste (/opt/qchat)
./install.sh --uninstall     # désinstallation
```

L'installation ajoute QChat au menu des applications et le **lance en arrière-plan à l'ouverture de
session** : les notifications arrivent même fenêtre fermée. Fermer la fenêtre ne quitte pas
l'application ; utilisez ☰ › Quitter (Ctrl+Q).

Sans installation : `python3 -m qchat` depuis ce dossier.

### Réseau

| Port      | Usage                                        |
|-----------|----------------------------------------------|
| 45455/tcp | messages (chiffrés)                          |
| 45454/udp | annonces de présence (diffusion + unicast)   |

Si le pare-feu `ufw` est actif (`sudo ufw status`), ouvrez ces ports (`sudo ./install.sh --system` le fait automatiquement) :
`sudo ufw allow 45455/tcp && sudo ufw allow 45454/udp`.

## Utilisation

1. Au premier lancement, choisissez votre nom affiché.
2. **Ajouter quelqu'un**, au choix :
   - **🔍 Découverte** : la plage de votre sous-réseau est préremplie (modifiable : `10.102.0.0/23`,
     `10.102.4.1-50`, une IP… ; 4096 adresses max). Chaque poste QChat trouvé s'affiche avec son nom,
     ses IP et son empreinte → **Ajouter**. Une personne trouvée sur plusieurs IP n'apparaît qu'une
     fois. La sonde s'arrête avant la fin de la poignée de main : rien n'est créé ni notifié chez la
     personne sondée, mais son identité est authentifiée (réponse signée par sa clé).
   - bouton **+** › *Ajouter un contact par IP…*, en saisissant son nom et une ou plusieurs IP ;
   - un clic sur un collègue de la section **Sur le réseau**.
3. Dès la première connexion, QChat récupère sa clé et **associe automatiquement toutes ses autres
   IP**. Les IP restent modifiables dans la fiche du contact (bouton ⋮).
4. **Groupe** : **+** › *Nouveau groupe…* ; puis ⋮ › *Ajouter des membres…*. Les nouveaux membres
   reçoivent la liste complète des participants et peuvent écrire à tous, même sans les avoir ajoutés
   eux-mêmes.

Entrée envoie, Maj+Entrée fait un retour à la ligne.

**Images et fichiers** : bouton 📎, glisser-déposer dans la conversation, ou Ctrl+V (fichiers copiés
dans le gestionnaire de fichiers, ou capture d'écran — un aperçu demande confirmation). Les images
s'affichent en miniature (clic pour ouvrir) ; chaque pièce jointe propose *Ouvrir*, *Enregistrer
sous…* et *Afficher dans le dossier*. Les fichiers reçus sont rangés dans
`~/.local/share/qchat/files/` (droits 600, jamais exécutables). Un fichier destiné à quelqu'un
d'absent part à son retour, comme un message.

## Sécurité

- Chaque poste génère une **clé d'identité Ed25519** (`~/.local/share/qchat/identity.key`, droits 600).
  La personne est la clé ; ses IP y sont rattachées.
- Chaque connexion TCP commence par une **poignée de main signée avec clés X25519 éphémères** :
  les clés de session (une par sens) sont dérivées du secret éphémère et de la transcription
  (BLAKE2b). Les messages sont chiffrés en **XSalsa20-Poly1305** avec un compteur comme nonce, ce qui
  bloque rejeu, réordonnancement et altération. Les clés éphémères sont jetées : un vol ultérieur
  de la clé d'identité ne permet pas de déchiffrer le trafic passé.
- **Confiance au premier contact (TOFU)** : la clé est mémorisée à la première connexion. Si une autre
  clé répond ensuite à l'une des IP d'un contact (réinstallation, IP réattribuée par DHCP, ou
  usurpation), **les envois sont suspendus** et un bandeau demande de valider. Comparez les
  **empreintes** (☰ › *Mon identité* et fiche du contact) de vive voix pour une vérification forte.
- Les annonces UDP sont signées : personne ne peut s'attribuer les IP de quelqu'un d'autre.
- Les fichiers transitent dans la même session chiffrée, par blocs de 192 Ko ; leur empreinte
  SHA-256 est vérifiée à l'arrivée et les noms de fichiers sont nettoyés (pas de `../`).
- Les messages de groupe sont chiffrés séparément pour chaque membre. Un message de groupe venant
  d'un non-membre est ignoré.

**Limites connues**
- L'historique local (`qchat.db`) n'est pas chiffré sur le disque (droits 600). Utilisez le chiffrement
  du disque du poste.
- IPv4 uniquement. Les membres ajoutés à un groupe par un tiers sont acceptés sur la foi de ce
  membre (confiance transitive).
- Une même personne sur deux **ordinateurs** différents apparaît comme deux contacts. Plusieurs
  **interfaces** sur un même poste, en revanche, sont bien gérées.

## Développement

```bash
python3 -m unittest discover -s tests -v       # tests (crypto, livraison, groupes, hors ligne…)
python3 -m qchat --home /tmp/profilB --port 46002 --debug   # 2e profil sur le même poste
```

| Fichier            | Rôle                                                        |
|--------------------|-------------------------------------------------------------|
| `qchat/crypto.py`  | identité, poignée de main, canal chiffré                    |
| `qchat/core.py`    | réseau : serveur TCP, envois, découverte UDP, groupes        |
| `qchat/store.py`   | base SQLite (contacts, IP, groupes, messages, file d'envoi) |
| `qchat/netutil.py` | interfaces réseau locales, validation des IP                |
| `qchat/notify.py`  | notifications D-Bus                                         |
| `qchat/gui.py`     | interface GTK 3                                             |

## Publier une version

1. Mettre à jour `__version__` dans `qchat/__init__.py` et committer.
2. `git tag v1.2.3 && git push origin main v1.2.3`

GitHub Actions ([release.yml](.github/workflows/release.yml)) lance les tests, construit le `.deb`
(`packaging/build-deb.sh`), crée la Release GitHub et met à jour le dépôt apt signé sur la branche
`gh-pages` (`packaging/build-apt-repo.sh`), servi par GitHub Pages. Le secret `APT_GPG_PRIVATE_KEY`
contient la clé privée de signature du dépôt.

## Licence

[MIT](LICENSE)
