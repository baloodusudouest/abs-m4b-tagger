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

### Erreur « failed to read dockerfile »

```
compose build operation failed: failed to solve: failed to read dockerfile:
open /volume1/@docker/tmp/buildkit-mount…/Dockerfile: no such file or directory
```

Ce message signifie que le stack a été créé via **Web editor**. Ce mode n'envoie que le
YAML collé : il n'existe aucun contexte de build, donc `build: .` ne trouve pas de
`Dockerfile`. Trois solutions.

**1. Méthode Repository (recommandée).** Portainer clone le dépôt, le `Dockerfile` est donc
présent. `Stacks → Add stack → Repository`, Compose path `docker-compose.yml`.

**2. Image GHCR.** Après le premier passage de GitHub Actions, utilise
`docker-compose.ghcr.yml` avec `IMAGE=ghcr.io/<compte>/abs-m4b-tagger:latest`. Pense à
rendre le package public, sinon Portainer devra s'authentifier.

**3. Construction manuelle puis Web editor.** En SSH :

```bash
cd /volume1/docker/abs-tagger
docker build -t abs-m4b-tagger:local .
```

puis colle `docker-compose.webeditor.yml` dans l'éditeur web (il référence l'image sans la
construire).

### Pièges de syntaxe dans l'éditeur web

- **Toujours guillemeter les valeurs de `environment`.** `ABS_VERIFY_SSL: true` est un
  booléen YAML, pas une chaîne, et Compose peut le rejeter. Écrire `"true"`.
- Une clé vide (`ABS_LIBRARIES:`) vaut `null` et non `""` : écrire `ABS_LIBRARIES: ""`.
- `user: 1026:101` doit être guillemeté : `user: "1026:101"`.
- Vérifier les accolades parasites dans les volumes : `…/config}:/config` crée un dossier
  nommé `config}`.


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

### C. Le même livre est présent deux fois

Deux items Audiobookshelf qui partagent le même ASIN désignent le même livre : import en
double, dossier dupliqué lors d'une réorganisation, ou même édition récupérée deux fois sous
deux noms différents. C'est surtout gênant avec l'export, dont le gabarit contient `{asin}` :
les deux copies produisent exactement le même chemin cible et se recouvrent silencieusement.

| `DUPLICATE_ACTION` | Effet |
|---|---|
| `report` *(défaut)* | Liste les groupes dans les logs et dans `/config/a-traiter.json`. |
| `move` | Isole **toutes** les copies dans `DUPLICATE_DIR`, une par sous-dossier. |
| `none` | Désactive la détection. |

`DUPLICATE_KEYS` définit ce qui fait doublon, dans l'ordre de priorité : `asin` (défaut),
`isbn`, ou `titre` (couple auteur + titre normalisé, utile pour les livres sans identifiant,
mais nettement plus sujet aux faux positifs). Un item déjà rattaché à un groupe n'est pas
réexaminé par les clés suivantes.

Contrairement au triage des orphelins, `move` déplace **les deux** copies, pas seulement la
seconde : l'outil n'a aucun moyen fiable de désigner la bonne. Le but est de te les présenter
côte à côte pour que tu tranches. Chaque groupe donne :

```
DUPLICATE_DIR/asin-B0BPTHK8SX/
├── 3f9a2c11/…/Un soupçon de haine/          <- copie 1, dossier d'origine préservé
└── 7d1e4b08/…/Un soupcon de haine (copie)/  <- copie 2
```

Le log affiche taille, durée, nombre de fichiers et formats de chaque copie pour te permettre
de choisir sans ouvrir les dossiers :

```
WARNING 1 livre(s) en doublon, soit 2 copie(s) :
WARNING   ASIN = B0BPTHK8SX — « Un soupçon de haine »
WARNING     - /media/…/Joe Abercrombie/…  [412.7 Mo, 14h22, 1 fichier(s), m4b]
WARNING     - /media/…/Abercrombie, Joe/…  [198.3 Mo, 14h20, 24 fichier(s), mp3]
```

Garde-fous appliqués :

- Le dossier d'un livre n'est déplacé en bloc que s'il ne contient **que** les fichiers de cet
  item ; sinon seuls les fichiers concernés partent. Les décisions sont toutes prises sur
  l'état initial du disque, avant le premier déplacement.
- Deux items ABS pointant sur le **même** dossier ne le déplacent qu'une fois.
- La détection tourne sur les items réellement inventoriés pendant la passe, y compris ceux
  que le cache d'état fait sauter — un doublon reste visible à la deuxième exécution.
- `DUPLICATE_DIR` doit être hors de la bibliothèque ABS, et il est exclu de la détection des
  orphelins.

Après un déplacement, ABS marque les deux items comme manquants. Une fois ton choix fait,
remets le dossier retenu dans la bibliothèque et purge le reste via
`Paramètres → Bibliothèques → Supprimer les éléments manquants`.

Pour un audit seul, sans rien écrire ni déplacer :

```bash
sudo docker exec abs-m4b-tagger python /app/main.py --duplicates-only
```

Ce mode force `DRY_RUN`, ignore le cache d'état et désactive tags, export et triage.

### D. Le livre n'est pas celui que dit la fiche

Audiobookshelf associe parfois la mauvaise fiche Audible à un livre : deux tomes
d'une même série, une réédition, voire deux romans sans rapport. Les tags écrits dans les
fichiers sont alors faux, et l'export les range sous un titre qui n'est pas le leur.

La durée tranche. Celle du fichier est mesurée par ABS au scan, dans le fichier lui-même —
c'est un fait, pas une métadonnée. Elle est comparée à la durée annoncée par le fournisseur
pour l'ASIN de l'item, via l'endpoint que l'onglet « Chercher » d'ABS utilise déjà :

```
GET /api/search/books?title=<ASIN>&provider=audible.fr   ->  "duration": 667   (minutes)
```

C'est ABS qui relaie la requête : aucune clé Audible à fournir, le token ABS suffit.

| Variable | Défaut | Rôle |
|---|---|---|
| `VERIFY_ACTION` | `report` | `report` ou `none` |
| `VERIFY_PROVIDER` | `audible.fr` | Fournisseur interrogé |
| `VERIFY_TOLERANCE_PCT` | `10` | Écart relatif toléré, en pourcent |
| `VERIFY_MIN_ECART_MIN` | `5` | Plancher absolu, en minutes |
| `VERIFY_DELAY_MS` | `400` | Pause entre deux requêtes sortantes |
| `VERIFY_MAX_PER_PASS` | `0` | Plafond par passe (0 = sans limite) |
| `VERIFY_RETRY_DAYS` | `30` | Délai avant de réessayer un « introuvable » |
| `VERIFY_ACCEPT_TAG` | `duree-ok` | Tag ABS validant un écart connu (vide = désactivé) |

Cinq verdicts : `conforme`, `durée incohérente`, `écart validé manuellement`,
`absent du fournisseur`, `sans ASIN`.

#### Valider un écart normal

Un écart n'est pas toujours une erreur : silences resserrés, jingle d'éditeur absent, version
remasterisée. Après avoir contrôlé le livre, pose le tag **`duree-ok`** sur l'item dans
Audiobookshelf (`Éditer → Tags`). Il passe en `écart validé manuellement` et cesse d'être
signalé, sans que la tolérance générale ait à être relâchée pour toute la bibliothèque.

La validation est enregistrée dans `/config/validations.json` avec l'ASIN et la durée du
fichier **au moment où tu l'as posée**. Elle est automatiquement annulée si l'un des deux
change — item réidentifié sur une autre fiche, fichier remplacé — car ce n'est plus le même
objet que celui que tu as contrôlé. Le log te le dit alors explicitement, et la validation
n'est PAS renouvelée toute seule : il faut retirer puis reposer le tag. Retirer le tag suffit
par ailleurs à révoquer la validation.

Ce tag doit être différent de `INCOMPLETE_TAG`, qui est posé et retiré automatiquement.

**Les deux seuils doivent être dépassés** pour qu'un écart soit signalé. Un pourcentage seul
est structurellement injuste envers les livres courts : deux minutes de jingle d'éditeur
pèsent 4 % sur un livre d'une heure, mais 0,2 % sur vingt heures. Le plancher
`VERIFY_MIN_ECART_MIN` neutralise ce bruit.

**Le cache ne contient que des mesures, jamais des verdicts.** Le classement est recalculé à
chaque passe à partir des durées stockées : ajuster `VERIFY_TOLERANCE_PCT` reclasse toute la
bibliothèque instantanément, sans une seule requête sortante. Un écart détecté il y a
plusieurs passes reste par ailleurs signalé tant qu'il n'est pas corrigé.

**Le résultat est mis en cache** dans `/config/verifications.json`. La première passe
contrôle toute la bibliothèque ; les suivantes ne reprennent un livre que si son ASIN a
changé, si la durée du fichier a bougé, ou s'il était introuvable il y a plus de
`VERIFY_RETRY_DAYS` jours. Sur une bibliothèque stable, les passes suivantes ne déclenchent
aucune requête sortante.

Chaque livre représente une requête vers le fournisseur : sur 6 000 titres, compter environ
une heure avec le délai par défaut. `VERIFY_MAX_PER_PASS` permet d'étaler ce premier
contrôle sur plusieurs passes plutôt que de tout faire d'un coup.

#### Effet sur les doublons

Le contrôle des durées alimente directement le classement des groupes de doublons :

| Classe | Condition | Déplaçable |
|---|---|---|
| **doublon confirmé** | toutes les copies collent à la durée de la fiche | oui |
| **ASIN erroné** | une seule copie colle : les autres sont mal identifiées | non |
| **à vérifier** | aucune information exploitable | non par défaut |

C'est la distinction essentielle : un même ASIN sur deux livres différents n'est pas un
doublon, c'est une erreur d'identification. Déplacer un tel groupe sortirait de la
bibliothèque deux livres légitimes. Seuls les groupes confirmés sont donc isolés par
`DUPLICATE_ACTION=move` ; les autres sont signalés pour correction dans ABS.
`DUPLICATE_MOVE_UNVERIFIED=true` autorise en plus les groupes indéterminés.

## Interface web de revue

Les passes produisent des constats ; l'interface sert à les traiter. Elle tourne dans le même
conteneur, sur un thread séparé, et s'ouvre sur `http://<ip-du-nas>:8681`.

Quatre files de travail, alimentées par `/config` et par l'API ABS en direct :

| File | Actions disponibles |
|---|---|
| **Écarts de durée** | Valider l'écart (pose le tag `duree-ok` dans ABS), ou réidentifier le livre |
| **Doublons** | Choisir la copie à garder, isoler les autres dans `DUPLICATE_DIR` |
| **Non identifiés** | Chercher une fiche chez le fournisseur et l'appliquer |
| **Orphelins** | Mettre le fichier de côté dans `UNMATCHED_DIR` |

La réidentification affiche les résultats du fournisseur avec **leur durée**, ce qui permet de
choisir la bonne fiche du premier coup. Appliquer une fiche écrit les métadonnées et la
pochette dans ABS, puis **purge la mesure et la validation** de ce livre : la passe suivante
le recontrôlera sur sa nouvelle identité.

| Variable | Défaut | Rôle |
|---|---|---|
| `WEB_ENABLE` | `true` | Démarre l'interface |
| `WEB_PORT` | `8681` | Port d'écoute (à publier dans le compose) |
| `WEB_HOST` | `0.0.0.0` | Interface d'écoute |

**L'interface n'a aucune authentification.** Elle peut modifier la base Audiobookshelf et
déplacer des fichiers : réserve-la au réseau local ou à Tailscale, et ne la publie pas sur
Internet sans placer une authentification devant.

Les écritures de l'interface et celles de la passe de fond partagent un verrou : elles ne
peuvent pas agir en même temps sur le disque. Quand `DRY_RUN=true`, les déplacements demandés
depuis l'interface sont simulés, comme pour la passe.

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
DUPLICATE_ACTION=report
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



### Nettoyage de la liste d'auteurs

Les métadonnées Audible françaises créditent régulièrement traducteurs et narrateurs dans
le champ *auteurs*, et **l'ordre varie d'un livre à l'autre**. Sans traitement, deux tomes
d'une même série atterrissent dans deux dossiers distincts :

```
Lucinda Riley, Marie-Axelle de la Rochefoucauld - traducteur/Les sept sœurs/…
Marie-Axelle de la Rochefoucauld - traductrice, Lucinda Riley/Les sept sœurs/…
```

Trois traitements sont appliqués avant tout nommage, dans cet ordre :

1. `AUTHOR_EXCLUDE` retire les noms contenant une mention de rôle (`traducteur`,
   `narrateur`, `lu par`, `préface`…). La liste est une suite de motifs séparés par des
   virgules, recherchés sans tenir compte de la casse.
2. `AUTHOR_DROP_NARRATORS` retire les personnes déjà présentes dans la liste des
   narrateurs — utile quand le rôle n'est pas mentionné dans le nom.
3. `AUTHOR_SORT` trie ce qui reste, ce qui garantit un dossier identique quel que soit
   l'ordre renvoyé par l'API.

Filet de sécurité : si les règles supprimaient **tous** les auteurs, la liste d'origine est
conservée intacte. Les co-auteurs légitimes (`Christopher Golden, Dirk Maggs`) sont
préservés. Le nettoyage vaut aussi pour les tags `aART` et `©ART` écrits dans les fichiers.

### Longueur des chemins

Un titre long produit vite un chemin de plus de 255 caractères, inaccessible depuis Windows
via SMB. `EXPORT_MAX_PATH` (défaut 255) déclenche un avertissement dans le journal sans
rien tronquer. Pour raccourcir, réduis `EXPORT_MAX_COMPONENT` (défaut 180) ou allège le
gabarit, par exemple en retirant `<{serie} {serie_num}]>` du nom de dossier.

### Grandes bibliothèques

Avec `TRUST_UPDATED_AT=true` (défaut), un livre dont l'horodatage `updatedAt` n'a pas bougé
côté Audiobookshelf est écarté **avant** le téléchargement de sa pochette. Sur plusieurs
milliers de livres, cela supprime autant de requêtes HTTP par passe. Mettre `false` force
la vérification complète par empreinte à chaque fois.


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
| `DUPLICATE_ACTION` | `report` | `report`, `move` ou `none` |
| `DUPLICATE_KEYS` | `asin` | Clés de regroupement : `asin`, `isbn`, `titre` |
| `DUPLICATE_DIR` | `/a-trier/doublons` | Dossier d'isolement des doublons, hors bibliothèque |
| `DUPLICATE_MOVE_UNVERIFIED` | `false` | Déplacer aussi les groupes non vérifiés |
| `VERIFY_ACTION` | `report` | `report` ou `none` |
| `VERIFY_PROVIDER` | `audible.fr` | Fournisseur interrogé via ABS |
| `VERIFY_TOLERANCE_PCT` | `10` | Écart de durée toléré, en pourcent |
| `VERIFY_MIN_ECART_MIN` | `5` | Plancher absolu sous lequel rien n'est signalé |
| `VERIFY_DELAY_MS` | `400` | Pause entre deux requêtes sortantes |
| `VERIFY_MAX_PER_PASS` | `0` | Plafond de vérifications par passe |
| `VERIFY_RETRY_DAYS` | `30` | Délai avant de réessayer un « introuvable » |
| `VERIFY_ACCEPT_TAG` | `duree-ok` | Tag ABS validant un écart connu (vide = désactivé) |

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
| `EXPORT_MAX_PATH` | `255` | Avertir au-delà de cette longueur totale de chemin |
| `AUTHOR_EXCLUDE` | voir ci-dessus | Motifs écartant un faux auteur (traducteur, narrateur…) |
| `AUTHOR_DROP_NARRATORS` | `true` | Retirer des auteurs les personnes listées comme narrateurs |
| `AUTHOR_SORT` | `true` | Trier les auteurs pour un nommage stable |
| `TRUST_UPDATED_AT` | `true` | Sauter un livre dont `updatedAt` n'a pas changé |

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
├── docker-compose.yml            # Portainer « Repository », build depuis le dépôt
├── docker-compose.ghcr.yml       # Portainer, image pré-construite (GHCR)
├── docker-compose.webeditor.yml  # Portainer « Web editor », image déjà présente
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
