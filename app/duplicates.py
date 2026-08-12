"""Détection des doublons dans la bibliothèque Audiobookshelf.

Deux items ABS différents qui partagent le même identifiant (ASIN, ISBN…)
désignent le même livre. C'est fréquent après une réorganisation de dossiers,
un import en double, ou quand la même édition a été récupérée deux fois sous
des noms de dossiers différents.

Le cas est particulièrement gênant avec l'export : le gabarit contenant
``{asin}``, les deux items produisent exactement le même chemin cible et se
recouvrent silencieusement — le second écrase le premier, ou pire, en mode
lien physique, les deux pointent vers des inodes différents selon l'ordre de
passage.

Ce module se contente d'observer pendant la passe (aucun accès disque
supplémentaire), puis, une fois tous les items connus, regroupe et signale.
Avec DUPLICATE_ACTION=move, **toutes** les copies d'un même livre sont
déplacées dans un dossier dédié : l'objectif est de pouvoir les comparer
côte à côte et de choisir soi-même laquelle conserver.
"""

import logging
import os

from triage import AUDIO_EXT, _unique_dest, _relative_root

log = logging.getLogger("duplicates")

# Clés de regroupement acceptées dans DUPLICATE_KEYS
KEY_CHOICES = ("asin", "isbn", "titre")


# --------------------------------------------------------------- collecte
def register(store: dict, cfg, meta) -> None:
    """Mémorise un item pour l'analyse de fin de passe.

    Appelé pour *tous* les items possédant des fichiers audio, y compris ceux
    que la passe saute (cache d'état) : sinon les doublons disparaîtraient du
    radar dès la deuxième exécution.
    """
    if cfg.duplicate_action == "none":
        return
    store[meta.id] = {
        "id": meta.id,
        "titre": meta.title,
        "auteur": ", ".join(meta.authors),
        "asin": meta.asin.strip().upper(),
        "isbn": meta.isbn.strip().replace("-", ""),
        "annee": meta.year,
        "chemin_abs": meta.path,
        "chemin_local": cfg.map_path(meta.path),
        "fichiers": [cfg.map_path(af["path"]) for af in meta.audio_files],
        "duree_s": int(sum(af.get("duration") or 0 for af in meta.audio_files)),
        "maj_abs": meta.updated_at,
    }


# ------------------------------------------------------------ regroupement
def _key_value(entry: dict, key: str) -> str:
    if key == "asin":
        return entry["asin"]
    if key == "isbn":
        return entry["isbn"]
    if key == "titre":
        titre = (entry["titre"] or "").strip().lower()
        auteur = (entry["auteur"] or "").strip().lower()
        return f"{auteur}|{titre}" if titre else ""
    return ""


def _human_size(nb: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if nb < 1024 or unit == "Go":
            return f"{nb:.0f} {unit}" if unit == "o" else f"{nb:.1f} {unit}"
        nb /= 1024.0
    return f"{nb:.1f} Go"


def _human_duration(seconds: int) -> str:
    if not seconds:
        return "?"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _measure(entry: dict) -> None:
    """Ajoute taille et formats — uniquement pour les items en doublon."""
    total, formats, manquants = 0, set(), 0
    for path in entry["fichiers"]:
        try:
            total += os.path.getsize(path)
        except OSError:
            manquants += 1
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext:
            formats.add(ext.lstrip("."))
    entry["taille_octets"] = total
    entry["taille"] = _human_size(total)
    entry["duree"] = _human_duration(entry["duree_s"])
    entry["formats"] = sorted(formats)
    entry["fichiers_manquants"] = manquants
    entry["nb_fichiers"] = len(entry["fichiers"])


def find_groups(store: dict, keys: list) -> list:
    """Regroupe les items partageant une même clé.

    Un item déjà rattaché à un groupe par une clé prioritaire n'est pas
    réexaminé par les clés suivantes : l'ASIN prime sur le titre.
    """
    groups, claimed = [], set()
    for key in keys:
        buckets = {}
        for item_id, entry in store.items():
            if item_id in claimed:
                continue
            value = _key_value(entry, key)
            if value:
                buckets.setdefault(value, []).append(entry)
        for value, entries in buckets.items():
            if len(entries) < 2:
                continue
            for entry in entries:
                claimed.add(entry["id"])
                _measure(entry)
            groups.append({
                "cle": key,
                "valeur": value,
                "titre": entries[0]["titre"],
                "copies": sorted(entries, key=lambda e: -e["taille_octets"]),
            })
    groups.sort(key=lambda g: (g["cle"], g["valeur"]))
    return groups


# ------------------------------------------------------------- déplacement
def _targets_for(entry: dict, roots: list) -> list:
    """Que déplacer pour cet item : son dossier, ou ses fichiers isolés.

    Le dossier n'est déplacé que s'il contient exactement les fichiers audio de
    l'item — sinon deux livres cohabitent dans le même dossier et on ne
    déplacerait pas seulement le doublon.
    """
    folder = entry["chemin_local"]
    existing = [f for f in entry["fichiers"] if os.path.isfile(f)]
    if not existing:
        return []
    if not os.path.isdir(folder):
        return existing
    if folder.rstrip("/") in [r.rstrip("/") for r in roots]:
        return existing                      # fichiers posés à la racine
    try:
        sur_disque = [f for f in os.listdir(folder)
                      if os.path.splitext(f)[1].lower() in AUDIO_EXT
                      and not f.startswith(".")]
    except OSError:
        return existing
    if len(sur_disque) != len(existing):
        return existing
    if any(os.path.dirname(f).rstrip("/") != folder.rstrip("/") for f in existing):
        return existing
    return [folder]


def _move(path: str, dest_dir: str, roots: list, dry_run: bool) -> str:
    import shutil
    root = _relative_root(path, roots)
    rel = os.path.relpath(path, root) if root else os.path.basename(path)
    dest = _unique_dest(os.path.join(dest_dir, rel))
    if dry_run:
        log.info("      [dry-run] déplacement vers %s", dest)
        return dest
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)
        log.info("      déplacé vers %s", dest)
        return dest
    except OSError as e:
        log.error("      déplacement impossible (%s) : %s", path, e)
        return ""


def move_group(cfg, group: dict, roots: list) -> int:
    """Déplace toutes les copies du groupe sous DUPLICATE_DIR/<clé>-<valeur>/."""
    libelle = f"{group['cle']}-{group['valeur']}".replace("/", "_")[:120]
    dest_dir = os.path.join(cfg.duplicate_dir, libelle)

    # Toutes les décisions sont prises sur l'état initial du disque : sinon le
    # premier déplacement « purifie » un dossier partagé et la copie suivante
    # emporterait le dossier entier au lieu de ses seuls fichiers.
    plan = [(entry, _targets_for(entry, roots)) for entry in group["copies"]]

    deplaces, vus = 0, set()
    for entry, targets in plan:
        for target in targets:
            reel = os.path.realpath(target)
            if reel in vus:                  # deux items ABS, un seul dossier
                log.info("      %s déjà déplacé (items ABS multiples sur le "
                         "même dossier)", os.path.basename(target))
                continue
            vus.add(reel)
            sous_dossier = os.path.join(dest_dir, entry["id"][:8])
            if _move(target, sous_dossier, roots, cfg.dry_run):
                deplaces += 1
                entry["deplace_vers"] = sous_dossier
    return deplaces


# ------------------------------------------------------------------ rapport
def handle_duplicates(cfg, store: dict, report: list) -> int:
    """Signale — et éventuellement isole — les doublons. Retourne leur nombre."""
    if cfg.duplicate_action == "none" or not store:
        return 0

    groups = find_groups(store, cfg.duplicate_keys)
    if not groups:
        log.info("Aucun doublon détecté (clés : %s).", ", ".join(cfg.duplicate_keys))
        return 0

    total = sum(len(g["copies"]) for g in groups)
    log.warning("%d livre(s) en doublon, soit %d copie(s) :", len(groups), total)

    for group in groups:
        log.warning("  %s = %s — « %s »", group["cle"].upper(), group["valeur"],
                    group["titre"] or "?")
        for entry in group["copies"]:
            alerte = ""
            if entry["fichiers_manquants"]:
                alerte = f"  /!\\ {entry['fichiers_manquants']} fichier(s) absent(s) du disque"
            log.warning("    - %s  [%s, %s, %d fichier(s), %s]%s",
                        entry["chemin_local"], entry["taille"], entry["duree"],
                        entry["nb_fichiers"],
                        "/".join(entry["formats"]) or "?", alerte)
        report.append({
            "doublon": group["valeur"],
            "cle": group["cle"],
            "titre": group["titre"],
            "copies": [{
                "id": e["id"],
                "chemin": e["chemin_local"],
                "taille": e["taille"],
                "duree": e["duree"],
                "nb_fichiers": e["nb_fichiers"],
                "formats": e["formats"],
            } for e in group["copies"]],
        })

    if cfg.duplicate_action == "move":
        roots = cfg.library_roots
        log.warning("  Isolement dans %s — Audiobookshelf marquera ces items "
                    "« manquants » au prochain scan.", cfg.duplicate_dir)
        for group in groups:
            move_group(cfg, group, roots)
    else:
        log.warning("  (DUPLICATE_ACTION=report : rien n'a été déplacé)")

    return len(groups)
