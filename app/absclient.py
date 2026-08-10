"""Client minimal de l'API Audiobookshelf."""

import logging
import requests

log = logging.getLogger("abs")


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

    def ping(self) -> str:
        r = self._get("/ping")
        try:
            data = self._get("/api/me").json()
            return data.get("username", "?")
        except Exception:
            return "?" if r.ok else "?"

    # -------------------------------------------------------------- endpoints
    def libraries(self) -> list:
        data = self._get("/api/libraries").json()
        return data.get("libraries", data if isinstance(data, list) else [])

    def library_item_ids(self, library_id: str) -> list:
        """Récupère tous les ids d'items d'une bibliothèque (pagination incluse)."""
        ids, page, limit = [], 0, 500
        while True:
            data = self._get(f"/api/libraries/{library_id}/items",
                             limit=limit, page=page, minified=1).json()
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
        return self._get(f"/api/items/{item_id}", expanded=1).json()

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
