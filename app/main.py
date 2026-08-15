#!/usr/bin/env python3
"""abs-m4b-tagger — écrit les métadonnées Audiobookshelf dans les fichiers m4b/mp3.

Lit l'API Audiobookshelf, puis écrit directement les tags dans les fichiers
présents sur le NAS (aucun téléchargement, contrairement à Mp3tag).

Assure aussi le triage :
  - livres présents dans ABS mais non identifiés  -> tag ABS et/ou déplacement
  - fichiers audio présents sur disque mais absents d'ABS -> signalement/déplacement
"""

import argparse
import json
import logging
import os
import signal
import sys
import time

from config import Config
from absclient import AbsClient, AbsError
from tagger import AUDIO_EXT, BookMeta, prepare_cover, write_mp3, write_mp4
import chapters as chapmod
import triage
import duplicates as dupmod
import verify as verifymod
import runstate
import web as webmod
import export as exportmod

log = logging.getLogger("main")
_STOP = False


def _handle_signal(signum, frame):
    global _STOP
    _STOP = True
    log.info("Signal %s reçu, arrêt à la fin de l'item en cours…", signum)


# ------------------------------------------------------------------- état
def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        log.warning("Impossible de lire l'état %s : %s", path, e)
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Impossible d'écrire l'état %s : %s", path, e)


# ------------------------------------------------------------- fichiers annexes
def write_sidecars(folder: str, meta: BookMeta, cover, dry_run: bool) -> None:
    """cover.jpg / desc.txt / reader.txt (utile pour Plex et Booksonic)."""
    if not os.path.isdir(folder):
        return
    targets = []
    if cover and cover[0]:
        ext = ".png" if cover[2] == "image/png" else ".jpg"
        targets.append((os.path.join(folder, f"cover{ext}"), cover[0], "wb"))
    if meta.description:
        targets.append((os.path.join(folder, "desc.txt"), meta.description, "w"))
    if meta.narrator:
        targets.append((os.path.join(folder, "reader.txt"), meta.narrator, "w"))
    for path, data, mode in targets:
        if dry_run:
            log.debug("      [dry-run] annexe %s", os.path.basename(path))
            continue
        try:
            kwargs = {"encoding": "utf-8"} if mode == "w" else {}
            with open(path, mode, **kwargs) as fh:
                fh.write(data)
        except OSError as e:
            log.warning("      annexe %s impossible : %s", os.path.basename(path), e)


# --------------------------------------------------------------- traitement
def export_intact(cfg, meta, previous: dict) -> bool:
    """L'export de ce livre est-il toujours en bon état ?

    Vérifie que chaque fichier attendu existe et, en mode lien physique, qu'il
    partage encore son inode avec la source — un outil tiers ayant réécrit le
    fichier aurait rompu le lien sans que les métadonnées ne changent.
    """
    if cfg.export_action == "none":
        return True
    rels = previous.get("export_files") or []
    if len(rels) != len(meta.audio_files):
        return False
    for af, rel in zip(meta.audio_files, rels):
        dst = os.path.join(cfg.export_dir, rel)
        if not os.path.isfile(dst):
            return False
        if cfg.export_action == "hardlink":
            src = cfg.map_path(af["path"])
            if os.path.isfile(src) and not exportmod.links_match(src, dst):
                log.debug("    lien physique rompu, rétablissement : %s", rel)
                return False
    return True


def handle_incomplete(client, cfg, meta, item, gaps, report) -> None:
    """Pose le tag ABS et/ou déplace le dossier d'un livre non identifié."""
    labels = ", ".join(triage.FIELD_LABELS.get(g, g) for g in gaps)
    log.info("  /!\\ %s — non identifié (manque : %s)", meta.title or meta.id, labels)
    report.append({"id": meta.id, "titre": meta.title, "manque": gaps,
                   "chemin": meta.path})

    if cfg.on_incomplete in ("tag", "both"):
        try:
            triage.apply_abs_tag(client, meta.id, meta.tags, cfg.incomplete_tag,
                                 True, cfg.dry_run)
        except AbsError as e:
            log.error("      %s", e)

    if cfg.on_incomplete in ("move", "both"):
        local = cfg.map_path(meta.path)
        if os.path.isdir(local) or os.path.isfile(local):
            triage.move_aside(local, cfg.library_roots, cfg.unmatched_dir, cfg.dry_run)
        else:
            log.warning("      dossier introuvable pour déplacement : %s", local)


_NON_EXPORTES = {}


def process_item(client: AbsClient, cfg: Config, item_id: str, state: dict,
                 known_paths: set, report: list, dup_store: dict,
                 verify_store: dict, verdicts_connus: dict = None) -> str:
    """Retourne 'ok', 'skip', 'incomplete', 'error' ou 'missing'."""
    raw = client.item(item_id)
    meta = BookMeta(raw, strip_html=cfg.strip_html)

    if not meta.audio_files:
        log.debug("  %s : aucun fichier audio", meta.title or item_id)
        return "skip"

    dropped = meta.refine_authors(cfg.author_exclude, cfg.author_drop_narrators,
                                  cfg.author_sort)
    if dropped:
        log.debug("    auteurs écartés (%s) : %s", meta.title, ", ".join(dropped))

    previous = state.get(item_id, {})
    if raw.get("isMissing") and previous.get("moved"):
        log.debug("  %s : déjà déplacé, item marqué manquant dans ABS", meta.title)
        return "skip"

    # Les chemins connus servent ensuite à repérer les orphelins sur le disque
    for af in meta.audio_files:
        known_paths.add(os.path.normpath(cfg.map_path(af["path"])))

    # Enregistré avant tout retour anticipé : un doublon doit rester visible
    # même quand le cache d'état fait sauter les deux copies.
    dupmod.register(dup_store, cfg, meta)
    verifymod.register(verify_store, cfg, meta)

    # --- livre non identifié ? -------------------------------------------
    gaps = triage.missing_fields(meta, raw, cfg.incomplete_checks)
    if gaps:
        handle_incomplete(client, cfg, meta, raw, gaps, report)
        if not cfg.tag_incomplete_files:
            state.pop(meta.id, None)
            return "incomplete"
    elif cfg.remove_tag_when_complete and cfg.on_incomplete in ("tag", "both"):
        try:
            triage.apply_abs_tag(client, meta.id, meta.tags, cfg.incomplete_tag,
                                 False, cfg.dry_run)
        except AbsError as e:
            log.debug("      retrait du tag impossible : %s", e)

    # --- écriture des tags ------------------------------------------------
    need_cover = (cfg.write_cover or cfg.write_sidecars
                  or (cfg.export_action != "none" and "cover" in cfg.export_sidecars))
    # Sur une grande bibliothèque, télécharger la pochette de chaque livre à
    # chaque passe coûte cher : si ABS n'a pas touché à l'item, on s'arrête là.
    if (cfg.trust_updated_at and not cfg.force and meta.updated_at
            and previous.get("updatedAt") == meta.updated_at
            and previous.get("fingerprint")
            and (cfg.export_action == "none"
                 or (previous.get("export") == exportmod.expected_rel(cfg, meta)
                     and export_intact(cfg, meta, previous)))):
        return "skip"

    cover_raw, cover_mime = (None, None)
    if need_cover:
        cover_raw, cover_mime = client.cover(item_id)
    cover = prepare_cover(cover_raw, cover_mime, cfg.cover_max_px)

    fingerprint = meta.fingerprint(cover[0] if cover else None)
    export_rel = previous.get("export", "")
    export_ok = True
    if cfg.export_action != "none" and not gaps:
        expected = exportmod.expected_rel(cfg, meta)
        export_ok = (previous.get("export") == expected
                     and export_intact(cfg, meta, previous))
    if not cfg.force and previous.get("fingerprint") == fingerprint and export_ok:
        return "skip"

    total = len(meta.audio_files)
    label = f"{meta.author} — {meta.title}" if meta.author else meta.title
    log.info("  %s (%d fichier%s)", label, total, "s" if total > 1 else "")

    # En mode déplacement, les fichiers ne sont plus à leur emplacement d'origine
    # après la première passe : on retombe sur la copie déjà exportée.
    prev_exported = {}
    if cfg.export_action == "move":
        for i, rel in enumerate(previous.get("export_files", []), start=1):
            prev_exported[i] = os.path.join(cfg.export_dir, rel)

    touched, failed = 0, 0
    exported_files = []
    for af in meta.audio_files:
        local = cfg.map_path(af["path"])
        if not os.path.isfile(local):
            fallback = prev_exported.get(af["index"])
            if fallback and os.path.isfile(fallback):
                log.debug("    fichier déjà déplacé, traité sur place : %s", fallback)
                local = fallback
            elif runstate.deplace_par_interface(local):
                log.info("    mis de côté depuis l'interface, ignoré : %s",
                         os.path.basename(local))
                continue
            else:
                log.error("    fichier introuvable : %s (source ABS : %s)", local, af["path"])
                log.error("    -> vérifie PATH_MAP et le montage du volume")
                failed += 1
                continue
        ext = os.path.splitext(local)[1].lower()
        if ext not in AUDIO_EXT:
            continue
        try:
            if cfg.sync_chapters and total == 1 and meta.chapters:
                chapmod.sync_chapters(local, meta.chapters, af.get("duration") or 0,
                                      cfg.dry_run)
            if ext == ".mp3":
                write_mp3(local, meta, af["index"], total, cfg, cover)
            else:
                write_mp4(local, meta, af["index"], total, cfg, cover)
            touched += 1
            exported_files.append((local, af["index"]))
            log.debug("    tags écrits : %s", os.path.basename(local))
        except Exception as e:
            log.error("    échec sur %s : %s", os.path.basename(local), e)
            failed += 1

    if cfg.write_sidecars:
        write_sidecars(cfg.map_path(meta.path), meta, cover, cfg.dry_run)

    if failed:
        return "missing" if not touched else "error"

    # --- export du livre validé ------------------------------------------
    export_files = previous.get("export_files", [])
    moved = previous.get("moved", False)

    # Filtre optionnel : n'exporter que ce dont la durée colle à la fiche.
    # Un livre non encore mesuré est écarté, pas exporté « par défaut ».
    exportable = True
    if cfg.export_only_verified and cfg.export_action != "none":
        statut = (verdicts_connus or {}).get(item_id)
        exportable = statut in (verifymod.OK, verifymod.ACCEPTE)
        if not exportable:
            log.info("      export différé (durée : %s)",
                     verifymod.LIBELLES.get(statut, "pas encore mesurée"))
            _NON_EXPORTES[statut or "non mesuré"] = \
                _NON_EXPORTES.get(statut or "non mesuré", 0) + 1

    if cfg.export_action != "none" and exported_files and not gaps and exportable:
        try:
            res = exportmod.export_item(cfg, meta, exported_files, cover_raw,
                                        cover_mime, export_rel)
            export_rel = res["rel"]
            if res["files"]:
                export_files = res["files"]
            if cfg.export_action == "move" and res["copies"]:
                moved = True
                if cfg.after_move == "remove" and not cfg.dry_run:
                    try:
                        client.delete_item(item_id)
                        log.info("      item retiré de la base Audiobookshelf")
                    except AbsError as e:
                        log.warning("      %s", e)
        except Exception as e:
            log.error("    export impossible : %s", e)

    if not cfg.dry_run:
        state[item_id] = {
            "fingerprint": fingerprint,
            "title": meta.title,
            "updatedAt": meta.updated_at,
            "taggedAt": int(time.time()),
            "export": export_rel,
            "export_files": export_files,
            "moved": moved,
        }
    return "ok"


# --------------------------------------------------------------- orphelins
def handle_orphans(cfg: Config, known_paths: set, report: list) -> int:
    if cfg.orphan_action == "none":
        return 0
    roots = cfg.library_roots
    if not roots:
        return 0

    excludes = [cfg.unmatched_dir]
    if cfg.duplicate_dir:
        excludes.append(cfg.duplicate_dir)
    if cfg.export_action != "none":
        excludes.append(cfg.export_dir)
    orphans = triage.find_orphans(roots, known_paths, excludes,
                                  cfg.orphan_min_age_min * 60)
    if not orphans:
        log.info("Aucun fichier orphelin sur le disque.")
        return 0

    targets = triage.group_orphans(orphans, roots)
    log.warning("%d fichier(s) audio absent(s) d'Audiobookshelf, soit %d élément(s) à trier :",
                len(orphans), len(targets))
    for t in targets:
        log.warning("  - %s", t)
        report.append({"orphelin": t})
        if cfg.orphan_action == "move":
            triage.move_aside(t, roots, cfg.unmatched_dir, cfg.dry_run)
    if cfg.orphan_action == "report":
        log.warning("  (ORPHAN_ACTION=report : rien n'a été déplacé)")
    return len(targets)


def write_report(cfg: Config, report: list) -> None:
    if not report:
        return
    path = os.path.join(os.path.dirname(cfg.state_file) or ".", "a-traiter.json")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"genere": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "elements": report}, fh, indent=1, ensure_ascii=False)
        log.info("Rapport écrit : %s", path)
    except OSError as e:
        log.warning("Rapport impossible : %s", e)


# ------------------------------------------------------------------ passes
def select_libraries(client: AbsClient, cfg: Config) -> list:
    libs = [l for l in client.libraries() if l.get("mediaType", "book") == "book"]
    if not cfg.libraries:
        return libs
    wanted = {w.lower() for w in cfg.libraries}
    chosen = [l for l in libs if l.get("id", "").lower() in wanted
              or (l.get("name") or "").lower() in wanted]
    if not chosen:
        log.warning("Aucune bibliothèque ne correspond à ABS_LIBRARIES=%s", cfg.libraries)
    return chosen


def run_once(cfg: Config) -> int:
    client = AbsClient(cfg.abs_url, cfg.abs_token, cfg.verify_ssl, cfg.timeout)
    state = load_state(cfg.state_file)
    stats = {"ok": 0, "skip": 0, "incomplete": 0, "error": 0, "missing": 0}
    known_paths, report, dup_store, verify_store = set(), [], {}, {}
    _NON_EXPORTES.clear()
    verdicts_connus = {}
    if cfg.export_only_verified and cfg.export_action != "none":
        verdicts_connus = verifymod.verdicts_en_cache(cfg)
        pretes = sum(1 for s in verdicts_connus.values()
                     if s in (verifymod.OK, verifymod.ACCEPTE))
        log.info("Export limité aux durées conformes : %d livre(s) éligible(s) "
                 "sur %d mesuré(s)", pretes, len(verdicts_connus))
    started = time.time()
    full_scan = not cfg.only_items

    if cfg.only_items:
        item_ids = list(cfg.only_items)
        log.info("Traitement de %d item(s) explicite(s)", len(item_ids))
    else:
        item_ids = []
        for lib in select_libraries(client, cfg):
            ids = client.library_item_ids(lib["id"])
            log.info("Bibliothèque « %s » : %d livre(s)", lib.get("name", lib["id"]), len(ids))
            item_ids.extend(ids)

    runstate.maj(passe_en_cours=True, debut=int(started), traites=0,
                 total=len(item_ids), phase="tags")
    for item_id in item_ids:
        if _STOP:
            full_scan = False
            break
        runstate.ETAT["traites"] += 1
        try:
            with runstate.VERROU:
                result = process_item(client, cfg, item_id, state, known_paths,
                                      report, dup_store, verify_store,
                                      verdicts_connus)
        except AbsError as e:
            log.error("  %s : %s", item_id, e)
            result = "error"
        except Exception as e:
            log.exception("  %s : erreur inattendue : %s", item_id, e)
            result = "error"
        stats[result] = stats.get(result, 0) + 1
        if stats["ok"] and stats["ok"] % 25 == 0:
            save_state(cfg.state_file, state)

    save_state(cfg.state_file, state)

    runstate.maj(phase="vérification des durées")
    verdicts = {}
    if full_scan and not _STOP:
        try:
            verdicts = verifymod.run(cfg, client, verify_store, report)
        except Exception as e:
            log.error("Vérification des durées interrompue : %s", e)

    runstate.maj(phase="doublons et orphelins")
    duplicates_found = 0
    if full_scan:
        duplicates_found = dupmod.handle_duplicates(cfg, dup_store, report, verdicts)

    orphans = 0
    if full_scan and not cfg.libraries:
        orphans = handle_orphans(cfg, known_paths, report)
    elif cfg.orphan_action != "none":
        log.info("Détection des orphelins ignorée (passe partielle : --item ou ABS_LIBRARIES).")

    write_report(cfg, report)

    log.info(
        "Terminé en %.1fs — %d taggé(s), %d inchangé(s), %d non identifié(s), "
        "%d doublon(s), %d orphelin(s), %d fichier(s) manquant(s), %d erreur(s)%s",
        time.time() - started, stats["ok"], stats["skip"], stats["incomplete"],
        duplicates_found, orphans, stats["missing"], stats["error"],
        "  [DRY-RUN, rien écrit]" if cfg.dry_run else "")
    return 1 if stats["error"] or stats["missing"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Tagger m4b/mp3 depuis Audiobookshelf")
    parser.add_argument("--once", action="store_true", help="une seule passe puis sortie")
    parser.add_argument("--dry-run", action="store_true", help="n'écrit rien")
    parser.add_argument("--force", action="store_true", help="ignore le cache d'état")
    parser.add_argument("--item", action="append", default=[], help="id d'item ABS (répétable)")
    parser.add_argument("--library", action="append", default=[], help="nom ou id de bibliothèque")
    parser.add_argument("--no-triage", action="store_true",
                        help="désactive le tag/déplacement des non identifiés et orphelins")
    parser.add_argument("--no-verify", action="store_true",
                        help="ne contrôle pas les durées auprès du fournisseur")
    parser.add_argument("--duplicates-only", action="store_true",
                        help="ne fait que détecter les doublons (aucune écriture de tags)")
    args = parser.parse_args()

    cfg = Config.from_env()
    if args.dry_run:
        cfg.dry_run = True
    if args.force:
        cfg.force = True
    if args.item:
        cfg.only_items = args.item
    if args.library:
        cfg.libraries = args.library
    if args.once:
        cfg.interval = 0
    if args.no_triage:
        cfg.on_incomplete = "none"
        cfg.orphan_action = "none"
        cfg.duplicate_action = "none"
        cfg.verify_action = "none"
        cfg.tag_incomplete_files = True
    if args.duplicates_only:
        cfg.dry_run = True
        cfg.force = True
        cfg.on_incomplete = "none"
        cfg.orphan_action = "none"
        cfg.export_action = "none"
        cfg.interval = 0
        if cfg.duplicate_action == "none":
            cfg.duplicate_action = "report"
    if args.no_verify:
        cfg.verify_action = "none"

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    errors = cfg.validate()
    if errors:
        for e in errors:
            log.error("Configuration : %s", e)
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("abs-m4b-tagger — serveur %s", cfg.abs_url)
    if cfg.path_map:
        for src, dst in cfg.path_map:
            log.info("Mapping : %s  ->  %s", src, dst)
    else:
        log.warning("PATH_MAP vide : les chemins ABS doivent être identiques dans ce conteneur.")
    if cfg.sync_chapters:
        version = chapmod.ffmpeg_version()
        if chapmod.ffmpeg_available() and version:
            log.info("Synchronisation des chapitres activée — %s", version)
        else:
            log.error("SYNC_CHAPTERS=true mais ffmpeg/ffprobe est introuvable dans le "
                      "conteneur : les chapitres ne seront PAS synchronisés.")
    if cfg.on_incomplete != "none":
        log.info("Livres non identifiés : action=%s, critères=%s, tag ABS=« %s »",
                 cfg.on_incomplete, ",".join(cfg.incomplete_checks), cfg.incomplete_tag)
    if cfg.orphan_action != "none":
        log.info("Orphelins disque : action=%s, dossier de tri=%s",
                 cfg.orphan_action, cfg.unmatched_dir)
    if cfg.verify_action != "none":
        log.info("Vérification des durées : fournisseur=%s, tolérance=%s%% et %s min "
                 "minimum%s", cfg.verify_provider, cfg.verify_tolerance_pct,
                 cfg.verify_min_ecart_min,
                 f", tag de validation ABS=« {cfg.verify_accept_tag} »"
                 if cfg.verify_accept_tag else "")
    if cfg.duplicate_action != "none":
        log.info("Doublons : action=%s, clés=%s%s", cfg.duplicate_action,
                 ",".join(cfg.duplicate_keys),
                 f", dossier={cfg.duplicate_dir}" if cfg.duplicate_action == "move" else "")
        if cfg.duplicate_action == "move" and cfg.export_action != "none":
            log.warning("DUPLICATE_ACTION=move avec l'export actif : les doublons sont "
                        "déplacés APRÈS avoir été exportés. Fais une passe "
                        "EXPORT_ACTION=none d'abord.")
    if cfg.export_action != "none":
        log.info("Export (%s) vers %s", cfg.export_action, cfg.export_dir)
        if cfg.export_action == "hardlink":
            roots = cfg.library_roots or []
            bad = [r for r in roots if not exportmod.same_device(r, cfg.export_dir)]
            if bad:
                log.warning("Lien physique demandé mais %s et %s ne sont pas sur le même "
                            "système de fichiers : les fichiers seront COPIÉS (espace doublé).",
                            ", ".join(bad), cfg.export_dir)
            elif roots:
                log.info("Lien physique : source et destination sur le même volume, "
                         "aucun espace supplémentaire consommé.")
        if cfg.export_action == "move":
            log.warning("Mode DÉPLACEMENT : les fichiers quittent la bibliothèque source "
                        "(nettoyage du dossier source : %s, item ABS : %s)",
                        "oui" if cfg.move_cleanup_source else "non", cfg.after_move)
        log.info("Gabarit dossier : %s", cfg.export_dir_template)
        log.info("Gabarit fichier : %s%s", cfg.export_file_template,
                 "  + annexes : " + ", ".join(cfg.export_sidecars)
                 if cfg.export_sidecars else "")

    if cfg.web_enable:
        webmod.demarrer(cfg)

    rc = 0
    while True:
        rc = run_once(cfg)
        if cfg.interval <= 0 or _STOP:
            break
        log.info("Prochaine passe dans %d s", cfg.interval)
        for _ in range(cfg.interval):
            if _STOP:
                break
            time.sleep(1)
        if _STOP:
            break
    return rc


if __name__ == "__main__":
    sys.exit(main())
