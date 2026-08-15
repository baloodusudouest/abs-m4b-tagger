"""Configuration via variables d'environnement."""

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "oui")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        return default


def _list(name: str, default=None) -> list:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return list(default or [])
    return [x.strip() for x in raw.split(",") if x.strip()]


def _path_map(raw: str) -> list:
    """'/audiobooks:/data/livres;/autre:/data/autre' -> [('/audiobooks','/data/livres'), ...]"""
    pairs = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            continue
        src, dst = chunk.rsplit(":", 1)
        pairs.append((src.rstrip("/"), dst.rstrip("/")))
    # Les préfixes les plus longs d'abord pour éviter les collisions
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


@dataclass
class Config:
    # --- Connexion Audiobookshelf ---
    abs_url: str = ""
    abs_token: str = ""
    verify_ssl: bool = True
    timeout: int = 30

    # --- Sélection ---
    libraries: list = field(default_factory=list)   # noms ou ids, vide = toutes
    only_items: list = field(default_factory=list)  # ids d'items précis

    # --- Chemins ---
    path_map: list = field(default_factory=list)

    # --- Comportement ---
    interval: int = 0          # 0 = one-shot, sinon secondes entre deux passes
    dry_run: bool = False
    force: bool = False        # ignore le cache d'état
    state_file: str = "/config/state.json"

    # --- Tags ---
    write_cover: bool = True
    cover_max_px: int = 1200
    strip_html: bool = True
    write_comment: bool = True
    track_title_mode: str = "filename"   # filename | chapter | title
    sync_chapters: bool = False
    write_sidecars: bool = False         # cover.jpg / desc.txt / reader.txt
    extra_tool_tag: bool = True

    # --- Triage des livres non identifiés ---
    incomplete_checks: list = field(default_factory=lambda: ["identifier", "author", "cover"])
    on_incomplete: str = "tag"            # tag | move | both | none
    incomplete_tag: str = "a-identifier"
    remove_tag_when_complete: bool = True
    tag_incomplete_files: bool = False    # écrire quand même les tags fichiers ?

    # --- Fichiers présents sur disque mais absents d'ABS ---
    orphan_action: str = "report"         # report | move | none
    orphan_scan_dirs: list = field(default_factory=list)
    orphan_min_age_min: int = 30

    unmatched_dir: str = "/a-trier"

    # --- Doublons (plusieurs items ABS pour un même livre) ---
    duplicate_action: str = "report"      # report | move | none
    duplicate_keys: list = field(default_factory=lambda: ["asin"])
    duplicate_dir: str = "/a-trier/doublons"
    duplicate_move_unverified: bool = False   # déplacer même sans vérification

    # --- Vérification des durées auprès du fournisseur (via ABS) ---
    verify_action: str = "report"         # report | none
    verify_provider: str = "audible.fr"
    verify_tolerance_pct: float = 10.0
    verify_min_ecart_min: float = 5.0     # plancher absolu, en minutes
    verify_delay_ms: int = 400            # pause entre deux requêtes sortantes
    verify_max_per_pass: int = 0          # 0 = pas de plafond
    verify_retry_days: int = 30           # réessai des « introuvable »
    verify_accept_tag: str = "duree-ok"   # tag ABS validant un écart connu

    # --- Interface web ---
    web_enable: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8681

    # --- Export des livres validés ---
    export_action: str = "none"          # none | copy | move | hardlink | symlink
    move_cleanup_source: bool = True     # supprimer le dossier source vidé
    after_move: str = "keep"             # keep | remove (retire l'item de la base ABS)
    export_dir: str = "/export"
    export_dir_template: str = (
        "{auteur}/<{serie}/><{annee} - >{titre}"
        "<  [{serie} {serie_num}]>< {asin}>< [{region}]>"
    )
    export_file_template: str = "{titre}"
    export_sidecars: list = field(default_factory=lambda: ["cover", "metadata", "desc", "reader"])
    export_region: str = "us"            # code fixe, ou "auto" d'après la langue
    export_overwrite: bool = False
    export_collapse_spaces: bool = False
    export_max_component: int = 180
    export_prune_stale: bool = True      # purger les fichiers obsolètes après renommage
    export_max_path: int = 255           # longueur totale au-delà de laquelle on alerte

    # --- Nettoyage de la liste d'auteurs ---
    author_exclude: list = field(default_factory=lambda: [
        "traducteur", "traductrice", "traduction", "narrateur", "narratrice",
        "lu par", "adaptation", "illustrateur", "illustratrice", "postface", "préface",
    ])
    author_drop_narrators: bool = True
    author_sort: bool = True
    trust_updated_at: bool = True        # sauter un livre dont updatedAt n'a pas bougé

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            abs_url=os.environ.get("ABS_URL", "").rstrip("/"),
            abs_token=os.environ.get("ABS_TOKEN", "").strip(),
            verify_ssl=_bool("ABS_VERIFY_SSL", True),
            timeout=_int("ABS_TIMEOUT", 30),
            libraries=_list("ABS_LIBRARIES"),
            only_items=_list("ONLY_ITEM_IDS"),
            path_map=_path_map(os.environ.get("PATH_MAP", "")),
            interval=_int("INTERVAL", 0),
            dry_run=_bool("DRY_RUN", False),
            force=_bool("FORCE", False),
            state_file=os.environ.get("STATE_FILE", "/config/state.json"),
            write_cover=_bool("WRITE_COVER", True),
            cover_max_px=_int("COVER_MAX_PX", 1200),
            strip_html=_bool("STRIP_HTML", True),
            write_comment=_bool("WRITE_COMMENT", True),
            track_title_mode=os.environ.get("TRACK_TITLE_MODE", "filename").strip().lower(),
            sync_chapters=_bool("SYNC_CHAPTERS", False),
            write_sidecars=_bool("WRITE_SIDECARS", False),
            extra_tool_tag=_bool("WRITE_TOOL_TAG", True),
            incomplete_checks=[c.lower() for c in _list(
                "INCOMPLETE_CHECKS", ["identifier", "author", "cover"])],
            on_incomplete=os.environ.get("ON_INCOMPLETE", "tag").strip().lower(),
            incomplete_tag=os.environ.get("INCOMPLETE_TAG", "a-identifier").strip(),
            remove_tag_when_complete=_bool("REMOVE_TAG_WHEN_COMPLETE", True),
            tag_incomplete_files=_bool("TAG_INCOMPLETE_FILES", False),
            orphan_action=os.environ.get("ORPHAN_ACTION", "report").strip().lower(),
            orphan_scan_dirs=_list("ORPHAN_SCAN_DIRS"),
            orphan_min_age_min=_int("ORPHAN_MIN_AGE_MIN", 30),
            unmatched_dir=os.environ.get("UNMATCHED_DIR", "/a-trier").rstrip("/"),
            duplicate_action=os.environ.get("DUPLICATE_ACTION", "report").strip().lower(),
            duplicate_keys=[k.lower() for k in _list("DUPLICATE_KEYS", ["asin"])],
            duplicate_dir=os.environ.get("DUPLICATE_DIR", "/a-trier/doublons").rstrip("/"),
            duplicate_move_unverified=_bool("DUPLICATE_MOVE_UNVERIFIED", False),
            verify_action=os.environ.get("VERIFY_ACTION", "report").strip().lower(),
            verify_provider=os.environ.get("VERIFY_PROVIDER", "audible.fr").strip(),
            verify_tolerance_pct=_float("VERIFY_TOLERANCE_PCT", 10.0),
            verify_min_ecart_min=_float("VERIFY_MIN_ECART_MIN", 5.0),
            verify_delay_ms=_int("VERIFY_DELAY_MS", 400),
            verify_max_per_pass=_int("VERIFY_MAX_PER_PASS", 0),
            verify_retry_days=_int("VERIFY_RETRY_DAYS", 30),
            verify_accept_tag=os.environ.get("VERIFY_ACCEPT_TAG", "duree-ok").strip(),
            web_enable=_bool("WEB_ENABLE", True),
            web_host=os.environ.get("WEB_HOST", "0.0.0.0").strip(),
            web_port=_int("WEB_PORT", 8681),
            export_action=os.environ.get("EXPORT_ACTION", "none").strip().lower(),
            move_cleanup_source=_bool("MOVE_CLEANUP_SOURCE", True),
            after_move=os.environ.get("AFTER_MOVE", "keep").strip().lower(),
            export_dir=os.environ.get("EXPORT_DIR", "/export").rstrip("/"),
            export_dir_template=os.environ.get(
                "EXPORT_DIR_TEMPLATE",
                "{auteur}/<{serie}/><{annee} - >{titre}"
                "<  [{serie} {serie_num}]>< {asin}>< [{region}]>"),
            export_file_template=os.environ.get("EXPORT_FILE_TEMPLATE", "{titre}"),
            export_sidecars=[s.lower() for s in _list(
                "EXPORT_SIDECARS", ["cover", "metadata", "desc", "reader"])],
            export_region=os.environ.get("EXPORT_REGION", "us").strip().lower(),
            export_overwrite=_bool("EXPORT_OVERWRITE", False),
            export_collapse_spaces=_bool("EXPORT_COLLAPSE_SPACES", False),
            export_max_component=_int("EXPORT_MAX_COMPONENT", 180),
            export_prune_stale=_bool("EXPORT_PRUNE_STALE", True),
            export_max_path=_int("EXPORT_MAX_PATH", 255),
            author_exclude=_list("AUTHOR_EXCLUDE", [
                "traducteur", "traductrice", "traduction", "narrateur", "narratrice",
                "lu par", "adaptation", "illustrateur", "illustratrice",
                "postface", "préface"]),
            author_drop_narrators=_bool("AUTHOR_DROP_NARRATORS", True),
            author_sort=_bool("AUTHOR_SORT", True),
            trust_updated_at=_bool("TRUST_UPDATED_AT", True),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )

    @property
    def library_roots(self) -> list:
        """Racines locales à scanner : ORPHAN_SCAN_DIRS, sinon les cibles de PATH_MAP."""
        if self.orphan_scan_dirs:
            return [d.rstrip("/") for d in self.orphan_scan_dirs]
        return [dst for _, dst in self.path_map]

    def map_path(self, abs_path: str) -> str:
        """Traduit un chemin vu par Audiobookshelf en chemin vu par ce conteneur."""
        if not abs_path:
            return abs_path
        norm = abs_path.rstrip("/") if abs_path != "/" else abs_path
        for src, dst in self.path_map:
            if norm == src:
                return dst
            if abs_path.startswith(src + "/"):
                return dst + abs_path[len(src):]
        return abs_path

    def validate(self) -> list:
        errors = []
        if not self.abs_url:
            errors.append("ABS_URL est obligatoire (ex: http://192.168.1.90:13378)")
        if not self.abs_token:
            errors.append("ABS_TOKEN est obligatoire (clé API Audiobookshelf)")
        if self.track_title_mode not in ("filename", "chapter", "title"):
            errors.append("TRACK_TITLE_MODE doit valoir filename, chapter ou title")
        if self.on_incomplete not in ("tag", "move", "both", "none"):
            errors.append("ON_INCOMPLETE doit valoir tag, move, both ou none")
        if self.orphan_action not in ("report", "move", "none"):
            errors.append("ORPHAN_ACTION doit valoir report, move ou none")
        if "move" in (self.on_incomplete, self.orphan_action) or self.on_incomplete == "both":
            if not self.unmatched_dir:
                errors.append("UNMATCHED_DIR est obligatoire pour déplacer des livres")
        if self.duplicate_action not in ("report", "move", "none"):
            errors.append("DUPLICATE_ACTION doit valoir report, move ou none")
        if self.duplicate_action != "none":
            from duplicates import KEY_CHOICES
            bad = [k for k in self.duplicate_keys if k not in KEY_CHOICES]
            if bad:
                errors.append(f"DUPLICATE_KEYS : valeurs inconnues {bad} "
                              f"(choix : {', '.join(KEY_CHOICES)})")
            if not self.duplicate_keys:
                errors.append("DUPLICATE_KEYS ne peut pas être vide")
        if self.duplicate_action == "move":
            if not self.duplicate_dir:
                errors.append("DUPLICATE_DIR est obligatoire quand DUPLICATE_ACTION=move")
            for root in self.library_roots:
                if self.duplicate_dir == root or self.duplicate_dir.startswith(root + "/"):
                    errors.append(
                        f"DUPLICATE_DIR ({self.duplicate_dir}) est à l'intérieur de la "
                        f"bibliothèque ({root}) : Audiobookshelf rescannerait les doublons "
                        f"mis de côté")
        if self.verify_action not in ("report", "none"):
            errors.append("VERIFY_ACTION doit valoir report ou none")
        if self.verify_action != "none":
            if not self.verify_provider:
                errors.append("VERIFY_PROVIDER ne peut pas être vide")
            if not 0 < self.verify_tolerance_pct < 100:
                errors.append("VERIFY_TOLERANCE_PCT doit être compris entre 0 et 100")
            if self.verify_min_ecart_min < 0:
                errors.append("VERIFY_MIN_ECART_MIN ne peut pas être négatif")
            if (self.verify_accept_tag and self.incomplete_tag
                    and self.verify_accept_tag == self.incomplete_tag):
                errors.append("VERIFY_ACCEPT_TAG et INCOMPLETE_TAG doivent différer : "
                              "le second est posé et retiré automatiquement")
        if self.orphan_action != "none" and not self.library_roots:
            errors.append("ORPHAN_SCAN_DIRS (ou PATH_MAP) est requis pour la détection des orphelins")
        if self.export_action not in ("none", "copy", "move", "hardlink", "symlink"):
            errors.append("EXPORT_ACTION doit valoir none, copy, move, hardlink ou symlink")
        if self.after_move not in ("keep", "remove"):
            errors.append("AFTER_MOVE doit valoir keep ou remove")
        if self.export_action != "none":
            if not self.export_dir:
                errors.append("EXPORT_DIR est obligatoire quand EXPORT_ACTION n'est pas 'none'")
            if not self.export_dir_template.strip():
                errors.append("EXPORT_DIR_TEMPLATE ne peut pas être vide")
            if not self.export_file_template.strip():
                errors.append("EXPORT_FILE_TEMPLATE ne peut pas être vide")
            for label, tpl in (("EXPORT_DIR_TEMPLATE", self.export_dir_template),
                               ("EXPORT_FILE_TEMPLATE", self.export_file_template)):
                if "{" not in tpl:
                    errors.append(
                        f"{label} ne contient aucune variable : « {tpl} ». Les variables "
                        f"s'écrivent entre accolades, par exemple {{titre}}. Tous les "
                        f"livres porteraient sinon le même nom.")
            for root in self.library_roots:
                if self.export_dir == root or self.export_dir.startswith(root + "/"):
                    errors.append(
                        f"EXPORT_DIR ({self.export_dir}) est à l'intérieur de la bibliothèque "
                        f"source ({root}) : Audiobookshelf rescannerait les copies")
            bad = [s for s in self.export_sidecars if s not in
                   ("cover", "metadata", "desc", "reader", "nfo")]
            if bad:
                errors.append(f"EXPORT_SIDECARS : valeurs inconnues {bad}")
        return errors
