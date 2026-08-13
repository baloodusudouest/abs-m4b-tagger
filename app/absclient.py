"""Client minimal de l'API Audiobookshelf."""

import html as _html
import logging
import os
import re

import requests

log = logging.getLogger("abs")


# --------------------------------------------------------------------- entités
# Audiobookshelf renvoie parfois des entités HTML non décodées dans les champs
# texte (« Jusqu&rsquo;à la fin du monde », « Les Sept S&oelig;urs »). Elles se
# retrouvent telles quelles dans les tags des fichiers et dans les noms de
# dossiers exportés. On les décode dès la sortie de l'API.
#
# Mettre DECODE_HTML_ENTITIES=false pour retrouver l'ancien comportement.
DECODE_HTML_ENTITIES = os.getenv("DECODE_HTML_ENTITIES", "true").strip().lower() not in (
    "false", "0", "no", "off",
)

# Volontairement STRICT : html.unescape() décode aussi les entités sans
# point-virgule final, ce qui transformerait « Fish&notchips » en « Fish¬chips »
# ou « R&Bient » en « R®ient ». On n'accepte que les formes bien fermées.
_ENTITY_RE = re.compile(
    r"&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});"
)

# Champs contenant du HTML légitime : y décoder &lt; et &gt; fabriquerait de
# vraies balises, que STRIP_HTML supprimerait ensuite avec le texte autour.
_MARKUP_KEYS = {"description", "descriptionPlain", "descriptionHtml", "html"}
_MARKUP_ENTITIES = {"lt", "gt", "amp", "#60", "#62", "#38", "#x3c", "#x3e", "#x26"}

# Certaines valeurs arrivent doublement encodées (« &amp;rsquo; »).
_MAX_PASSES = 3


def _sub_entity(m, keep_markup):
    if keep_markup and m.group(1).lower() in _MARKUP_ENTITIES:
        return m.group(0)
    return _html.unescape(m.group(0))


def decode_entities(text, keep_markup=False):
    """Décode les entités HTML bien formées d'une chaîne."""
    if not isinstance(text, str) or "&" not in text:
        return text
    for _ in range(_MAX_PASSES):
        new = _ENTITY_RE.sub(lambda m: _sub_entity(m, keep_markup), text)
        if new == text:
            break
        text = new
    return text


def deep_decode(obj, _key=None):
    """Applique decode_entities() à toutes les chaînes d'une structure JSON."""
    if isinstance(obj, str):
        return decode_entities(obj, keep_markup=(_key in _MARKUP_KEYS))
    if isinstance(obj, dict):
        return {k: deep_decode(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_decode(v, _key) for v in obj]
    return obj


class AbsError(Exception):
    pass


class AbsClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = verify_ssl
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "abs-m4b-tagger/1.0",
        })

    # ------------------------------------------------------------------ utils
    def _get(self, path: str, **params):
        url = f"{self.base}{path}"
        r = self.s.get(url, params=params or None, timeout=self.timeout)
        if r.status_code == 401:
            raise AbsError("401 non autorisé : vérifie ABS_TOKEN (clé API).")
        if r.status_code == 404:
            raise AbsError(f"404 introuvable : {url}")
        r.raise_for_status()
        return r

    def _json(self, path: str, decode: bool = True, **params):
        """GET + parsing JSON, entités HTML décodées au passage.

        decode=False sur les endpoints dont on n'exploite que les identifiants :
        inutile de parcourir toute la structure.
        """
        data = self._get(path, **params).json()
        if decode and DECODE_HTML_ENTITIES:
            return deep_decode(data)
        return data

    def ping(self) -> str:
        r = self._get("/ping")
        try:
            data = self._json("/api/me")
            return data.get("username", "?")
        except Exception:
            return "?" if r.ok else "?"

    # -------------------------------------------------------------- endpoints
    def libraries(self) -> list:
        data = self._json("/api/libraries")
        return data.get("libraries", data if isinstance(data, list) else [])

    def library_item_ids(self, library_id: str) -> list:
        """Récupère tous les ids d'items d'une bibliothèque (pagination incluse)."""
        ids, page, limit = [], 0, 500
        while True:
            data = self._json(f"/api/libraries/{library_id}/items",
                              decode=False, limit=limit, page=page, minified=1)
            results = data.get("results", [])
            for it in results:
                if it.get("mediaType", "book") == "book":
                    ids.append(it["id"])
            total = data.get("total", len(ids))
            page += 1
            if not results or len(ids) >= total or page > 1000:
                break
        return ids

    def item(self, item_id: str) -> dict:
        return self._json(f"/api/items/{item_id}", expanded=1)

    def set_tags(self, item_id: str, tags: list) -> bool:
        """Remplace la liste de tags d'un item (PATCH, avec repli sur batch/update)."""
        payload = sorted(set(t for t in tags if t))
        url = f"{self.base}/api/items/{item_id}/media"
        r = self.s.patch(url, json={"tags": payload}, timeout=self.timeout)
        if r.status_code in (404, 405, 400):
            r = self.s.post(
                f"{self.base}/api/items/batch/update",
                json=[{"id": item_id, "mediaPayload": {"tags": payload}}],
                timeout=self.timeout,
            )
        if not r.ok:
            raise AbsError(f"MAJ des tags impossible ({r.status_code}) : {r.text[:200]}")
        return True

    def delete_item(self, item_id: str) -> bool:
        """Retire un item de la BASE Audiobookshelf. Aucun fichier n'est supprimé."""
        r = self.s.delete(f"{self.base}/api/items/{item_id}", timeout=self.timeout)
        if not r.ok:
            raise AbsError(f"suppression de l'item impossible ({r.status_code})")
        return True

    def cover(self, item_id: str) -> tuple:
        """Retourne (bytes, mime) ou (None, None)."""
        try:
            r = self._get(f"/api/items/{item_id}/cover", raw=1)
        except Exception as e:
            log.debug("Pas de couverture pour %s (%s)", item_id, e)
            return None, None
        content = r.content
        if not content:
            return None, None
        mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return content, mime
