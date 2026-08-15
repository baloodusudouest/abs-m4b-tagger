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
est longue. Le cache `/config/verifications.json` ne conserve donc que la
**mesure** — durée du fichier, durée de la fiche — et jamais le verdict. Celui-ci
est recalculé à chaque passe : changer la tolérance reclasse instantanément toute
la bibliothèque, sans une seule requête sortante. Et un écart détecté il y a trois
passes reste signalé tant qu'il n'est pas corrigé.

Le seuil combine deux conditions, qui doivent être réunies pour signaler un
écart : un pourcentage (VERIFY_TOLERANCE_PCT) et un plancher absolu
(VERIFY_MIN_ECART_MIN). Sans ce plancher, deux minutes de jingle représentent
4 % sur un livre d'une heure mais 0,2 % sur vingt heures : un seuil purement
relatif noierait les livres courts sous les faux positifs.
"""

import json
import logging
import os
import time

import runstate
from absclient import AbsError

log = logging.getLogger("verify")

# Verdicts possibles
OK = "conforme"
ECART = "ecart"
ACCEPTE = "accepte"
INTROUVABLE = "introuvable"
SANS_ASIN = "sans_asin"

LIBELLES = {
    OK: "conforme",
    ECART: "durée incohérente",
    ACCEPTE: "écart validé manuellement",
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
        "tags": list(meta.tags or []),
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
    """Écriture atomique, sous le verrou partagé avec l'interface web."""
    path = cache_path(cfg)
    try:
        runstate.VERROU.acquire()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Cache de vérification non enregistré : %s", e)
    finally:
        try:
            runstate.VERROU.release()
        except RuntimeError:
            pass


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


def _mesurer(result: dict) -> dict:
    """Extrait la durée de référence. Ne juge pas : le verdict vient plus tard."""
    try:
        ref_s = int(float(result.get("duration")) * 60)
    except (TypeError, ValueError):
        ref_s = 0
    if ref_s <= 0:
        return {"trouve": False, "motif": "durée absente de la fiche"}
    return {
        "trouve": True,
        "duree_ref_s": ref_s,
        "titre_ref": result.get("title") or "",
        "asin_ref": str(result.get("asin") or "").strip().upper(),
    }


def verdict(mesure: dict, cfg) -> dict:
    """Applique les seuils courants à une mesure. Aucun accès réseau.

    Les deux conditions doivent être réunies pour signaler : dépasser le
    pourcentage ET dépasser le plancher absolu.
    """
    out = dict(mesure)
    if not mesure.get("trouve"):
        out["statut"] = INTROUVABLE
        return out
    ref_s = mesure["duree_ref_s"]
    ecart_s = abs(mesure["duree_s"] - ref_s)
    pct = (ecart_s / ref_s) * 100 if ref_s else 100.0
    depasse_pct = pct > cfg.verify_tolerance_pct
    depasse_plancher = ecart_s > cfg.verify_min_ecart_min * 60
    out["ecart_s"] = ecart_s
    out["ecart_pct"] = round(pct, 1)
    out["statut"] = ECART if (depasse_pct and depasse_plancher) else OK
    return out


def _hms(seconds: int) -> str:
    if not seconds:
        return "?"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


# ------------------------------------------------- validations manuelles
def validations_path(cfg) -> str:
    return os.path.join(os.path.dirname(cfg.state_file) or ".", "validations.json")


def load_validations(cfg) -> dict:
    try:
        with open(validations_path(cfg), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        log.warning("Validations manuelles illisibles : %s", e)
        return {}


def save_validations(cfg, data: dict) -> None:
    path = validations_path(cfg)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Validations manuelles non enregistrées : %s", e)


def _validation_valable(entry: dict, enreg: dict) -> bool:
    """Une validation ne vaut que pour l'état exact qui a été contrôlé.

    Si le livre est réidentifié sur un autre ASIN, ou si le fichier est
    remplacé, ce n'est plus le même objet : la validation devient caduque.
    """
    if not enreg:
        return False
    if enreg.get("asin") != entry["asin"]:
        return False
    return abs((enreg.get("duree_s") or 0) - entry["duree_s"]) <= 2


# --------------------------------------------------- compatibilité du cache
def _normalise(cached: dict) -> dict:
    """Convertit une entrée de cache d'une version antérieure en mesure pure.

    Les premières versions y écrivaient le verdict (`statut`, `ecart_pct`)
    calculé avec la tolérance de l'époque. On ne garde que les faits.
    """
    if "trouve" in cached:
        return cached
    out = {k: cached[k] for k in
           ("asin", "duree_s", "duree_ref_s", "titre_ref", "asin_ref",
            "verifie_le", "titre", "chemin")
           if k in cached}
    out["trouve"] = bool(cached.get("duree_ref_s"))
    if not out["trouve"]:
        out["motif"] = cached.get("motif") or "aucun résultat"
    return out


def verdicts_en_cache(cfg) -> dict:
    """Verdicts connus AVANT la passe, à partir des seules mesures en cache.

    La vérification s'exécute après la boucle sur les items, alors que l'export
    a lieu pendant. Sans ce pré-chargement, aucun filtre sur la durée ne serait
    applicable à l'export. Un livre jamais mesuré n'apparaît pas ici : il sera
    mesuré en fin de passe et exportable à la suivante.
    """
    if cfg.verify_action == "none":
        return {}
    validations = load_validations(cfg)
    out = {}
    for item_id, brut in load_cache(cfg).items():
        mesure = _normalise(brut)
        v = verdict(mesure, cfg)
        if v["statut"] == ECART:
            enreg = validations.get(item_id) or {}
            if enreg and not enreg.get("revoquee"):
                v["statut"] = ACCEPTE
        out[item_id] = v["statut"]
    return out


# ------------------------------------------------------------------ passe
def run(cfg, client, store: dict, report: list) -> dict:
    """Mesure ce qui manque, puis reclasse TOUT avec les seuils courants."""
    if cfg.verify_action == "none" or not store:
        return {}

    cache = load_cache(cfg)
    mesures, a_faire = {}, []
    sans_asin = 0

    for item_id, entry in store.items():
        if not entry["asin"]:
            mesures[item_id] = {"trouve": False, "sans_asin": True}
            sans_asin += 1
            continue
        cached = _normalise(cache.get(item_id) or {})
        if _needs_check(entry, cached, cfg.verify_retry_days):
            a_faire.append(entry)
        else:
            mesures[item_id] = cached

    if a_faire:
        _mesurer_lot(cfg, client, a_faire, cache, mesures)
    else:
        log.info("Vérification des durées : aucune nouvelle mesure nécessaire "
                 "(%d livre(s) en cache).", len(mesures) - sans_asin)

    # --- verdicts : recalculés à chaque passe, y compris sur le cache --------
    validations = load_validations(cfg)
    validations_modifiees = False
    verdicts = {}
    stats = {OK: 0, ECART: 0, ACCEPTE: 0, INTROUVABLE: 0, SANS_ASIN: 0}
    ecarts, perimees = [], []

    for item_id, mesure in mesures.items():
        if mesure.get("sans_asin"):
            v = {"statut": SANS_ASIN}
        else:
            v = verdict(mesure, cfg)

        if v["statut"] == ECART and cfg.verify_accept_tag:
            entry = store[item_id]
            marque = cfg.verify_accept_tag in (entry.get("tags") or [])
            enreg = validations.get(item_id)

            if not marque:
                if enreg:                     # tag retiré dans ABS : on oublie
                    validations.pop(item_id, None)
                    validations_modifiees = True
            elif not enreg:                   # première pose du tag
                validations[item_id] = {
                    "asin": entry["asin"],
                    "duree_s": entry["duree_s"],
                    "duree_ref_s": v.get("duree_ref_s"),
                    "ecart_pct": v.get("ecart_pct"),
                    "titre": entry["titre"],
                    "valide_le": int(time.time()),
                }
                validations_modifiees = True
                v["statut"] = ACCEPTE
                log.info("  Écart validé pour « %s » (%s%%) — tag « %s »",
                         entry["titre"] or item_id, v.get("ecart_pct"),
                         cfg.verify_accept_tag)
            elif _validation_valable(entry, enreg) and not enreg.get("revoquee"):
                v["statut"] = ACCEPTE
            elif _validation_valable(entry, enreg):
                pass                          # révoquée : on attend une nouvelle pose
            else:
                # L'objet validé n'est plus le même : la validation ne vaut plus.
                # Elle n'est PAS renouvelée automatiquement — sinon la sécurité
                # ne servirait à rien.
                perimees.append((item_id, dict(enreg)))
                validations[item_id] = {
                    "asin": entry["asin"],
                    "duree_s": entry["duree_s"],
                    "titre": entry["titre"],
                    "revoquee": True,
                    "revoquee_le": int(time.time()),
                }
                validations_modifiees = True

        verdicts[item_id] = v
        stats[v["statut"]] = stats.get(v["statut"], 0) + 1
        if v["statut"] == ECART:
            ecarts.append((item_id, v))

    for item_id, enreg in perimees:
        entry = store[item_id]
        log.warning("  Validation ANNULÉE pour « %s » : validée sur ASIN %s / %s, "
                    "l'item est maintenant sur ASIN %s / %s.",
                    enreg.get("titre") or item_id, enreg.get("asin"),
                    _hms(enreg.get("duree_s") or 0), entry["asin"],
                    _hms(entry["duree_s"]))
        log.warning("      Pour revalider : retire puis repose le tag « %s » "
                    "dans Audiobookshelf.", cfg.verify_accept_tag)
    if validations_modifiees:
        save_validations(cfg, validations)

    if ecarts:
        ecarts.sort(key=lambda x: -x[1]["ecart_pct"])
        log.warning("%d livre(s) dont la durée ne correspond pas à la fiche "
                    "(tolérance %s%%, plancher %s min) :",
                    len(ecarts), cfg.verify_tolerance_pct, cfg.verify_min_ecart_min)
        for item_id, v in ecarts:
            entry = store[item_id]
            log.warning("  /!\\ %s — %s : fichier %s, fiche %s (%s%%)",
                        entry["titre"] or item_id, entry["asin"],
                        _hms(entry["duree_s"]), _hms(v["duree_ref_s"]), v["ecart_pct"])
            if v.get("titre_ref") and v["titre_ref"] != entry["titre"]:
                log.warning("        la fiche %s porte le titre « %s »",
                            v.get("asin_ref") or entry["asin"], v["titre_ref"])
            report.append({
                "identification_suspecte": entry["asin"],
                "titre": entry["titre"],
                "titre_fiche": v.get("titre_ref"),
                "chemin": entry["chemin"],
                "duree_fichier": _hms(entry["duree_s"]),
                "duree_fiche": _hms(v["duree_ref_s"]),
                "ecart_pct": v["ecart_pct"],
            })

    log.info("Durées — %d conforme(s), %d écart(s), %d validé(s) manuellement, "
             "%d introuvable(s), %d sans ASIN",
             stats[OK], stats[ECART], stats[ACCEPTE], stats[INTROUVABLE],
             stats[SANS_ASIN])
    if ecarts and cfg.verify_accept_tag:
        log.info("  Un écart normal (silences resserrés, édition différente) se "
                 "valide en posant le tag « %s » sur l'item dans Audiobookshelf.",
                 cfg.verify_accept_tag)
    return verdicts


def _mesurer_lot(cfg, client, a_faire: list, cache: dict, mesures: dict) -> None:
    """Interroge le fournisseur pour les items non encore mesurés."""
    plafond = cfg.verify_max_per_pass or len(a_faire)
    lot = a_faire[:plafond]
    log.info("Vérification des durées auprès de « %s » : %d livre(s) à mesurer%s "
             "(cache : %d)", cfg.verify_provider, len(lot),
             f" sur {len(a_faire)}" if len(lot) < len(a_faire) else "", len(mesures))
    if len(lot) > 200:
        log.info("  Compter environ %d minute(s) : chaque livre est une requête "
                 "sortante vers le fournisseur.",
                 max(1, int(len(lot) * cfg.verify_delay_ms / 1000 / 60)))

    echecs = 0
    for i, entry in enumerate(lot, start=1):
        try:
            results = client.search_books(entry["asin"], provider=cfg.verify_provider)
            echecs = 0
        except (AbsError, Exception) as e:
            echecs += 1
            log.warning("  %s : %s", entry["titre"] or entry["id"], e)
            if echecs >= 5:
                log.error("  5 échecs consécutifs : mesure interrompue, elle "
                          "reprendra à la prochaine passe.")
                break
            time.sleep(min(30, 2 ** echecs))
            continue

        if results:
            mesure = _mesurer(_pick(results, entry["asin"]))
        else:
            mesure = {"trouve": False, "motif": "aucun résultat"}
        mesure.update({"asin": entry["asin"], "duree_s": entry["duree_s"],
                       "titre": entry["titre"], "chemin": entry["chemin"],
                       "verifie_le": int(time.time())})
        mesures[entry["id"]] = mesure
        cache[entry["id"]] = mesure

        if i % 50 == 0:
            save_cache(cfg, cache)
            log.info("  … %d/%d mesurés", i, len(lot))
        if cfg.verify_delay_ms:
            time.sleep(cfg.verify_delay_ms / 1000.0)

    save_cache(cfg, cache)
    reste = len(a_faire) - len(lot)
    if reste > 0:
        log.info("  %d livre(s) reporté(s) à la prochaine passe.", reste)
