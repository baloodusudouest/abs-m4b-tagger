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

## Export des livres validés

Une fois un livre taggé **et jugé complet** par le triage, ses fichiers peuvent être copiés
vers une seconde bibliothèque au nommage strict, avec la pochette et les métadonnées ABS.
Les livres signalés comme non identifiés ne sont jamais exportés : la bibliothèque de
destination ne contient donc que du propre.

Activation : `EXPORT_ACTION=hardlink` (ou `copy`, `move`, `symlink`) et `EXPORT_DIR`
pointant vers la destination.

### Gabarits de nommage

| Syntaxe | Effet |
|---|---|
| `{variable}` | Remplacée par sa valeur |
| `<...>` | Bloc optionnel : affiché seulement si **toutes** ses variables sont renseignées |
| `/` | Séparateur de dossiers |

Variables disponibles :

`{auteur}` `{auteur1}` `{auteur_lf}` (Nom, Prénom) `{titre}` `{titre_sans_prefixe}`
`{sous_titre}` `{serie}` `{serie_num}` `{serie_num2}` (01, 02…) `{narrateur}` `{narrateur1}`
`{annee}` `{editeur}` `{asin}` `{isbn}` `{langue}` `{genre}` `{genres}` `{region}`
`{piste}` `{piste2}` `{total}` `{ext}`

Le gabarit par défaut reproduit la convention du guide Plex :

```
EXPORT_DIR_TEMPLATE={auteur}/<{serie}/><{annee} - >{titre}<  [{serie} {serie_num}]>< {asin}>< [{region}]>
EXPORT_FILE_TEMPLATE={titre}
```

Résultat :

```
J.K. Rowling/
└── Le Monde des sorciers/
    └── 2017 - Harry Potter à l'École des Sorciers  [Le Monde des sorciers 1] B06Y64F73B [us]/
        ├── Harry Potter à l'École des Sorciers.m4b
        ├── cover.jpg
        ├── metadata.json
        ├── desc.txt
        └── reader.txt
```

Les blocs optionnels absorbent proprement les champs manquants : un livre hors série sans
ASIN donne simplement `Bernard Werber/1991 - Les Fourmis`, sans crochets vides ni tirets
orphelins. Pour un livre en plusieurs fichiers, ` - {piste2}` est ajouté automatiquement au
nom de fichier s'il ne contient pas déjà `{piste}`.

### Mode lien physique (`EXPORT_ACTION=hardlink`) — recommandé

Un lien physique n'est pas une copie : c'est un **second nom pour le même contenu sur le
disque**. Les deux emplacements pointent vers le même inode.

- **Un seul exemplaire de données.** Un livre de 800 Mo visible dans deux bibliothèques
  n'occupe que 800 Mo. Windows et File Station afficheront la taille aux deux endroits,
  mais l'espace n'est compté qu'une fois par le volume.
- **Modification répercutée.** Écrire les tags avec mutagen modifie le fichier *en place* :
  les deux chemins voient immédiatement le changement, même quand la pochette fait grossir
  le fichier. Aucune resynchronisation n'est nécessaire.
- **Déplaçable librement.** Renommer ou déplacer l'un des deux chemins **sur le même
  volume** ne casse rien : un déplacement intra-volume n'est qu'un changement d'étiquette.
- **Suppression sans risque.** Supprimer un des deux noms ne supprime pas les données ;
  elles ne disparaissent que lorsque le dernier lien est supprimé.

Deux limites réelles :

**Le même volume est obligatoire.** `/volume1` et `/volume2` sont des systèmes de fichiers
distincts : un lien physique entre les deux est impossible, et un déplacement de l'un vers
l'autre est en réalité une copie qui rompt le lien. Le programme le signale au démarrage et
bascule sur une copie plutôt que d'échouer.

**Certains outils cassent le lien sans prévenir.** Un logiciel qui écrit un fichier
temporaire puis le remplace (ffmpeg, donc `SYNC_CHAPTERS`, mais aussi beaucoup d'éditeurs)
crée un nouvel inode : les deux chemins deviennent alors deux fichiers indépendants. Le
programme vérifie l'inode à chaque passe et **rétablit automatiquement le lien** si besoin.

### Configuration Docker indispensable

Les liens physiques ne fonctionnent qu'à l'intérieur d'un même système de fichiers, **tel
que vu par le conteneur**. Il faut donc monter **un seul volume parent** contenant à la
fois la bibliothèque source et la destination :

```yaml
volumes:
  - /volume1/media:/media          # ✅ un seul montage parent
```

```
PATH_MAP=/audiobooks:/media/livres
ORPHAN_SCAN_DIRS=/media/livres
EXPORT_DIR=/media/AudioBooks/Audible
UNMATCHED_DIR=/media/a-trier
```

À éviter — deux montages séparés donnent des copies silencieuses même si tout est sur
`/volume1` :

```yaml
volumes:
  - /volume1/media/livres:/livres              # ❌
  - /volume1/media/AudioBooks:/export          # ❌
```

Au démarrage, le programme compare les systèmes de fichiers et affiche soit
`source et destination sur le même volume, aucun espace supplémentaire consommé`, soit un
avertissement explicite.


### Mode déplacement (`EXPORT_ACTION=move`)

Les fichiers **quittent** la bibliothèque Audiobookshelf. C'est le mode « chaîne
d'import » : ABS sert de zone de préparation, la bibliothèque exportée devient la
référence. Trois conséquences à connaître avant de l'activer.

**Le dossier source est supprimé** une fois ses fichiers audio partis, si
`MOVE_CLEANUP_SOURCE=true` (défaut). La suppression n'a lieu que si le dossier ne contient
plus que des résidus connus — `cover.jpg`, `metadata.json`, `desc.txt`, `reader.txt`,
`book.nfo`, `@eaDir`. **Le moindre fichier inattendu annule la suppression** et le dossier
est conservé avec un message dans les logs.

**Les items restent dans Audiobookshelf, marqués comme manquants.** C'est le
comportement par défaut (`AFTER_MOVE=keep`) : tu gardes l'historique de lecture et tu peux
purger quand tu veux via `Paramètres → Bibliothèques → Supprimer les éléments manquants`.
Avec `AFTER_MOVE=remove`, l'item est retiré de la base ABS dès le déplacement — **aucun
fichier n'est supprimé**, seulement l'entrée en base, mais la progression de lecture est
perdue.

**Les corrections ultérieures restent possibles.** Si tu modifies les métadonnées dans ABS
après le déplacement, la passe suivante retrouve le fichier à son emplacement exporté grâce
au chemin mémorisé dans `state.json`, réécrit ses tags sur place, et renomme dossier et
fichier si le nommage a changé. Rien n'est recopié ni dupliqué.

> Si tu purges les items manquants dans ABS, ce lien est rompu : les livres concernés ne
> seront plus jamais retaggés automatiquement. Purge donc en connaissance de cause.



### Région

`{region}` vaut `EXPORT_REGION` (défaut `us`). Avec `EXPORT_REGION=auto`, elle est déduite
de la langue du livre : français → `fr`, anglais → `us`, allemand → `de`, etc.

### Fichiers annexes

`EXPORT_SIDECARS` accepte, séparés par des virgules :

| Valeur | Fichier produit |
|---|---|
| `cover` | `cover.jpg` (pochette d'origine, non redimensionnée) |
| `metadata` | `metadata.json` au format Audiobookshelf, réimportable |
| `desc` | `desc.txt` (résumé) |
| `reader` | `reader.txt` (narrateur) |
| `nfo` | `book.nfo` (Plex / Jellyfin) |

### Comportement

- **Idempotent** : un fichier déjà à jour n'est pas retransféré. En mode lien physique, « à
  jour » signifie *même inode*, ce qui permet de détecter et réparer un lien rompu.
- **Auto-réparateur** : si le dossier d'export est supprimé ou un fichier manque, la passe
  suivante le recrée, même si les métadonnées ABS n'ont pas changé.
- **Renommage suivi** : si tu changes le gabarit ou si les métadonnées ABS évoluent, l'ancien
  dossier est déplacé vers le nouveau nom plutôt que dupliqué, et les dossiers parents vidés
  sont supprimés.
- **Noms compatibles Windows/SMB** : `:` devient ` -`, `/` et `\` deviennent `-`, `?` `*`
  `<` `>` sont retirés, `"` devient `'`. Les points et espaces en fin de nom sont supprimés
  (donc `Rowling, J.K.` donne `Rowling, J.K`), les composants sont tronqués à
  `EXPORT_MAX_COMPONENT` caractères et les noms réservés (`CON`, `PRN`…) sont préfixés.
- **Espaces doubles conservés** par défaut, pour coller exactement à la convention du guide
  Plex. Mets `EXPORT_COLLAPSE_SPACES=true` pour les réduire.
- `EXPORT_DIR` ne doit pas se trouver dans la bibliothèque source — le programme refuse de
  démarrer sinon, pour éviter qu'Audiobookshelf ne rescanne les copies.
- `hardlink` n'occupe aucun espace supplémentaire, mais impose que source et destination
  soient sur le **même volume** ; sinon le programme bascule automatiquement sur une copie.


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
| `SYNC_CHAPTERS` | `false` | Écrit les chapitres ABS dans le fichier (ffmpeg inclus dans l'image) |
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

### Export des livres validés

| Variable | Défaut | Rôle |
|---|---|---|
| `EXPORT_ACTION` | `hardlink` | `none`, `copy`, `move`, `hardlink` ou `symlink` |
| `MOVE_CLEANUP_SOURCE` | `true` | Supprimer le dossier source vidé (résidus connus uniquement) |
| `AFTER_MOVE` | `keep` | `keep` (item ABS conservé, marqué manquant) ou `remove` (retiré de la base) |
| `MEDIA_DIR` | `/volume1/media` | Volume parent hôte monté sur `/media` |
| `EXPORT_DIR` | `/media/AudioBooks/Audible` | Destination, vue par le conteneur |
| `EXPORT_DIR_TEMPLATE` | voir ci-dessus | Gabarit d'arborescence |
| `EXPORT_FILE_TEMPLATE` | `{titre}` | Gabarit du nom de fichier |
| `EXPORT_SIDECARS` | `cover,metadata,desc,reader` | Annexes à déposer |
| `EXPORT_REGION` | `us` | Code région, ou `auto` d'après la langue |
| `EXPORT_OVERWRITE` | `false` | Recopier même si le fichier existe déjà |
| `EXPORT_COLLAPSE_SPACES` | `false` | Réduire les espaces multiples |
| `EXPORT_MAX_COMPONENT` | `180` | Longueur max d'un dossier ou fichier |
| `EXPORT_PRUNE_STALE` | `true` | Supprimer les fichiers obsolètes laissés par un renommage |

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

**`SYNC_CHAPTERS`.** ffmpeg et ffprobe sont inclus dans l'image : rien à installer, il
suffit de passer `SYNC_CHAPTERS=true`. Voir la section dédiée plus bas.

**Cache d'état.** `config/state.json` mémorise une empreinte des métadonnées de chaque livre ;
une passe suivante ne réécrit que ce qui a changé dans ABS. Utilise `FORCE=true` après avoir
modifié la configuration des tags.

**Déplacements.** Commence toujours par `ORPHAN_ACTION=report` et `ON_INCOMPLETE=tag`. Ne
passe à `move` qu'une fois `a-traiter.json` relu et validé.

---

## Synchronisation des chapitres

ffmpeg et ffprobe sont **inclus dans l'image** (le `Dockerfile` vérifie leur présence à la
construction). Pour activer la fonction : `SYNC_CHAPTERS=true`. Au démarrage, le programme
affiche la version détectée, ou une erreur explicite si le binaire manque.

Les chapitres définis dans Audiobookshelf sont écrits dans le fichier via un remux en copie
de flux (`-c copy`) : aucun réencodage, donc rapide, et la qualité audio est intacte.

**Désactivé par défaut, volontairement.** Contrairement aux tags qui ne touchent que
l'atome `moov`, un remux **reconstruit tout le fichier**. Conséquences :

- Sur un m4b de 800 Mo, c'est 800 Mo lus et réécrits — quelques secondes en local, mais
  rien à voir avec l'écriture d'un tag.
- L'inode change, donc Audiobookshelf rescanne le fichier et **un lien physique existant
  est rompu**. Le programme le détecte à la passe suivante et rétablit le lien
  automatiquement.
- Ne s'applique qu'aux livres constitués d'un **seul fichier**.

Le programme ne remuxe que si nécessaire : les chapitres présents dans le fichier sont
d'abord lus avec ffprobe et comparés à ceux d'ABS (titres et positions à la seconde près).
Si tout concorde, ffmpeg n'est pas lancé du tout.

**Pochette embarquée.** Certaines pochettes MP4 refusent de se remuxer (mjpeg mal formé,
dimensions absentes) et faisaient échouer l'opération. Le programme tente d'abord un remux
complet ; en cas d'échec, il recommence sans le flux image, et la pochette est de toute
façon réécrite juste après par le tagger. Vérifié : après remux, `covr` est bien présent.


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
│   ├── triage.py        # livres non identifiés et fichiers orphelins
│   ├── naming.py        # moteur de gabarits et assainissement des noms
│   └── export.py        # export des livres validés
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
