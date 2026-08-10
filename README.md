# abs-m4b-tagger

Écrit les métadonnées d'**Audiobookshelf** directement dans les tags des fichiers
`.m4b` / `.m4a` / `.mp3`, **en local sur le NAS**, sans passer par Mp3tag ni télécharger
les fichiers. Assure aussi le triage des livres qui demandent une intervention manuelle.

Le mapping des tags reprend celui du guide *Plex-Audiobook-Guide* (seanap / CaptZapp1729),
mais la source de vérité est ta base Audiobookshelf au lieu d'Audible.com.

## Pourquoi

Mp3tag travaille via SMB : il lit le fichier entier depuis le NAS, le modifie côté PC, puis
le renvoie. Sur un m4b de 800 Mo, ça fait ~1,6 Go de trafic réseau par livre. Ici le
conteneur tourne sur le NAS, monte la bibliothèque en local et ne réécrit que l'atome
`moov` — quelques Ko, moins d'une seconde par livre.

---

## 1. Publier le dépôt sur GitHub

Sur github.com, crée un dépôt **public** nommé `abs-m4b-tagger`, sans README ni .gitignore
(ils sont déjà là). Puis, depuis le dossier du projet :

```bash
cd /volume1/docker/abs-tagger      # ou ton dossier local

git init -b main
git add .
git commit -m "abs-m4b-tagger : version initiale"
git remote add origin https://github.com/<TON-COMPTE>/abs-m4b-tagger.git
git push -u origin main
```

Le `.gitignore` exclut déjà `.env`, `config/` et `state.json`. **Ne commite jamais ta clé
API Audiobookshelf** : elle se saisit dans Portainer, pas dans le dépôt.

> Un dépôt privé fonctionne aussi, mais Portainer demandera alors un *Personal Access
> Token* GitHub avec la portée `repo`.

---

## 2. Installer depuis GitHub via Portainer

### Récupérer une clé API Audiobookshelf

`Paramètres → Utilisateurs → ton compte → Clés API` (ABS ≥ 2.26).
Sur les versions antérieures : `Paramètres → Utilisateurs → Voir le token`.

### Préparer les dossiers sur le NAS

```bash
mkdir -p /volume1/docker/abs-tagger/config /volume1/media/a-trier
```

`/volume1/media/a-trier` doit être **hors** de la bibliothèque Audiobookshelf.

### Créer le stack

Portainer → **Stacks → Add stack → Repository** :

| Champ | Valeur |
|---|---|
| Name | `abs-m4b-tagger` |
| Repository URL | `https://github.com/<TON-COMPTE>/abs-m4b-tagger` |
| Repository reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |

Puis, section **Environment variables**, ajoute au minimum :

```
ABS_URL      = http://192.168.1.90:13378
ABS_TOKEN    = <ta clé API>
PATH_MAP     = /audiobooks:/livres
LIBRARY_DIR  = /volume1/media/livres
PUID         = 1026
PGID         = 100
DRY_RUN      = true          <- pour le premier essai
```

Toutes les autres variables ont une valeur par défaut ; la liste complète est dans
`.env.example`. **Deploy the stack** : Portainer clone le dépôt, construit l'image à partir
du `Dockerfile` et démarre le conteneur.

Active **GitOps updates** si tu veux que Portainer redéploie automatiquement à chaque push.

### Les trois réglages critiques

| Réglage | À vérifier |
|---|---|
| `ABS_URL` / `ABS_TOKEN` | adresse du serveur ABS + clé API |
| `PATH_MAP` | correspondance entre les chemins **vus par ABS** et ceux montés ici |
| `PUID` / `PGID` | UID/GID ayant le droit d'écrire dans la bibliothèque |

**`PATH_MAP` est la cause d'erreur n°1.** Si la bibliothèque ABS pointe sur `/audiobooks`
(chemin interne au conteneur Audiobookshelf) et que le même dossier est monté ici sur
`/livres`, il faut `PATH_MAP=/audiobooks:/livres`. Le plus simple reste de monter le même
chemin des deux côtés — dans ce cas `PATH_MAP` peut rester vide.

Pour l'UID/GID sur Synology : en SSH, `id ton_utilisateur` (souvent `1026:100`).

### Premier lancement en simulation

Laisse `DRY_RUN=true` pour la première passe. Rien n'est écrit, rien n'est déplacé : tu
vérifies dans les logs du conteneur que les chemins sont bien résolus. Si tu vois
`fichier introuvable`, c'est `PATH_MAP` ou le montage qui ne colle pas. Une fois validé,
repasse `DRY_RUN` à `false` et redéploie.

---

## 3. Variante sans construction : image GHCR

Le workflow `.github/workflows/docker-publish.yml` publie automatiquement l'image sur
GitHub Container Registry à chaque push sur `main`. Rien à configurer : le `GITHUB_TOKEN`
fourni par Actions suffit.

Après le premier build réussi, rends le package public dans GitHub
(`Packages → abs-m4b-tagger → Package settings → Change visibility`), sinon Portainer devra
s'authentifier.

Crée alors le stack avec **Compose path = `docker-compose.ghcr.yml`** et la variable
`IMAGE = ghcr.io/<TON-COMPTE>/abs-m4b-tagger:latest`. Portainer se contente de tirer
l'image : déploiement en quelques secondes, sans compilation sur le NAS.

---

## 4. Mettre à jour

```bash
git add -A && git commit -m "Ajuste les critères de triage" && git push
```

Puis dans Portainer, sur le stack : **Pull and redeploy** (ou automatiquement si GitOps est
activé).

---

## Le triage : livres à traiter à la main

Deux situations distinctes.

### A. Le livre existe dans ABS mais n'est pas identifié

Jamais « matché » avec Audible, pas d'ASIN, pas d'auteur, pas de pochette. Écrire ces
métadonnées vides dans les fichiers ne servirait à rien : le conteneur saute l'écriture et
**signale** le livre.

`INCOMPLETE_CHECKS` définit ce qui rend un livre incomplet. Valeurs possibles :
`identifier` (ASIN **ou** ISBN), `author`, `narrator`, `cover`, `description`, `series`,
`year`, `publisher`, `genres`, `title`. Défaut : `identifier,author,cover`.

`ON_INCOMPLETE` définit l'action :

| Valeur | Effet |
|---|---|
| `tag` *(défaut)* | Ajoute le tag ABS `INCOMPLETE_TAG` (défaut `a-identifier`) à l'item. Tu filtres ensuite dans l'interface ABS et tu fais « Match » manuellement. |
| `move` | Déplace le dossier du livre dans `UNMATCHED_DIR`, hors bibliothèque. |
| `both` | Les deux. |
| `none` | Ne rien faire. |

Avec `REMOVE_TAG_WHEN_COMPLETE=true`, le tag est **retiré automatiquement** dès que tu as
complété le livre dans ABS : la passe suivante le détecte, retire le tag et écrit enfin les
tags dans les fichiers. La liste des livres à traiter se vide toute seule.

Pour filtrer dans Audiobookshelf : `Filtrer → Tags → a-identifier`.

### B. Le fichier est sur le disque mais absent d'Audiobookshelf

Dossier mal nommé, scan jamais lancé, extension inhabituelle… Après avoir parcouru toute la
bibliothèque, le conteneur compare les fichiers audio du disque à ceux connus d'ABS.

| `ORPHAN_ACTION` | Effet |
|---|---|
| `report` *(défaut)* | Liste les orphelins dans les logs et dans `/config/a-traiter.json`. |
| `move` | Les déplace dans `UNMATCHED_DIR` en conservant l'arborescence relative. |
| `none` | Désactive la détection. |

Garde-fous appliqués :

- Un dossier n'est déplacé en bloc que si **tous** ses fichiers audio sont orphelins ; dans
  un dossier mixte, seuls les fichiers concernés partent.
- Les fichiers modifiés depuis moins de `ORPHAN_MIN_AGE_MIN` minutes (défaut 30) sont
  ignorés — ils sont probablement en cours de copie.
- La détection ne tourne **que sur une passe complète** : désactivée si tu utilises
  `--item` ou `ABS_LIBRARIES`, sinon tout le reste passerait pour orphelin.
- `UNMATCHED_DIR` doit être hors de la bibliothèque ABS, sinon le scanner reprend les
  fichiers que tu viens de mettre de côté.

Après un déplacement, ABS marque les items concernés comme manquants ; purge-les via
`Paramètres → Bibliothèques → Supprimer les éléments manquants`.

### Rapport

Chaque passe écrit `/config/a-traiter.json` : livres non identifiés (avec les champs
manquants) et fichiers orphelins.

---

## Variables d'environnement

### Connexion et chemins

| Variable | Défaut | Rôle |
|---|---|---|
| `ABS_URL` | — | URL du serveur, ex. `http://192.168.1.90:13378` |
| `ABS_TOKEN` | — | Clé API Audiobookshelf |
| `ABS_VERIFY_SSL` | `true` | `false` pour un certificat auto-signé |
| `PATH_MAP` | `/audiobooks:/livres` | `"<vu par ABS>:<monté ici>"`, paires séparées par `;` |
| `ABS_LIBRARIES` | vide | Noms ou ids de bibliothèques, séparés par `,` (vide = toutes) |
| `ONLY_ITEM_IDS` | vide | Ids d'items précis, séparés par `,` |
| `CONFIG_DIR` / `LIBRARY_DIR` / `TRIAGE_DIR` | voir `.env.example` | Montages côté NAS |
| `PUID` / `PGID` | `1026` / `100` | Identité du conteneur |

### Fonctionnement

| Variable | Défaut | Rôle |
|---|---|---|
| `INTERVAL` | `3600` | Secondes entre deux passes. `0` = une passe puis arrêt |
| `DRY_RUN` | `false` | N'écrit et ne déplace rien |
| `FORCE` | `false` | Ignore le cache et retague tout |
| `LOG_LEVEL` | `INFO` | `DEBUG` pour le détail fichier par fichier |

### Tags écrits dans les fichiers

| Variable | Défaut | Rôle |
|---|---|---|
| `WRITE_COVER` | `true` | Intègre la pochette |
| `COVER_MAX_PX` | `1200` | Redimensionne la pochette. `0` = original |
| `STRIP_HTML` | `true` | Nettoie le HTML des résumés ABS |
| `WRITE_COMMENT` | `true` | Écrit aussi le résumé dans `COMMENT` |
| `TRACK_TITLE_MODE` | `filename` | Titre de piste des livres multi-fichiers : `filename`, `chapter` ou `title` |
| `SYNC_CHAPTERS` | `false` | Réécrit les chapitres via ffmpeg (voir avertissement) |
| `WRITE_SIDECARS` | `false` | Génère `cover.jpg` / `desc.txt` / `reader.txt` |

### Triage

| Variable | Défaut | Rôle |
|---|---|---|
| `INCOMPLETE_CHECKS` | `identifier,author,cover` | Champs dont l'absence rend un livre « à traiter » |
| `ON_INCOMPLETE` | `tag` | `tag`, `move`, `both` ou `none` |
| `INCOMPLETE_TAG` | `a-identifier` | Tag posé dans Audiobookshelf |
| `REMOVE_TAG_WHEN_COMPLETE` | `true` | Retire le tag une fois le livre complété |
| `TAG_INCOMPLETE_FILES` | `false` | Écrire quand même les tags des livres incomplets |
| `ORPHAN_ACTION` | `report` | `report`, `move` ou `none` |
| `ORPHAN_SCAN_DIRS` | `/livres` | Racines à scanner |
| `ORPHAN_MIN_AGE_MIN` | `30` | Âge minimum d'un fichier pour être considéré orphelin |
| `UNMATCHED_DIR` | `/a-trier` | Dossier de tri manuel, hors bibliothèque |

Arguments CLI : `--once`, `--dry-run`, `--force`, `--item <id>`, `--library <nom>`, `--no-triage`.

---

## Tags écrits

### M4B / M4A (atomes MP4)

| Atome | Contenu |
|---|---|
| `©nam` | Titre de piste (voir `TRACK_TITLE_MODE`) |
| `©alb` | Titre du livre |
| `©ART` | Auteur(s) + narrateur(s), dédoublonnés |
| `aART` / `soaa` | Auteur(s) |
| `©wrt` | Narrateur(s) |
| `©gen` | Genres séparés par `/` |
| `©day` | Année de publication |
| `©grp` | `Série, Book N` |
| `soal` | `Série 01 - Titre` (ou `Titre - Sous-titre`) |
| `desc` / `ldes` / `©cmt` | Résumé (court / long / commentaire) |
| `cprt` | Année + éditeur |
| `stik` | `2` = Audiobook |
| `pgap` | Lecture sans blanc |
| `shwm` / `©mvn` / `©mvi` | Série (movement) |
| `trkn` | Numéro de piste / total |
| `covr` | Pochette |
| `----:com.apple.iTunes:*` | `SERIES`, `SERIES-PART`, `SUBTITLE`, `PUBLISHER`, `NARRATOR`, `ASIN`, `ISBN`, `LANGUAGE`, `WWWAUDIOFILE`, `ABS_ITEM_ID` |

### MP3 (ID3v2.3)

`TIT2`, `TALB`, `TIT3`, `TPE1`, `TPE2`, `TCOM`, `TCON`, `TDRC`, `TSOA`, `TIT1`, `TPUB`,
`TCOP`, `TRCK`, `COMM`, `WOAF`, `MVNM`, `MVIN`, `APIC`, plus `TXXX:SERIES`,
`TXXX:SERIES-PART`, `TXXX:ASIN`, `TXXX:ISBN`, `TXXX:SUBTITLE`, `TXXX:NARRATOR`,
`TXXX:LANGUAGE`, `TXXX:ABS_ITEM_ID`.

---

## Points d'attention

**Boucle de métadonnées.** Dans ABS, `Bibliothèque → Paramètres → Scanner → Ordre de
priorité des métadonnées`, laisse `metadata.json` (ou la base) **au-dessus** des tags des
fichiers audio. Sinon ABS relit ce que tu viens d'écrire et peut écraser des champs corrigés
à la main.

**`SYNC_CHAPTERS`.** L'écriture des chapitres passe par ffmpeg en copie de flux : le fichier
est reconstruit, son inode change et ABS le rescanne. Rapide (pas de réencodage) mais ça
touche tout le fichier, contrairement aux tags. Ne s'applique qu'aux livres d'un seul fichier.

**Cache d'état.** `config/state.json` mémorise une empreinte des métadonnées de chaque livre ;
une passe suivante ne réécrit que ce qui a changé dans ABS. Utilise `FORCE=true` après avoir
modifié la configuration des tags.

**Déplacements.** Commence toujours par `ORPHAN_ACTION=report` et `ON_INCOMPLETE=tag`. Ne
passe à `move` qu'une fois `a-traiter.json` relu et validé.

---

## Structure du dépôt

```
abs-m4b-tagger/
├── .github/workflows/docker-publish.yml   # build + push GHCR
├── app/
│   ├── main.py          # orchestration, passes, rapport
│   ├── config.py        # variables d'environnement
│   ├── absclient.py     # client API Audiobookshelf
│   ├── tagger.py        # normalisation + écriture MP4/ID3
│   ├── chapters.py      # synchronisation des chapitres (ffmpeg)
│   └── triage.py        # livres non identifiés et fichiers orphelins
├── Dockerfile
├── docker-compose.yml        # stack Portainer, build depuis le dépôt
├── docker-compose.ghcr.yml   # stack Portainer, image pré-construite
├── .env.example
├── requirements.txt
└── LICENSE
```

## Usage en ligne de commande

```bash
docker compose run --rm abs-m4b-tagger --once --dry-run
docker compose run --rm abs-m4b-tagger --once --item li_8gch9ve09orgn4fdz8
docker compose run --rm abs-m4b-tagger --once --library "Livres audio"
docker compose run --rm abs-m4b-tagger --once --force
docker compose run --rm abs-m4b-tagger --once --no-triage
```
