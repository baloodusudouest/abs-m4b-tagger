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

    def update_metadata(self, item_id: str, metadata: dict) -> bool:
        """Écrit les métadonnées d'un item (PATCH /api/items/<id>/media).

        Seules les clés fournies sont modifiées ; les autres sont conservées.
        """
        r = self.s.patch(f"{self.base}/api/items/{item_id}/media",
                         json={"metadata": metadata}, timeout=self.timeout)
        if not r.ok:
            raise AbsError(f"MAJ des métadonnées impossible ({r.status_code}) : "
                           f"{r.text[:200]}")
        return True

    def set_cover_url(self, item_id: str, url: str) -> bool:
        """Demande à ABS de télécharger la pochette depuis une URL."""
        r = self.s.post(f"{self.base}/api/items/{item_id}/cover",
                        json={"url": url}, timeout=self.timeout)
        if not r.ok:
            raise AbsError(f"pochette impossible ({r.status_code})")
        return True

    def delete_item(self, item_id: str) -> bool:
        """Retire un item de la BASE Audiobookshelf. Aucun fichier n'est supprimé."""
        r = self.s.delete(f"{self.base}/api/items/{item_id}", timeout=self.timeout)
        if not r.ok:
            raise AbsError(f"suppression de l'item impossible ({r.status_code})")
        return True

    def search_books(self, title: str, author: str = "", provider: str = "audible.fr") -> list:
        """Interroge un fournisseur de métadonnées VIA Audiobookshelf.

        C'est l'endpoint qu'utilise l'onglet « Chercher » de l'interface. ABS
        relaie la requête vers Audible : aucune clé supplémentaire n'est
        nécessaire, seul le token ABS compte. `title` accepte un ASIN.

        Retourne une liste de dictionnaires ; le champ `duration` y est exprimé
        en MINUTES.
        """
        params = {"title": title, "provider": provider}
        if author:
            params["author"] = author
        url = f"{self.base}/api/search/books"
        try:
            r = self.s.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise AbsError(f"serveur Audiobookshelf injoignable : {e}")
        if r.status_code == 429:
            raise AbsError("429 : trop de requêtes, le fournisseur limite le débit")
        if r.status_code == 401:
            raise AbsError("401 non autorisé : vérifie ABS_TOKEN (clé API).")
        if not r.ok:
            raise AbsError(f"recherche fournisseur impossible ({r.status_code})")
        try:
            data = r.json()
        except ValueError:
            raise AbsError("réponse du fournisseur illisible")
        if isinstance(data, dict):
            data = data.get("results") or data.get("books") or []
        return data if isinstance(data, list) else []

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
