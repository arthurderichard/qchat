# QChat

Messagerie instantanée **pair-à-pair** pour le réseau interne, sous Linux.
Aucun serveur : chaque poste discute directement avec les autres.

- **Chiffrement de bout en bout** de tous les échanges (et confidentialité persistante).
- **Notifications de bureau** natives (GNOME, KDE, XFCE, Cinnamon, MATE…). Un clic ouvre la conversation.
- **Groupes** : création, ajout de membres par n'importe quel membre, départ.
- **Partage d'images et de fichiers** (jusqu'à 200 Mo) dans les conversations et les groupes,
  chiffré lui aussi de bout en bout.
- **Interface moderne** aux couleurs de QChat, en **thème clair ou sombre** : suit par défaut le
  réglage du bureau (et ses changements en direct), ou se force via ☰ › Thème.
- **Réactions par emoji** sur les messages et les fichiers (❤️ 👍 👎 😂 😮 😢 🔥, ou n'importe quel emoji via +).
- **Mentions** dans les groupes : `@Tristan` met le message en évidence et prévient la personne.
- **Modification des messages** envoyés, avec l'historique des versions consultable par tous.
- **Réponse à un message** précis (citation cliquable) et **suppression** de ses propres messages.
- **Emoji** : sélecteur complet (bouton 😊 de la barre de message) avec recherche en français.
- **Photos de profil et de groupe**, synchronisées chez tous vos contacts.
- **GIF** : recherche et envoi de GIF depuis la barre de message, sans rien configurer (KLIPY).
- **Wizz** : `/wizz` (ou `/poke`), ou le menu *Interactions* de la conversation, fait surgir chez le destinataire une fenêtre animée et sonore.
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

**Mises à jour automatiques** (depuis la 1.5.0) : le paquet installe un minuteur systemd,
`qchat-update.timer`, qui interroge le dépôt QChat deux fois par jour au plus : vers 10 h et 15 h
(± 30 min pour étaler les postes), une vérification manquée poste éteint étant rattrapée au démarrage.
Il ne consulte que ce dépôt signé et ne met à jour que le paquet `qchat` (avec ses éventuelles
nouvelles dépendances). Dès que la nouvelle version est installée, QChat se relance tout seul : sans
rien changer s'il tournait en arrière-plan, sur la même conversation si la fenêtre était ouverte. Il
attend pour cela qu'aucun message ne soit en cours de saisie ni aucune fenêtre secondaire ouverte.
Une notification annonce ensuite la nouvelle version.

**Redémarrer QChat** (menu ☰) relance l'application à la demande, par exemple juste après une mise à
jour faite à la main avec apt. Quand une version plus récente est installée, le bouton ☰ est mis en
évidence et l'entrée devient « Redémarrer pour passer en X.Y.Z ».

```bash
systemctl list-timers qchat-update.timer     # prochaine vérification
sudo systemctl start qchat-update.service    # vérifier tout de suite
journalctl -u qchat-update.service           # historique des mises à jour
sudo systemctl disable --now qchat-update.timer   # désactiver les mises à jour automatiques
```

Les postes encore en 1.4.0 ou avant doivent passer une fois à la main en 1.5.0
(`sudo apt update && sudo apt upgrade`) ; ensuite tout est automatique.

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

**Réactions** : survolez un message et cliquez sur 😊 (ou clic droit sur le message), puis choisissez
un emoji. Les réactions s'affichent sous le message ; cliquez sur une pastille pour ajouter ou retirer
la vôtre, survolez-la pour voir qui a réagi. Vous êtes notifié quand quelqu'un réagit à vos messages.

**Modifier un message** : survolez l'un de vos messages et cliquez sur ✏ (ou clic droit › *Modifier*,
ou flèche ↑ dans une zone de saisie vide pour le dernier envoyé). Entrée enregistre, Échap annule.
Le message porte alors la mention « modifié » : cliquez dessus pour voir toutes ses versions.
Les postes en version antérieure à 1.2.0 continuent d'afficher le texte d'origine.

**Photos** : menu ☰ › *Mon identité* › **Choisir une photo…** (ou *Retirer*). L'image est recadrée en
carré et réduite, puis envoyée à tous vos contacts, même hors ligne (elle partira dès qu'ils seront
joignables). Pour un groupe : bouton ⋮ de la discussion › **Choisir une photo…** ; n'importe quel
membre peut la changer, et les nouveaux membres la reçoivent avec leur invitation. Les postes en
version antérieure à 1.3.0 continuent d'afficher les initiales.

**GIF** : bouton **GIF** de la barre de message, prêt à l'emploi (aucune clé à saisir). Les tendances
s'affichent, tapez pour chercher, survolez une vignette pour l'animer, cliquez (ou Entrée pour le premier
résultat) pour envoyer : le GIF est téléchargé puis envoyé comme une image ordinaire, chiffrée — vos
contacts n'ont besoin ni d'Internet ni de rien d'autre. La recherche passe par le service KLIPY
(api.klipy.com), avec le filtrage de contenu le plus strict ; c'est la seule fonction de QChat qui
contacte Internet.

**Répondre à un message** : survolez-le et cliquez sur ↩ (ou clic droit › *Répondre*). Le message cité
apparaît au-dessus de la réponse ; cliquez sur la citation pour retrouver l'original. Échap annule.

**Supprimer un message** : clic droit sur l'un de vos messages › *Supprimer*. Chez vous comme chez vos
destinataires, il est remplacé par « Message supprimé par son auteur » ; son texte, ses versions, ses
réactions et sa pièce jointe sont effacés.

**Mentionner quelqu'un** (groupes) : tapez `@` puis le début de son nom ; choisissez dans la liste
(↑/↓ puis Entrée ou Tab). `@Prénom` suffit aussi quand le prénom est unique dans le groupe. La
mention apparaît en couleur, et la personne mentionnée voit le message marqué d'un trait et reçoit
une notification « … vous a mentionné ».

**Réagir** : survolez un message et cliquez sur 😊 (ou clic droit) : ❤️ 👍 👎 😂 😮 😢 🔥, ou **+** pour
choisir n'importe quel emoji. Un message composé uniquement d'emoji s'affiche en grand.

**Emoji** : le bouton 😊, à droite de *GIF*, ouvre la liste de tous les emoji, par catégorie, avec les
emoji récents en tête. Tapez pour chercher en français (« cœur », « pouce », « fête »…), Entrée insère
le premier résultat.

**Copier** : surlignez un passage d'un message puis Ctrl+C, ou clic droit › *Copier le texte* pour
le message entier. Les images s'affichent sans nom de fichier ; clic droit pour les enregistrer.

**Wizz** : tapez `/wizz` (ou son alias `/poke`) puis Entrée, ou passez par le bouton **⚡ Interactions**
en haut d'une conversation avec une personne. Chez le destinataire — dans un groupe, chez
tous les membres — une fenêtre surgit au milieu de l'écran principal : elle tremble, l'avatar rebondit,
« WIZZ ! » ondule en arc-en-ciel au son d'un bzzz. *Répondre* ouvre la conversation ; elle se ferme seule
après quelques secondes. Anti-spam : un wizz ou poke toutes les 30 s au plus, toutes conversations confondues (et pas plus d'une fenêtre par contact toutes les 30 s à la réception) ; un wizz reçu plus de 2 min
après son envoi (destinataire hors ligne) est simplement noté dans la conversation. Sous Wayland, c'est
le bureau qui place la fenêtre (en général au centre de l'écran actif).

**Images et fichiers** : bouton 📎, glisser-déposer n'importe où dans la fenêtre, ou Ctrl+V (fichiers copiés
dans le gestionnaire de fichiers, ou capture d'écran — un aperçu demande confirmation). Les images
s'affichent en miniature, et les GIF y sont animés ; chaque pièce jointe propose *Ouvrir*, *Enregistrer
sous…* et *Afficher dans le dossier*.

**Visionneuse** : un clic sur une miniature (ou *Ouvrir*) affiche les images, les PDF et les fichiers
texte dans QChat même. Zoom avec + / − / 0 (ajuster), Ctrl+molette ou double-clic sur une image ;
← → passent aux autres fichiers de la discussion ; Échap ferme. Les autres types de fichiers s'ouvrent
avec l'application par défaut. Les PDF utilisent `pdftoppm` (paquet `poppler-utils`), et la coloration
du code utilise GtkSourceView (`gir1.2-gtksource-4`) s'il est installé. Les fichiers reçus sont rangés dans
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
| `qchat/gifs.py`    | recherche et téléchargement de GIF via l'API KLIPY           |
| `qchat/emojis.py`  | liste des emoji (noms et mots-clés français fournis par GTK)  |
| `qchat/updater.py` | détection d'une version installée plus récente et redémarrage |
| `qchat/viewer.py`  | visionneuse intégrée (images, GIF animés, PDF, texte)       |
| `qchat/wizz.py`    | fenêtre animée des wizz / poke et son bzzz                  |

## Publier une version

1. Mettre à jour `__version__` dans `qchat/__init__.py` et committer.
2. `git tag v1.2.3 && git push origin main v1.2.3`

GitHub Actions ([release.yml](.github/workflows/release.yml)) lance les tests, construit le `.deb`
(`packaging/build-deb.sh`), crée la Release GitHub et met à jour le dépôt apt signé sur la branche
`gh-pages` (`packaging/build-apt-repo.sh`), servi par GitHub Pages. Le secret `APT_GPG_PRIVATE_KEY`
contient la clé privée de signature du dépôt.

## Licence

[MIT](LICENSE)
