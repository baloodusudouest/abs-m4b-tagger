"""Export des livres validés vers une bibliothèque structurée.

Seuls les livres jugés complets (voir triage.missing_fields) sont exportés.
Les fichiers audio sont copiés — après avoir été taggés — dans une arborescence
définie par gabarit, accompagnés de leurs fichiers annexes.
"""

import json
import logging
import os
import shutil

import naming
from tagger import AUDIO_EXT

log = logging.getLogger("export")

SIDECAR_CHOICES = ("cover", "metadata", "desc", "reader", "nfo")

# Fichiers considérés comme des résidus supprimables après un déplacement.
# Tout autre fichier fait renoncer à la suppression du dossier source.
RESIDUAL = {
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg",
    "metadata.json", "metadata.abs", "desc.txt", "reader.txt", "book.nfo",
    "desktop.ini", ".ds_store", "thumbs.db",
}
RESIDUAL_DIRS = {"@eadir", ".ab", "__macosx"}


# ------------------------------------------------------------------ transferts
_LINK_FALLBACK_WARNED = False


def _same_file(src: str, dst: str, action: str = "copy") -> bool:
    """Le transfert est-il déjà à jour ?

    En mode hardlink, « à jour » signifie *le même inode* : si un outil a réécrit
    le fichier via un temporaire (ffmpeg), le lien est rompu et il faut le refaire.
    """
    try:
        a, b = os.stat(src), os.stat(dst)
    except OSError:
        return False
    if action == "hardlink":
        return a.st_dev == b.st_dev and a.st_ino == b.st_ino
    return a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) < 2


def links_match(src: str, dst: str) -> bool:
    """Les deux chemins désignent-ils toujours le même inode ?"""
    return _same_file(src, dst, "hardlink")


def same_device(path_a: str, path_b: str) -> bool:
    """Les deux chemins sont-ils sur le même système de fichiers ?"""
    def dev(p):
        while p and not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                return None
            p = parent
        try:
            return os.stat(p).st_dev
        except OSError:
            return None
    da, db = dev(path_a), dev(path_b)
    return da is not None and da == db


def transfer(src: str, dst: str, action: str, overwrite: bool,
             dry_run: bool) -> str:
    """Copie / déplace / lie un fichier. Retourne 'copie', 'ignore' ou ''."""
    if os.path.exists(dst) and not overwrite and _same_file(src, dst, action):
        return "ignore"
    if dry_run:
        log.debug("      [dry-run] %s -> %s", action, dst)
        return "copie"

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        if action == "hardlink":
            try:
                os.link(src, tmp)
            except OSError as e:      # volumes différents
                global _LINK_FALLBACK_WARNED
                if not _LINK_FALLBACK_WARNED:
                    log.warning("Lien physique impossible (%s) : la source et la "
                                "destination ne sont pas sur le même volume. "
                                "Bascule sur une COPIE, l'espace disque sera doublé.", e)
                    _LINK_FALLBACK_WARNED = True
                shutil.copy2(src, tmp)
        elif action == "symlink":
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
            return "copie"
        elif action == "move":
            shutil.move(src, tmp)
        else:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return "copie"
    except OSError as e:
        log.error("      transfert impossible vers %s : %s", dst, e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return ""


def _write(path: str, data, dry_run: bool) -> None:
    if dry_run:
        log.debug("      [dry-run] annexe %s", os.path.basename(path))
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
        kwargs = {} if mode == "wb" else {"encoding": "utf-8"}
        with open(path, mode, **kwargs) as fh:
            fh.write(data)
    except OSError as e:
        log.warning("      annexe %s impossible : %s", os.path.basename(path), e)


# ------------------------------------------------------------------- annexes
def abs_metadata_json(meta) -> str:
    """Export au format metadata.json d'Audiobookshelf (réimportable)."""
    payload = {
        "tags": meta.tags,
        "chapters": meta.chapters,
        "metadata": {
            "title": meta.title or None,
            "subtitle": meta.subtitle or None,
            "authors": meta.authors,
            "narrators": meta.narrators,
            "series": ([f"{meta.series} #{meta.series_part}"] if meta.series_part
                       else [meta.series]) if meta.series else [],
            "genres": meta.genres,
            "publishedYear": meta.year or None,
            "publishedDate": meta.published_date or None,
            "publisher": meta.publisher or None,
            "description": meta.description or None,
            "isbn": meta.isbn or None,
            "asin": meta.asin or None,
            "language": meta.language or None,
            "explicit": meta.explicit,
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def book_nfo(meta) -> str:
    from xml.sax.saxutils import escape as esc
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<book>"]
    def tag(name, value):
        if value:
            lines.append(f"  <{name}>{esc(str(value))}</{name}>")
    tag("title", meta.title)
    tag("subtitle", meta.subtitle)
    for a in meta.authors:
        tag("author", a)
    for n in meta.narrators:
        tag("narrator", n)
    tag("series", meta.series)
    tag("seriespart", meta.series_part)
    tag("year", meta.year)
    tag("publisher", meta.publisher)
    tag("isbn", meta.isbn)
    tag("asin", meta.asin)
    tag("language", meta.language)
    for g in meta.genres:
        tag("genre", g)
    tag("plot", meta.description)
    lines.append("</book>")
    return "\n".join(lines) + "\n"


def write_sidecars(folder: str, meta, cover_bytes: bytes, cover_mime: str,
                   wanted: list, dry_run: bool) -> None:
    if "cover" in wanted and cover_bytes:
        ext = ".png" if "png" in (cover_mime or "") else ".jpg"
        _write(os.path.join(folder, f"cover{ext}"), cover_bytes, dry_run)
    if "metadata" in wanted:
        _write(os.path.join(folder, "metadata.json"), abs_metadata_json(meta), dry_run)
    if "desc" in wanted and meta.description:
        _write(os.path.join(folder, "desc.txt"), meta.description, dry_run)
    if "reader" in wanted and meta.narrator:
        _write(os.path.join(folder, "reader.txt"), meta.narrator, dry_run)
    if "nfo" in wanted:
        _write(os.path.join(folder, "book.nfo"), book_nfo(meta), dry_run)


# ------------------------------------------------------------------- ménage
def prune_empty(folder: str, root: str) -> None:
    """Supprime les dossiers vides jusqu'à la racine d'export."""
    root = os.path.normpath(root)
    folder = os.path.normpath(folder)
    while folder.startswith(root + os.sep) and folder != root:
        try:
            if os.listdir(folder):
                return
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def relocate_previous(old_rel: str, new_rel: str, root: str, dry_run: bool) -> None:
    """Le nommage a changé : on déplace l'ancien dossier au lieu d'en créer un
    doublon."""
    if not old_rel or old_rel == new_rel:
        return
    old = os.path.join(root, old_rel)
    new = os.path.join(root, new_rel)
    if not os.path.isdir(old):
        return
    if dry_run:
        log.info("      [dry-run] renommage export : %s -> %s", old_rel, new_rel)
        return
    try:
        if os.path.exists(new):
            shutil.rmtree(old)
            log.info("      ancien export supprimé : %s", old_rel)
        else:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.move(old, new)
            log.info("      export renommé : %s -> %s", old_rel, new_rel)
        prune_empty(os.path.dirname(old), root)
    except OSError as e:
        log.warning("      renommage de l'export impossible : %s", e)


def cleanup_source(folder: str, roots: list, dry_run: bool) -> None:
    """Supprime un dossier source vidé de ses fichiers audio.

    Ne s'exécute que si le dossier ne contient plus que des résidus connus
    (cover.jpg, metadata.json, @eaDir…). Tout fichier inattendu annule."""
    folder = os.path.normpath(folder)
    root = ""
    for r in roots:
        r = os.path.normpath(r)
        if folder.startswith(r + os.sep) and len(r) > len(root):
            root = r
    if not root or folder == root:
        return
    try:
        entries = os.listdir(folder)
    except OSError:
        return

    for name in entries:
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            if name.lower() not in RESIDUAL_DIRS:
                log.info("      dossier source conservé (contient %s) : %s", name, folder)
                return
        elif name.lower() not in RESIDUAL:
            log.info("      dossier source conservé (contient %s) : %s", name, folder)
            return

    if dry_run:
        log.info("      [dry-run] suppression du dossier source %s", folder)
        return
    try:
        shutil.rmtree(folder)
        log.info("      dossier source supprimé : %s", folder)
        prune_empty(os.path.dirname(folder), root)
    except OSError as e:
        log.warning("      suppression du dossier source impossible : %s", e)


# -------------------------------------------------------------------- export
def expected_rel(cfg, meta) -> str:
    """Chemin relatif que ce livre aura dans la bibliothèque exportée."""
    region = naming.region_for(meta, cfg.export_region)
    values = naming.build_vars(meta, region, 1, max(1, len(meta.audio_files)))
    return naming.render_path(cfg.export_dir_template, values,
                              cfg.export_max_component, cfg.export_collapse_spaces)


def export_item(cfg, meta, files: list, cover_bytes: bytes, cover_mime: str,
                previous_rel: str = "") -> dict:
    """Exporte un livre validé. `files` = [(chemin_local, index)].

    Retourne {"rel": chemin relatif, "copies": n, "ignores": n}.
    """
    root = cfg.export_dir
    region = naming.region_for(meta, cfg.export_region)
    total = len(files)

    rel_dir = expected_rel(cfg, meta)

    relocate_previous(previous_rel, rel_dir, root, cfg.dry_run)
    dest_dir = os.path.join(root, rel_dir)

    # Si le dossier d'export vient d'être renommé, les fichiers déjà exportés
    # ont suivi : on recale leurs chemins avant tout transfert.
    if previous_rel and previous_rel != rel_dir:
        old_dir = os.path.normpath(os.path.join(root, previous_rel))
        remapped = []
        for path, index in files:
            norm = os.path.normpath(path)
            if norm == old_dir or norm.startswith(old_dir + os.sep):
                path = os.path.join(dest_dir, os.path.relpath(norm, old_dir))
            remapped.append((path, index))
        files = remapped

    file_tpl = cfg.export_file_template
    if total > 1 and "{piste" not in file_tpl:
        file_tpl = file_tpl + " - {piste2}"

    copies = ignores = 0
    rel_files, sources = [], set()
    for path, index in files:
        ext = os.path.splitext(path)[1]
        fvars = naming.build_vars(meta, region, index, total, ext)
        name = naming.render_filename(file_tpl, fvars, ext,
                                      cfg.export_max_component,
                                      cfg.export_collapse_spaces)
        dst = os.path.join(dest_dir, name)
        if cfg.export_max_path and len(dst) > cfg.export_max_path:
            log.warning("      chemin de %d caractères, au-delà de la limite Windows : %s",
                        len(dst), dst)
        inside = os.path.normpath(path).startswith(os.path.normpath(root) + os.sep)
        result = transfer(path, dst, cfg.export_action,
                          cfg.export_overwrite, cfg.dry_run)
        if result:
            rel_files.append(os.path.relpath(dst, root))
        if result == "copie":
            copies += 1
        elif result == "ignore":
            ignores += 1
        if cfg.export_action == "move" and result == "copie" and not inside:
            sources.add(os.path.dirname(path))

    # Un renommage peut laisser l'ancien fichier sur place (le lien physique est
    # créé au nouveau nom sans supprimer l'ancien) : on purge ce qui n'a pas été
    # écrit lors de ce passage.
    if cfg.export_prune_stale and rel_files and not cfg.dry_run:
        expected = {os.path.basename(r) for r in rel_files}
        try:
            for name in os.listdir(dest_dir):
                path = os.path.join(dest_dir, name)
                if (os.path.isfile(path) and name not in expected
                        and os.path.splitext(name)[1].lower() in AUDIO_EXT):
                    os.remove(path)
                    log.info("      fichier obsolète supprimé : %s", name)
        except OSError as e:
            log.debug("      purge impossible dans %s : %s", dest_dir, e)

    if copies or not os.path.isdir(dest_dir):
        write_sidecars(dest_dir, meta, cover_bytes, cover_mime,
                       cfg.export_sidecars, cfg.dry_run)

    if copies:
        verbe = "déplacé" if cfg.export_action == "move" else "exporté"
        log.info("      %s vers %s (%d fichier%s)", verbe, rel_dir, copies,
                 "s" if copies > 1 else "")

    if cfg.export_action == "move" and cfg.move_cleanup_source:
        for folder in sorted(sources):
            cleanup_source(folder, cfg.library_roots, cfg.dry_run)

    return {"rel": rel_dir, "files": rel_files,
            "copies": copies, "ignores": ignores}
