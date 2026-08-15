"""Interface web de revue de la bibliothèque.

Les passes produisent des constats ; cette interface sert à les traiter. Elle
tourne dans le même conteneur, sur un thread séparé, et partage `/config` avec
le traitement de fond.

Quatre files de travail :
  - écarts de durée      : valider, ou réidentifier le livre
  - doublons             : choisir la copie à garder, isoler les autres
  - livres non identifiés : chercher une fiche et l'appliquer
  - orphelins disque      : mettre de côté, ou ignorer

Toutes les écritures passent par le verrou de `runstate` : jamais en même temps
que la passe de fond.

L'interface n'a AUCUNE authentification : elle est destinée au réseau local.
Elle peut modifier la base Audiobookshelf et déplacer des fichiers — ne l'expose
pas sur Internet sans placer une authentification devant.
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request, Response

import runstate
import triage
import verify as verifymod
from absclient import AbsClient, AbsError

log = logging.getLogger("web")


# --------------------------------------------------------------- utilitaires
def _hms(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "?"
    if not seconds:
        return "?"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _lire_json(path: str, defaut):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut
    except Exception as e:
        log.warning("Lecture de %s impossible : %s", path, e)
        return defaut


def _rapport(cfg) -> list:
    path = os.path.join(os.path.dirname(cfg.state_file) or ".", "a-traiter.json")
    data = _lire_json(path, {})
    return data.get("elements", []) if isinstance(data, dict) else []


def creer_app(cfg) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    client = AbsClient(cfg.abs_url, cfg.abs_token, cfg.verify_ssl, cfg.timeout)
    cache_titres = {}

    @app.errorhandler(Exception)
    def _toute_erreur(e):
        """ABS injoignable, timeout, JSON illisible : l'interface doit afficher
        un message, jamais une page 500 vide."""
        code = getattr(e, "code", None)
        if isinstance(code, int) and code < 500:
            return jsonify({"erreur": getattr(e, "description", str(e))}), code
        log.warning("%s %s : %s", request.method, request.path, e)
        return jsonify({"erreur": f"{type(e).__name__} : {e}"}), 502

    # ------------------------------------------------------------- helpers
    def _titre(item_id: str, mesure: dict) -> dict:
        """Titre et chemin, sans accès réseau : le remplissage est fait avant."""
        if mesure.get("titre"):
            return {"titre": mesure["titre"], "chemin": mesure.get("chemin", "")}
        return cache_titres.get(item_id) or {"titre": item_id, "chemin": ""}

    def _completer_titres(manquants: dict) -> None:
        """Récupère les titres absents du cache, en parallèle, une seule fois.

        Les caches écrits par les versions antérieures ne contiennent que des
        durées. Interroger ABS en série pour 150 livres bloquait l'affichage
        plusieurs minutes ; on parallélise, puis on écrit le résultat dans
        `verifications.json` pour que la fois suivante soit immédiate.
        """
        a_chercher = [i for i in manquants if i not in cache_titres]
        if not a_chercher:
            return
        log.info("Interface : récupération de %d titre(s) manquant(s)…",
                 len(a_chercher))

        def _un(item_id):
            try:
                raw = client.item(item_id)
                md = (raw.get("media") or {}).get("metadata") or {}
                return item_id, {"titre": md.get("title") or item_id,
                                 "chemin": raw.get("path", "")}
            except Exception:
                return item_id, {"titre": item_id, "chemin": ""}

        with ThreadPoolExecutor(max_workers=8) as pool:
            for item_id, info in pool.map(_un, a_chercher):
                cache_titres[item_id] = info

        # Persisté : le coût n'est payé qu'une fois.
        with runstate.VERROU:
            brut = verifymod.load_cache(cfg)
            touche = False
            for item_id, info in cache_titres.items():
                if item_id in brut and not brut[item_id].get("titre"):
                    brut[item_id]["titre"] = info["titre"]
                    brut[item_id]["chemin"] = info["chemin"]
                    touche = True
            if touche:
                verifymod.save_cache(cfg, brut)

    def _ecarts() -> list:
        mesures = verifymod.load_cache(cfg)
        validations = verifymod.load_validations(cfg)

        # 1er temps : quels items sont concernés, sans toucher au réseau
        retenus = {}
        for item_id, brut in mesures.items():
            m = verifymod._normalise(brut)
            if not m.get("trouve"):
                continue
            v = verifymod.verdict(m, cfg)
            enreg = validations.get(item_id) or {}
            valide = bool(enreg) and not enreg.get("revoquee")
            if v["statut"] == verifymod.ECART or valide:
                retenus[item_id] = m
        _completer_titres({i: m for i, m in retenus.items() if not m.get("titre")})

        out = []
        for item_id, brut in mesures.items():
            mesure = verifymod._normalise(brut)
            if not mesure.get("trouve"):
                continue
            v = verifymod.verdict(mesure, cfg)
            enreg = validations.get(item_id) or {}
            valide = bool(enreg) and not enreg.get("revoquee")
            if v["statut"] != verifymod.ECART and not valide:
                continue
            info = _titre(item_id, mesure)
            out.append({
                "id": item_id,
                "titre": info["titre"],
                "chemin": info["chemin"],
                "asin": mesure.get("asin", ""),
                "titre_fiche": mesure.get("titre_ref", ""),
                "duree_fichier": _hms(mesure.get("duree_s")),
                "duree_fiche": _hms(mesure.get("duree_ref_s")),
                "ecart_pct": v.get("ecart_pct"),
                "ecart": _hms(v.get("ecart_s")),
                "valide": valide,
            })
        out.sort(key=lambda x: (x["valide"], -(x["ecart_pct"] or 0)))
        return out

    # -------------------------------------------------------------- lecture
    @app.get("/api/etat")
    def etat():
        e = dict(runstate.ETAT)
        e["tolerance_pct"] = cfg.verify_tolerance_pct
        e["plancher_min"] = cfg.verify_min_ecart_min
        e["tag_validation"] = cfg.verify_accept_tag
        e["abs_url"] = cfg.abs_url
        e["dry_run"] = cfg.dry_run
        return jsonify(e)

    @app.get("/api/ecarts")
    def ecarts():
        return jsonify(_ecarts())

    @app.get("/api/doublons")
    def doublons():
        groupes = [e for e in _rapport(cfg) if "doublon" in e]
        for g in groupes:
            for c in g.get("copies", []):
                c.setdefault("titre", g.get("titre", ""))
        return jsonify(groupes)

    @app.get("/api/non-identifies")
    def non_identifies():
        return jsonify([e for e in _rapport(cfg) if "manque" in e])

    @app.get("/api/orphelins")
    def orphelins():
        return jsonify([e["orphelin"] for e in _rapport(cfg) if "orphelin" in e])

    @app.get("/api/cover/<item_id>")
    def cover(item_id):
        data, mime = client.cover(item_id)
        if not data:
            return Response(status=404)
        return Response(data, mimetype=mime or "image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/chercher")
    def chercher():
        q = (request.args.get("q") or "").strip()
        auteur = (request.args.get("auteur") or "").strip()
        if not q:
            return jsonify({"erreur": "requête vide"}), 400
        try:
            res = client.search_books(q, auteur, cfg.verify_provider)
        except AbsError as e:
            return jsonify({"erreur": str(e)}), 502
        sortie = []
        for r in res[:10]:
            serie = (r.get("series") or [{}])[0] if r.get("series") else {}
            sortie.append({
                "asin": r.get("asin") or "",
                "titre": r.get("title") or "",
                "auteur": r.get("author") or "",
                "narrateur": r.get("narrator") or "",
                "annee": r.get("publishedYear") or "",
                "duree_min": r.get("duration"),
                "duree": _hms((r.get("duration") or 0) * 60),
                "serie": serie.get("series") or "",
                "tome": serie.get("sequence") or "",
                "cover": r.get("cover") or "",
                "editeur": r.get("publisher") or "",
                "langue": r.get("language") or "",
            })
        return jsonify(sortie)

    # -------------------------------------------------------------- actions
    @app.post("/api/valider")
    def valider():
        body = request.get_json(force=True) or {}
        item_id = body.get("id")
        actif = bool(body.get("actif", True))
        if not item_id or not cfg.verify_accept_tag:
            return jsonify({"erreur": "paramètres manquants"}), 400
        with runstate.VERROU:
            try:
                raw = client.item(item_id)
                tags = list((raw.get("media") or {}).get("tags") or [])
                if actif and cfg.verify_accept_tag not in tags:
                    tags.append(cfg.verify_accept_tag)
                elif not actif:
                    tags = [t for t in tags if t != cfg.verify_accept_tag]
                client.set_tags(item_id, tags)
            except AbsError as e:
                return jsonify({"erreur": str(e)}), 502

            validations = verifymod.load_validations(cfg)
            if actif:
                mesures = verifymod.load_cache(cfg)
                m = verifymod._normalise(mesures.get(item_id) or {})
                validations[item_id] = {
                    "asin": m.get("asin", ""),
                    "duree_s": m.get("duree_s", 0),
                    "duree_ref_s": m.get("duree_ref_s"),
                    "titre": m.get("titre") or _titre(item_id, m)["titre"],
                    "valide_le": int(time.time()),
                    "via": "interface",
                }
            else:
                validations.pop(item_id, None)
            verifymod.save_validations(cfg, validations)
        return jsonify({"ok": True, "valide": actif})

    @app.post("/api/appliquer")
    def appliquer():
        """Applique une fiche fournisseur à un item : métadonnées + pochette."""
        body = request.get_json(force=True) or {}
        item_id, asin = body.get("id"), (body.get("asin") or "").strip()
        if not item_id or not asin:
            return jsonify({"erreur": "paramètres manquants"}), 400
        with runstate.VERROU:
            try:
                res = client.search_books(asin, provider=cfg.verify_provider)
            except AbsError as e:
                return jsonify({"erreur": str(e)}), 502
            fiche = next((r for r in res
                          if str(r.get("asin") or "").upper() == asin.upper()), None)
            if not fiche:
                return jsonify({"erreur": f"fiche {asin} introuvable"}), 404

            md = {"title": fiche.get("title"), "asin": fiche.get("asin")}
            for src, dst in (("subtitle", "subtitle"), ("publisher", "publisher"),
                             ("description", "descriptionPlain"),
                             ("publishedYear", "publishedYear"),
                             ("language", "language"), ("isbn", "isbn")):
                val = fiche.get(dst if dst != src else src)
                if val:
                    md[src] = val
            if fiche.get("author"):
                md["authors"] = [{"name": a.strip()}
                                 for a in str(fiche["author"]).split(",") if a.strip()]
            if fiche.get("narrator"):
                md["narrators"] = [n.strip()
                                   for n in str(fiche["narrator"]).split(",") if n.strip()]
            if fiche.get("series"):
                s0 = fiche["series"][0] if isinstance(fiche["series"], list) else fiche["series"]
                nom = s0.get("series") or s0.get("name")
                if nom:
                    md["series"] = [{"name": nom, "sequence": s0.get("sequence") or None}]
            if fiche.get("genres"):
                md["genres"] = list(fiche["genres"])

            try:
                client.update_metadata(item_id, md)
                if fiche.get("cover"):
                    try:
                        client.set_cover_url(item_id, fiche["cover"])
                    except AbsError as e:
                        log.warning("Pochette non appliquée : %s", e)
            except AbsError as e:
                return jsonify({"erreur": str(e)}), 502

            # La mesure et la validation portaient sur l'ancienne fiche.
            mesures = verifymod.load_cache(cfg)
            if mesures.pop(item_id, None) is not None:
                verifymod.save_cache(cfg, mesures)
            validations = verifymod.load_validations(cfg)
            if validations.pop(item_id, None) is not None:
                verifymod.save_validations(cfg, validations)
        return jsonify({"ok": True, "titre": fiche.get("title"), "asin": asin})

    @app.post("/api/doublon/resoudre")
    def resoudre_doublon():
        """Isole les copies non retenues d'un groupe."""
        body = request.get_json(force=True) or {}
        garder = body.get("garder")
        copies = body.get("copies") or []
        libelle = (body.get("libelle") or "doublon").replace("/", "_")[:120]
        if not garder or not copies:
            return jsonify({"erreur": "paramètres manquants"}), 400
        if not cfg.duplicate_dir:
            return jsonify({"erreur": "DUPLICATE_DIR non défini"}), 400

        deplaces, erreurs = [], []
        with runstate.VERROU:
            for copie in copies:
                if copie.get("id") == garder:
                    continue
                chemin = copie.get("chemin")
                if not chemin or not os.path.exists(chemin):
                    erreurs.append(f"introuvable : {chemin}")
                    continue
                dest = os.path.join(cfg.duplicate_dir, libelle,
                                    (copie.get("id") or "x")[:8])
                res = triage.move_aside(chemin, cfg.library_roots, dest, cfg.dry_run)
                if res:
                    runstate.note_deplacement(chemin)
                    deplaces.append(res)
                else:
                    erreurs.append(f"déplacement refusé : {chemin}")
        return jsonify({"ok": not erreurs, "deplaces": deplaces, "erreurs": erreurs,
                        "dry_run": cfg.dry_run})

    @app.post("/api/orphelin")
    def orphelin():
        body = request.get_json(force=True) or {}
        chemin, action = body.get("chemin"), body.get("action")
        if not chemin or action not in ("move",):
            return jsonify({"erreur": "paramètres manquants"}), 400
        if not os.path.exists(chemin):
            return jsonify({"erreur": "chemin introuvable"}), 404
        with runstate.VERROU:
            dest = triage.move_aside(chemin, cfg.library_roots,
                                     cfg.unmatched_dir, cfg.dry_run)
            if dest:
                runstate.note_deplacement(chemin)
        if not dest:
            return jsonify({"erreur": "déplacement refusé"}), 500
        return jsonify({"ok": True, "destination": dest, "dry_run": cfg.dry_run})

    @app.get("/")
    def index():
        return Response(PAGE, mimetype="text/html; charset=utf-8")

    return app


def demarrer(cfg) -> None:
    """Lance le serveur dans un thread démon."""
    try:
        app = creer_app(cfg)
    except Exception as e:
        log.error("Interface web non démarrée : %s", e)
        return

    def _run():
        try:
            from werkzeug.serving import make_server
            srv = make_server(cfg.web_host, cfg.web_port, app, threaded=True)
            srv.serve_forever()
        except Exception as e:
            log.error("Interface web arrêtée : %s", e)

    threading.Thread(target=_run, daemon=True, name="web").start()
    log.info("Interface web : http://<ip-du-nas>:%d  (réseau local, sans mot de passe)",
             cfg.web_port)


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>abs-m4b-tagger — revue</title>
<style>
:root{--bg:#16181d;--card:#1e2129;--line:#2e323c;--txt:#e6e8ec;--dim:#9aa1ad;
--acc:#4f8cff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
gap:16px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:600}
#etat{color:var(--dim);font-size:13px}
nav{display:flex;gap:6px;padding:12px 20px;flex-wrap:wrap}
nav button{background:var(--card);color:var(--txt);border:1px solid var(--line);
padding:7px 14px;border-radius:7px;cursor:pointer;font-size:14px}
nav button.on{background:var(--acc);border-color:var(--acc);color:#fff}
main{padding:0 20px 60px;max-width:1100px}
.item{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:14px;margin-bottom:10px;display:flex;gap:14px;align-items:flex-start}
.item img{width:64px;height:64px;object-fit:cover;border-radius:5px;background:#000;flex:0 0 auto}
.corps{flex:1;min-width:0}
.titre{font-weight:600;margin-bottom:3px}
.meta{color:var(--dim);font-size:13px;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;margin-left:6px}
.b-bad{background:#3d1518;color:var(--bad)}.b-ok{background:#12261a;color:var(--ok)}
.b-warn{background:#332800;color:var(--warn)}
.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
button.act{background:#272b34;color:var(--txt);border:1px solid var(--line);
padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
button.act:hover{border-color:var(--acc)}
button.act.primaire{background:var(--acc);border-color:var(--acc);color:#fff}
button.act:disabled{opacity:.5;cursor:default}
.vide{color:var(--dim);padding:30px 0;text-align:center}
dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);
border-radius:10px;padding:0;width:min(680px,94vw)}
dialog::backdrop{background:#000a}
.dlg-h{padding:14px 18px;border-bottom:1px solid var(--line);font-weight:600}
.dlg-b{padding:14px 18px;max-height:65vh;overflow:auto}
.dlg-f{padding:12px 18px;border-top:1px solid var(--line);text-align:right}
input[type=text]{width:100%;padding:9px;border-radius:6px;border:1px solid var(--line);
background:#14161b;color:var(--txt);font-size:14px;margin-bottom:10px}
.res{border:1px solid var(--line);border-radius:7px;padding:10px;margin-bottom:8px;
display:flex;gap:10px;cursor:pointer}
.res:hover{border-color:var(--acc)}
.res img{width:48px;height:48px;object-fit:cover;border-radius:4px}
.msg{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--card);
border:1px solid var(--acc);padding:11px 20px;border-radius:8px;display:none;z-index:99}
.grp{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:14px;margin-bottom:10px}
.cop{border:1px solid var(--line);border-radius:7px;padding:10px;margin-top:8px}
.cop.sel{border-color:var(--ok);background:#12261a55}
.banniere{display:none;margin:0 20px 10px;padding:10px 14px;border-radius:8px;
background:#332800;color:var(--warn);border:1px solid var(--warn);font-size:14px}
</style></head><body>
<header><h1>Revue de bibliothèque</h1><span id="etat">…</span></header>
<div id="banniere" class="banniere">Une passe est en cours. Les livres que tu mets de
côté maintenant seront ignorés par la passe, mais son rapport ne sera à jour qu'à la
prochaine exécution.</div>
<nav>
  <button data-v="ecarts" class="on">Écarts de durée <span id="n-ecarts"></span></button>
  <button data-v="doublons">Doublons <span id="n-doublons"></span></button>
  <button data-v="non-identifies">Non identifiés <span id="n-non-identifies"></span></button>
  <button data-v="orphelins">Orphelins <span id="n-orphelins"></span></button>
</nav>
<main id="vue"><div class="vide">Chargement…</div></main>
<div class="msg" id="msg"></div>
<dialog id="dlg"><div class="dlg-h" id="dlg-t">Chercher une fiche</div>
<div class="dlg-b"><input type="text" id="q" placeholder="Titre ou ASIN">
<div id="res"></div></div>
<div class="dlg-f"><button class="act" onclick="dlg.close()">Fermer</button></div></dialog>
<script>
const $=s=>document.querySelector(s), vue=$('#vue');
let courant='ecarts', cible=null, donnees={};

function toast(t,ok=true){const m=$('#msg');m.textContent=t;
 m.style.borderColor=ok?'var(--ok)':'var(--bad)';m.style.display='block';
 setTimeout(()=>m.style.display='none',3500);}

async function api(u,opt){const r=await fetch(u,opt);const j=await r.json().catch(()=>({}));
 if(!r.ok) throw new Error(j.erreur||('HTTP '+r.status));return j;}

let passeEnCours=false;
async function etat(){const e=await api('/api/etat');passeEnCours=e.passe_en_cours;
 $('#etat').textContent=(e.passe_en_cours?`passe en cours — ${e.phase} ${e.traites}/${e.total}`:
  'au repos')+(e.dry_run?' · DRY-RUN':'')+` · tolérance ${e.tolerance_pct}% / ${e.plancher_min} min`;
 $('#banniere').style.display=e.passe_en_cours?'block':'none';}

async function charger(v){courant=v;vue.innerHTML='<div class="vide">Chargement…</div>';
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 try{donnees[v]=await api('/api/'+v);}catch(e){vue.innerHTML='<div class="vide">'+e.message+'</div>';return;}
 $('#n-'+v).textContent='('+donnees[v].length+')';
 ({ecarts:rEcarts,doublons:rDoublons,'non-identifies':rNonId,orphelins:rOrph})[v]();}

function rEcarts(){const d=donnees.ecarts;
 if(!d.length){vue.innerHTML='<div class="vide">Aucun écart. Bibliothèque cohérente.</div>';return;}
 vue.innerHTML=d.map((x,i)=>`<div class="item"><img src="/api/cover/${x.id}" onerror="this.style.visibility='hidden'">
 <div class="corps"><div class="titre">${esc(x.titre)}
 <span class="badge ${x.valide?'b-ok':'b-bad'}">${x.valide?'validé':x.ecart_pct+'%'}</span></div>
 <div class="meta">fichier ${x.duree_fichier} · fiche ${x.duree_fiche} · écart ${x.ecart} · ASIN ${x.asin}</div>
 ${x.titre_fiche&&x.titre_fiche!==x.titre?`<div class="meta">la fiche porte le titre « ${esc(x.titre_fiche)} »</div>`:''}
 <div class="meta">${esc(x.chemin)}</div>
 <div class="actions">
 <button class="act ${x.valide?'':'primaire'}" onclick="valider('${x.id}',${!x.valide})">
 ${x.valide?'Retirer la validation':"C'est le bon livre"}</button>
 <button class="act" onclick="ouvrirRecherche('ecarts',${i})">Réidentifier…</button>
 </div></div></div>`).join('');}

function rDoublons(){const d=donnees.doublons;
 if(!d.length){vue.innerHTML='<div class="vide">Aucun doublon.</div>';return;}
 vue.innerHTML=d.map((g,gi)=>`<div class="grp"><div class="titre">${esc(g.titre||'?')}
 <span class="badge b-warn">${esc(g.classe||g.cle)}</span></div>
 <div class="meta">${esc(g.cle)} = ${esc(g.doublon)}</div>
 ${(g.copies||[]).map((c,ci)=>`<div class="cop" id="c${gi}-${ci}">
 <div class="meta">${esc(c.chemin)}</div>
 <div class="meta">${c.taille} · ${c.duree} · ${c.nb_fichiers} fichier(s) · ${(c.formats||[]).join('/')}
 ${c.verification?' · '+esc(c.verification):''}</div>
 <div class="actions"><button class="act" onclick="choisir(${gi},${ci})">Garder celle-ci</button></div>
 </div>`).join('')}
 <div class="actions"><button class="act primaire" id="v${gi}" disabled
 onclick="resoudre(${gi})">Isoler les autres copies</button></div></div>`).join('');}

let choix={};
function choisir(gi,ci){choix[gi]=ci;const g=donnees.doublons[gi];
 g.copies.forEach((_,i)=>$('#c'+gi+'-'+i).classList.toggle('sel',i===ci));
 $('#v'+gi).disabled=false;}

async function resoudre(gi){const g=donnees.doublons[gi],ci=choix[gi];
 if(ci===undefined)return;
 if(!confirm('Les autres copies seront déplacées hors de la bibliothèque. Continuer ?'
  +(passeEnCours?'\n\nUne passe est en cours : son rapport restera figé jusqu\u2019à la suivante.':'')))return;
 try{const r=await api('/api/doublon/resoudre',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({garder:g.copies[ci].id,copies:g.copies,
   libelle:g.cle+'-'+g.doublon})});
  toast(r.dry_run?'DRY-RUN : rien déplacé':(r.deplaces.length+' copie(s) isolée(s)'),!r.erreurs.length);
  if(r.erreurs.length)toast(r.erreurs[0],false);
 }catch(e){toast(e.message,false);}}

function rNonId(){const d=donnees['non-identifies'];
 if(!d.length){vue.innerHTML='<div class="vide">Tous les livres sont identifiés.</div>';return;}
 vue.innerHTML=d.map((x,i)=>`<div class="item"><div class="corps">
 <div class="titre">${esc(x.titre||x.id)}<span class="badge b-warn">${(x.manque||[]).join(', ')}</span></div>
 <div class="meta">${esc(x.chemin||'')}</div>
 <div class="actions"><button class="act primaire"
 onclick="ouvrirRecherche('non-identifies',${i})">Chercher une fiche…</button></div>
 </div></div>`).join('');}

function rOrph(){const d=donnees.orphelins;
 if(!d.length){vue.innerHTML='<div class="vide">Aucun orphelin.</div>';return;}
 vue.innerHTML=d.map((p,i)=>`<div class="item"><div class="corps">
 <div class="meta">${esc(p)}</div><div class="actions">
 <button class="act" onclick="deplacerOrphelin(${i})">Mettre de côté</button>
 </div></div></div>`).join('');}

async function deplacerOrphelin(i){const p=donnees.orphelins[i];
 if(!confirm('Déplacer hors de la bibliothèque ?\n\n'+p))return;
 try{const r=await api('/api/orphelin',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({chemin:p,action:'move'})});
  toast(r.dry_run?'DRY-RUN : rien déplacé':'Déplacé vers '+r.destination);charger('orphelins');
 }catch(e){toast(e.message,false);}}

async function valider(id,actif){
 try{await api('/api/valider',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({id,actif})});
  toast(actif?'Écart validé':'Validation retirée');charger('ecarts');
 }catch(e){toast(e.message,false);}}

// Les titres et chemins contiennent des apostrophes : on passe des index,
// jamais les chaînes elles-mêmes, pour ne pas casser les handlers en ligne.
function ouvrirRecherche(vueNom,i){const x=donnees[vueNom][i];
 cible=x.id;$('#q').value=x.titre||'';$('#res').innerHTML='';
 $('#dlg-t').textContent='Réidentifier : '+(x.titre||x.id);dlg.showModal();chercher();}

let tmr;
$('#q').addEventListener('input',()=>{clearTimeout(tmr);tmr=setTimeout(chercher,600);});

async function chercher(){const q=$('#q').value.trim();if(!q)return;
 $('#res').innerHTML='<div class="vide">Recherche…</div>';
 try{const d=await api('/api/chercher?q='+encodeURIComponent(q));
  $('#res').innerHTML=d.length?d.map(r=>`<div class="res" onclick="appliquer('${r.asin}')">
  <img src="${r.cover}" onerror="this.style.visibility='hidden'">
  <div><div class="titre">${esc(r.titre)} ${r.annee?'('+r.annee+')':''}</div>
  <div class="meta">${esc(r.auteur)}${r.narrateur?' · lu par '+esc(r.narrateur):''}</div>
  <div class="meta">${r.duree} · ${r.asin}${r.serie?' · '+esc(r.serie)+' '+r.tome:''}</div>
  </div></div>`).join(''):'<div class="vide">Aucun résultat</div>';
 }catch(e){$('#res').innerHTML='<div class="vide">'+e.message+'</div>';}}

async function appliquer(asin){
 if(!confirm('Appliquer cette fiche ? Les métadonnées ABS seront remplacées.'))return;
 try{const r=await api('/api/appliquer',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({id:cible,asin})});
  toast('Fiche appliquée : '+r.titre);dlg.close();charger(courant);
 }catch(e){toast(e.message,false);}}

function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>charger(b.dataset.v));
['doublons','non-identifies','orphelins'].forEach(async v=>{
 try{donnees[v]=await api('/api/'+v);$('#n-'+v).textContent='('+donnees[v].length+')';}catch(e){}});
etat();setInterval(etat,10000);charger('ecarts');
</script></body></html>
"""
