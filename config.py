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
        if self.orphan_action != "none" and not self.library_roots:
            errors.append("ORPHAN_SCAN_DIRS (ou PATH_MAP) est requis pour la détection des orphelins")
        return errors
