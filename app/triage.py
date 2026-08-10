"""Triage des livres à traiter manuellement.

Deux situations distinctes :

1. **Livre présent dans Audiobookshelf mais non identifié** (pas d'ASIN, pas
   d'auteur, pas de pochette…). On ne veut pas graver ces métadonnées vides dans
   les fichiers. Selon la configuration : on pose un tag ABS pour pouvoir filtrer
   dans l'interface, et/ou on déplace le dossier hors de la bibliothèque.

2. **Fichier audio présent sur le disque mais absent d'Audiobookshelf**
   (jamais scanné, dossier mal nommé, extension inhabituelle…). On le signale,
   et éventuellement on le déplace dans un dossier de tri.
"""

import logging
import os
import shutil
import time

log = logging.getLogger("triage")

AUDIO_EXT = (".m4b", ".m4a", ".mp4", ".mp3", ".ogg", ".opus", ".flac", ".aac", ".wma")

# Libellés lisibles pour les journaux
FIELD_LABELS = {
    "title": "titre",
    "author": "auteur",
    "narrator": "narrateur",
    "cover": "pochette",
    "description": "résumé",
    "asin": "ASIN",
    "isbn": "ISBN",
    "series": "série",
    "year": "année",
    "publisher": "éditeur",
    "genres": "genres",
    "identifier": "ASIN ou ISBN",
}


# ------------------------------------------------------ 1. livres incomplets
def missing_fields(meta, item: dict, checks: list) -> list:
    """Retourne la liste des champs demandés qui sont vides."""
    media = item.get("media") or {}
    has = {
        "title": bool(meta.title),
        "author": bool(meta.authors),
        "narrator": bool(meta.narrators),
        "cover": bool(media.get("coverPath") or item.get("coverPath")),
        "description": bool(meta.description),
        "asin": bool(meta.asin),
        "isbn": bool(meta.isbn),
        "series": bool(meta.series),
        "year": bool(meta.year),
        "publisher": bool(meta.publisher),
        "genres": bool(meta.genres),
        "identifier": bool(meta.asin or meta.isbn),
    }
    return [f for f in checks if f in has and not has[f]]


def apply_abs_tag(client, item_id: str, current_tags: list, tag: str,
                  present: bool, dry_run: bool) -> bool:
    """Ajoute ou retire `tag` de l'item. Retourne True si l'appel a eu lieu."""
    tags = [t for t in (current_tags or [])]
    already = tag in tags
    if present and already:
        return False
    if not present and not already:
        return False
    if present:
        tags.append(tag)
    else:
        tags = [t for t in tags if t != tag]
    if dry_run:
        log.info("      [dry-run] tag ABS « %s » %s", tag, "ajouté" if present else "retiré")
        return False
    client.set_tags(item_id, tags)
    log.info("      tag ABS « %s » %s", tag, "ajouté" if present else "retiré")
    return True


# -------------------------------------------------------------- déplacements
def _unique_dest(dest: str) -> str:
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    for i in range(2, 1000):
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{base} ({int(time.time())}){ext}"


def _relative_root(path: str, roots: list) -> str:
    """Retourne la racine de bibliothèque qui contient `path`, sinon ''."""
    best = ""
    for root in roots:
        root = root.rstrip("/")
        if path == root or path.startswith(root + "/"):
            if len(root) > len(best):
                best = root
    return best


def move_aside(path: str, roots: list, unmatched_dir: str, dry_run: bool) -> str:
    """Déplace un fichier ou un dossier dans le dossier de tri en conservant
    l'arborescence relative. Retourne la destination, ou '' si rien n'a été fait."""
    if not os.path.exists(path):
        return ""
    if not unmatched_dir:
        log.warning("      UNMATCHED_DIR non défini : déplacement annulé")
        return ""
    root = _relative_root(path, roots)
    rel = os.path.relpath(path, root) if root else os.path.basename(path)
    dest = _unique_dest(os.path.join(unmatched_dir, rel))
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


# --------------------------------------------------- 2. fichiers orphelins
def scan_audio_files(roots: list, exclude: list = None) -> list:
    """Liste tous les fichiers audio sous les racines données."""
    exclude = [e.rstrip("/") for e in (exclude or []) if e]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            log.warning("Dossier de scan introuvable : %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if any(dirpath == e or dirpath.startswith(e + "/") for e in exclude):
                dirnames[:] = []
                continue
            for name in filenames:
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                    found.append(os.path.join(dirpath, name))
    return found


def find_orphans(roots: list, known_paths: set, excludes, min_age_seconds: int) -> list:
    """Fichiers audio du disque inconnus d'Audiobookshelf.

    `excludes` : dossier(s) à ne jamais parcourir (tri manuel, export…)."""
    if isinstance(excludes, str):
        excludes = [excludes]
    known = {os.path.normpath(p) for p in known_paths}
    now = time.time()
    orphans = []
    for path in scan_audio_files(roots, exclude=[e for e in (excludes or []) if e]):
        if os.path.normpath(path) in known:
            continue
        try:
            if now - os.path.getmtime(path) < min_age_seconds:
                log.debug("Orphelin trop récent, ignoré : %s", path)
                continue
        except OSError:
            continue
        orphans.append(path)
    return orphans


def group_orphans(orphans: list, roots: list) -> list:
    """Regroupe les orphelins par dossier livre, pour déplacer le dossier entier
    plutôt que fichier par fichier. Un dossier n'est déplacé que si TOUS ses
    fichiers audio sont orphelins."""
    by_dir = {}
    for path in orphans:
        by_dir.setdefault(os.path.dirname(path), []).append(path)

    targets = []
    for folder, files in sorted(by_dir.items()):
        if folder in [r.rstrip("/") for r in roots]:
            targets.extend(files)          # fichiers posés à la racine
            continue
        try:
            all_audio = [f for f in os.listdir(folder)
                         if os.path.splitext(f)[1].lower() in AUDIO_EXT
                         and not f.startswith(".")]
        except OSError:
            targets.extend(files)
            continue
        if len(all_audio) == len(files):
            targets.append(folder)         # dossier entièrement orphelin
        else:
            targets.extend(files)
    return targets
