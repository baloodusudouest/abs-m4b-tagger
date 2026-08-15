"""Vérification des identifications par la durée de référence Audible.

Audiobookshelf peut associer une fiche Audible au mauvais livre : deux tomes
d'une même série, une réédition, voire deux romans sans rapport. Les tags
écrits dans les fichiers sont alors faux, et l'export les range sous un titre
qui n'est pas le leur.

La durée du fichier tranche. Elle est mesurée par ABS au scan, dans le fichier
lui-même — c'est un fait, pas une métadonnée. On la compare à la durée annoncée
par Audible pour l'ASIN de l'item (endpoint `/api/search/books`, champ
`duration`, en minutes). Un écart important signifie que la fiche ne correspond
pas au contenu.

Chaque appel sort vers Audible : sur une grande bibliothèque, la première passe
est longue. Le résultat est donc mis en cache dans `/config/verifications.json`
et n'est refait que si l'ASIN ou la durée du fichier a changé.
"""

import json
import logging
import os
import time

from absclient import AbsError

log = logging.getLogger("verify")

# Verdicts possibles
OK = "conforme"
ECART = "ecart"
INTROUVABLE = "introuvable"
SANS_ASIN = "sans_asin"

LIBELLES = {
    OK: "conforme",
    ECART: "durée incohérente",
    INTROUVABLE: "absent du fournisseur",
    SANS_ASIN: "sans ASIN",
}


# --------------------------------------------------------------- collecte
def register(store: dict, cfg, meta) -> None:
    """Mémorise un item à vérifier. Appelé pour tous les items, y compris ceux
    que le cache d'état fait sauter."""
    if cfg.verify_action == "none":
        return
    store[meta.id] = {
        "id": meta.id,
        "titre": meta.title,
        "auteur": ", ".join(meta.authors),
        "asin": meta.asin.strip().upper(),
        "duree_s": int(sum(af.get("duration") or 0 for af in meta.audio_files)),
        "chemin": cfg.map_path(meta.path),
    }


# ------------------------------------------------------------------ cache
def cache_path(cfg) -> str:
    return os.path.join(os.path.dirname(cfg.state_file) or ".", "verifications.json")


def load_cache(cfg) -> dict:
    try:
        with open(cache_path(cfg), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        log.warning("Cache de vérification illisible : %s", e)
        return {}


def save_cache(cfg, cache: dict) -> None:
    path = cache_path(cfg)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Cache de vérification non enregistré : %s", e)


def _needs_check(entry: dict, cached: dict, retry_days: int) -> bool:
    """Un item déjà vérifié n'est repris que si quelque chose a bougé."""
    if not cached:
        return True
    if cached.get("asin") != entry["asin"]:
        return True
    # Tolère la seconde d'arrondi entre deux scans ABS
    if abs((cached.get("duree_s") or 0) - entry["duree_s"]) > 2:
        return True
    # Une absence de réponse n'est pas un verdict : on retente plus tard
    if cached.get("statut") == INTROUVABLE and retry_days > 0:
        age = time.time() - (cached.get("verifie_le") or 0)
        if age > retry_days * 86400:
            return True
    return False


# ------------------------------------------------------------ comparaison
def _pick(results: list, asin: str) -> dict:
    """Choisit le résultat correspondant à l'ASIN demandé."""
    for r in results:
        if str(r.get("asin") or "").strip().upper() == asin:
            return r
    return results[0] if results else {}


def _compare(entry: dict, result: dict, tolerance_pct: float) -> dict:
    ref_min = result.get("duration")
    try:
        ref_s = int(float(ref_min) * 60)
    except (TypeError, ValueError):
        ref_s = 0
    if ref_s <= 0:
        return {"statut": INTROUVABLE, "motif": "durée absente de la fiche"}

    ecart_s = abs(entry["duree_s"] - ref_s)
    pct = (ecart_s / ref_s) * 100 if ref_s else 100.0
    statut = OK if pct <= tolerance_pct else ECART
    return {
        "statut": statut,
        "duree_ref_s": ref_s,
        "ecart_s": ecart_s,
        "ecart_pct": round(pct, 1),
        "titre_ref": result.get("title") or "",
        "asin_ref": str(result.get("asin") or "").strip().upper(),
    }


def _hms(seconds: int) -> str:
    if not seconds:
        return "?"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


# ------------------------------------------------------------------ passe
def run(cfg, client, store: dict, report: list) -> dict:
    """Vérifie les items non encore validés. Retourne {item_id: verdict}."""
    if cfg.verify_action == "none" or not store:
        return {}

    cache = load_cache(cfg)
    verdicts = {}
    a_faire = []

    for item_id, entry in store.items():
        if not entry["asin"]:
            verdicts[item_id] = {"statut": SANS_ASIN}
            continue
        cached = cache.get(item_id)
        if _needs_check(entry, cached, cfg.verify_retry_days):
            a_faire.append(entry)
        else:
            verdicts[item_id] = cached

    if not a_faire:
        log.info("Vérification des durées : tout est à jour (%d livre(s) en cache).",
                 len(verdicts))
        return verdicts

    plafond = cfg.verify_max_per_pass or len(a_faire)
    lot = a_faire[:plafond]
    log.info("Vérification des durées auprès de « %s » : %d livre(s) à contrôler"
             "%s (cache : %d déjà validé(s))",
             cfg.verify_provider, len(lot),
             f" sur {len(a_faire)}" if len(lot) < len(a_faire) else "",
             len(verdicts))
    if len(lot) > 200:
        log.info("  Compter environ %d minute(s) : chaque livre est une requête "
                 "sortante vers le fournisseur.",
                 max(1, int(len(lot) * cfg.verify_delay_ms / 1000 / 60)))

    stats = {OK: 0, ECART: 0, INTROUVABLE: 0}
    echecs_consecutifs = 0

    for i, entry in enumerate(lot, start=1):
        try:
            results = client.search_books(entry["asin"], provider=cfg.verify_provider)
            echecs_consecutifs = 0
        except AbsError as e:
            echecs_consecutifs += 1
            log.warning("  %s : %s", entry["titre"] or entry["id"], e)
            if echecs_consecutifs >= 5:
                log.error("  5 échecs consécutifs : vérification interrompue, "
                          "elle reprendra à la prochaine passe.")
                break
            time.sleep(min(30, 2 ** echecs_consecutifs))
            continue
        except Exception as e:
            echecs_consecutifs += 1
            log.warning("  %s : %s", entry["titre"] or entry["id"], e)
            if echecs_consecutifs >= 5:
                log.error("  5 échecs consécutifs : vérification interrompue.")
                break
            continue

        if results:
            verdict = _compare(entry, _pick(results, entry["asin"]), cfg.verify_tolerance_pct)
        else:
            verdict = {"statut": INTROUVABLE, "motif": "aucun résultat"}

        verdict.update({"asin": entry["asin"], "duree_s": entry["duree_s"],
                        "verifie_le": int(time.time())})
        verdicts[entry["id"]] = verdict
        cache[entry["id"]] = verdict
        stats[verdict["statut"]] = stats.get(verdict["statut"], 0) + 1

        if verdict["statut"] == ECART:
            log.warning("  /!\\ %s — %s : fichier %s, fiche %s (%s%%)",
                        entry["titre"] or entry["id"], entry["asin"],
                        _hms(entry["duree_s"]), _hms(verdict["duree_ref_s"]),
                        verdict["ecart_pct"])
            if verdict.get("titre_ref") and verdict["titre_ref"] != entry["titre"]:
                log.warning("      la fiche %s porte le titre « %s »",
                            verdict.get("asin_ref") or entry["asin"], verdict["titre_ref"])
            report.append({
                "identification_suspecte": entry["asin"],
                "titre": entry["titre"],
                "titre_fiche": verdict.get("titre_ref"),
                "chemin": entry["chemin"],
                "duree_fichier": _hms(entry["duree_s"]),
                "duree_fiche": _hms(verdict["duree_ref_s"]),
                "ecart_pct": verdict["ecart_pct"],
            })

        if i % 50 == 0:
            save_cache(cfg, cache)
            log.info("  … %d/%d vérifiés", i, len(lot))
        if cfg.verify_delay_ms:
            time.sleep(cfg.verify_delay_ms / 1000.0)

    save_cache(cfg, cache)
    reste = len(a_faire) - len(lot)
    log.info("Vérification terminée — %d conforme(s), %d écart(s), %d introuvable(s)%s",
             stats.get(OK, 0), stats.get(ECART, 0), stats.get(INTROUVABLE, 0),
             f", {reste} reporté(s) à la prochaine passe" if reste > 0 else "")
    return verdicts
