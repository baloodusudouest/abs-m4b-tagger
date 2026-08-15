"""Normalisation des métadonnées Audiobookshelf et écriture des tags."""

import hashlib
import html
import io
import json
import logging
import os
import re

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.id3 import (
    ID3, ID3NoHeaderError, APIC, COMM, TALB, TCOM, TCON, TCOP, TDRC, TIT1,
    TIT2, TIT3, TPE1, TPE2, TPUB, TRCK, TSOA, TXXX, WOAF, MVNM, MVIN,
)

log = logging.getLogger("tagger")

AUDIO_EXT = (".m4b", ".m4a", ".mp4", ".mp3")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


# --------------------------------------------------------------------- helpers
def clean_text(value, strip_html: bool = True) -> str:
    if not value:
        return ""
    text = str(value)
    if strip_html:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
        text = _TAG_RE.sub("", text)
        text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _first(seq):
    return seq[0] if seq else None


# ------------------------------------------------------------------- BookMeta
class BookMeta:
    """Vue normalisée d'un livre Audiobookshelf."""

    def __init__(self, item: dict, strip_html: bool = True):
        self.id = item.get("id", "")
        self.path = item.get("path", "")
        self.updated_at = item.get("updatedAt", 0)

        media = item.get("media") or {}
        md = media.get("metadata") or {}

        self.title = clean_text(md.get("title"), strip_html)
        self.title_sort = clean_text(md.get("titleIgnorePrefix"), strip_html)
        self.subtitle = clean_text(md.get("subtitle"), strip_html)

        # clean_text() décode les entités HTML (&oelig;, &euml;, &ocirc;) que les
        # fiches Audible françaises contiennent souvent. Sans ce passage, un
        # « Rapha&euml;l Personnaz » atterrit tel quel dans les tags ET dans les
        # noms de dossiers d'export.
        authors = md.get("authors") or []
        self.authors = [clean_text(a.get("name"), strip_html)
                        for a in authors if a.get("name")]
        if not self.authors and md.get("authorName"):
            self.authors = [clean_text(x, strip_html)
                            for x in str(md["authorName"]).split(",") if x.strip()]
        self.authors = [a for a in self.authors if a]
        self.author_lf = clean_text(md.get("authorNameLF"), strip_html)

        narrators = md.get("narrators") or []
        if isinstance(narrators, str):
            narrators = [narrators]
        self.narrators = [clean_text(n, strip_html) for n in narrators if n]
        if not self.narrators and md.get("narratorName"):
            self.narrators = [clean_text(x, strip_html)
                              for x in str(md["narratorName"]).split(",") if x.strip()]
        self.narrators = [n for n in self.narrators if n]

        series = md.get("series") or []
        if isinstance(series, dict):
            series = [series]
        s0 = _first(series) or {}
        self.series = clean_text(s0.get("name"), strip_html)
        self.series_part = str(s0.get("sequence") or "").strip()
        if not self.series and md.get("seriesName"):
            raw = str(md["seriesName"])
            m = re.match(r"^(.*?)\s+#\s*([\d.]+)$", raw)
            if m:
                self.series, self.series_part = m.group(1).strip(), m.group(2)
            else:
                self.series = raw.strip()

        self.genres = [clean_text(g, strip_html) for g in (md.get("genres") or []) if g]
        self.genres = [g for g in self.genres if g]
        self.tags = [t for t in (media.get("tags") or item.get("tags") or []) if t]
        self.year = str(md.get("publishedYear") or "").strip()
        self.published_date = str(md.get("publishedDate") or "").strip()
        self.publisher = clean_text(md.get("publisher"), strip_html)
        self.description = clean_text(md.get("description"), strip_html)
        self.isbn = str(md.get("isbn") or "").strip()
        self.asin = str(md.get("asin") or "").strip()
        self.language = clean_text(md.get("language"), strip_html)
        self.explicit = bool(md.get("explicit"))

        self.chapters = media.get("chapters") or []

        # Fichiers audio, triés par index
        files = media.get("audioFiles") or []
        self.audio_files = []
        for f in sorted(files, key=lambda x: x.get("index", 0)):
            fmd = f.get("metadata") or {}
            p = fmd.get("path") or f.get("path")
            if p:
                self.audio_files.append({
                    "index": f.get("index", len(self.audio_files) + 1),
                    "path": p,
                    "duration": f.get("duration", 0),
                })

    def refine_authors(self, patterns: list, drop_narrators: bool = True,
                       sort: bool = True) -> list:
        """Nettoie la liste d'auteurs renvoyée par Audiobookshelf.

        Les métadonnées Audible françaises créditent souvent traducteurs et
        narrateurs comme auteurs, et l'ordre varie d'un livre à l'autre : un même
        auteur se retrouve alors dans deux dossiers différents. On retire les
        mentions de rôle, les personnes déjà listées comme narrateurs, puis on
        trie pour garantir un nommage stable.

        Retourne la liste des noms écartés.
        """
        if not self.authors:
            return []
        original = list(self.authors)
        rx = re.compile("|".join(patterns), re.I) if patterns else None
        narrators = {n.strip().lower() for n in self.narrators} if drop_narrators else set()

        kept = []
        for name in original:
            if rx and rx.search(name):
                continue
            if name.strip().lower() in narrators:
                continue
            kept.append(name)

        if not kept:            # tout aurait disparu : on ne touche à rien
            return []
        if sort:
            kept.sort(key=lambda n: n.lower())
        self.authors = kept
        return [n for n in original if n not in kept]

    # ---------------------------------------------------------- champs dérivés
    @property
    def author(self) -> str:
        return ", ".join(self.authors)

    @property
    def narrator(self) -> str:
        return ", ".join(self.narrators)

    @property
    def artist(self) -> str:
        """Auteur(s) + narrateur(s), sans doublon (cf. guide Plex)."""
        seen, out = set(), []
        for name in self.authors + self.narrators:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(name.strip())
        return ", ".join(out)

    @property
    def genre(self) -> str:
        return "/".join(self.genres)

    @property
    def album_sort(self) -> str:
        if self.series:
            part = (self.series_part or "0").strip()
            m = re.match(r"^(\d+)(\.\d+)?$", part)
            if m:  # 1 -> 01, 1.5 -> 01.5, 12 -> 12 (tri alphabétique correct)
                part = f"{int(m.group(1)):02d}{m.group(2) or ''}"
            return f"{self.series} {part} - {self.title}"
        if self.subtitle:
            return f"{self.title} - {self.subtitle}"
        return self.title

    @property
    def content_group(self) -> str:
        if not self.series:
            return ""
        return f"{self.series}, Book {self.series_part}" if self.series_part else self.series

    @property
    def audible_url(self) -> str:
        return f"https://www.audible.com/pd/{self.asin}" if self.asin else ""

    def track_title(self, filename: str, index: int, total: int, mode: str) -> str:
        if total <= 1:
            return self.title
        if mode == "title":
            return self.title
        if mode == "chapter":
            return f"Chapitre {index}"
        return os.path.splitext(os.path.basename(filename))[0]

    def fingerprint(self, cover_bytes: bytes = None) -> str:
        payload = {
            "t": self.title, "st": self.subtitle, "a": self.authors, "n": self.narrators,
            "s": self.series, "sp": self.series_part, "g": self.genres, "y": self.year,
            "p": self.publisher, "d": self.description, "i": self.isbn, "as": self.asin,
            "l": self.language, "c": [(c.get("start"), c.get("title")) for c in self.chapters],
            "f": [f["path"] for f in self.audio_files],
        }
        h = hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
        if cover_bytes:
            h.update(hashlib.sha1(cover_bytes).digest())
        return h.hexdigest()


# ----------------------------------------------------------------- couverture
def prepare_cover(data: bytes, mime: str, max_px: int = 0):
    """Retourne (bytes, format_mp4, mime) ou (None, None, None)."""
    if not data:
        return None, None, None
    fmt = MP4Cover.FORMAT_PNG if "png" in (mime or "") else MP4Cover.FORMAT_JPEG
    out_mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
    if max_px and max_px > 0:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            if max(img.size) > max_px:
                img.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
                return buf.getvalue(), MP4Cover.FORMAT_JPEG, "image/jpeg"
        except Exception as e:  # Pillow absent ou image exotique : on garde l'original
            log.debug("Redimensionnement couverture ignoré : %s", e)
    return data, fmt, out_mime


# -------------------------------------------------------------- écriture MP4
def _ff(value: str) -> MP4FreeForm:
    return MP4FreeForm(str(value).encode("utf-8"))


def build_mp4_tags(meta: BookMeta, filename: str, index: int, total: int, cfg) -> dict:
    tags = {}

    def setv(key, value):
        if value not in (None, "", []):
            tags[key] = value

    setv("\xa9nam", [meta.track_title(filename, index, total, cfg.track_title_mode)])
    setv("\xa9alb", [meta.title])
    setv("\xa9ART", [meta.artist])
    setv("aART", [meta.author])
    setv("\xa9wrt", [meta.narrator])
    setv("\xa9gen", [meta.genre])
    setv("\xa9day", [meta.year])
    setv("\xa9grp", [meta.content_group])
    setv("soal", [meta.album_sort])
    setv("soaa", [meta.author])
    setv("desc", [meta.description[:255]] if meta.description else None)
    setv("ldes", [meta.description])
    if cfg.write_comment:
        setv("\xa9cmt", [meta.description])
    setv("cprt", [f"{meta.year} {meta.publisher}".strip()] if meta.publisher else None)

    tags["stik"] = [2]            # iTunes media type = Audiobook
    tags["pgap"] = [True]         # lecture sans blanc
    tags["trkn"] = [(index, total)]
    if meta.explicit:
        tags["rtng"] = [1]

    if meta.series:
        tags["shwm"] = [True]
        setv("\xa9mvn", [meta.series])
        try:
            tags["\xa9mvi"] = [int(float(meta.series_part))]
        except (ValueError, TypeError):
            pass
        tags["----:com.apple.iTunes:SERIES"] = [_ff(meta.series)]
        if meta.series_part:
            tags["----:com.apple.iTunes:SERIES-PART"] = [_ff(meta.series_part)]

    freeform = {
        "SUBTITLE": meta.subtitle,
        "PUBLISHER": meta.publisher,
        "NARRATOR": meta.narrator,
        "ASIN": meta.asin,
        "ISBN": meta.isbn,
        "LANGUAGE": meta.language,
        "WWWAUDIOFILE": meta.audible_url,
        "ABS_ITEM_ID": meta.id,
    }
    for k, v in freeform.items():
        if v:
            tags[f"----:com.apple.iTunes:{k}"] = [_ff(v)]

    if cfg.extra_tool_tag:
        tags["\xa9too"] = ["abs-m4b-tagger"]
    return tags


def write_mp4(path: str, meta: BookMeta, index: int, total: int, cfg,
              cover=None) -> bool:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    new_tags = build_mp4_tags(meta, path, index, total, cfg)

    # On repart des clés gérées : suppression puis réécriture
    managed_prefixes = ("----:com.apple.iTunes:",)
    managed_keys = {
        "\xa9nam", "\xa9alb", "\xa9ART", "aART", "\xa9wrt", "\xa9gen", "\xa9day",
        "\xa9grp", "soal", "soaa", "desc", "ldes", "\xa9cmt", "cprt", "stik",
        "pgap", "trkn", "rtng", "shwm", "\xa9mvn", "\xa9mvi", "\xa9too",
    }
    for key in list(audio.tags.keys()):
        if key in managed_keys or key.startswith(managed_prefixes):
            del audio.tags[key]

    audio.tags.update(new_tags)

    if cfg.write_cover and cover and cover[0]:
        audio.tags["covr"] = [MP4Cover(cover[0], imageformat=cover[1])]

    if cfg.dry_run:
        return False
    audio.save()
    return True


# -------------------------------------------------------------- écriture MP3
def write_mp3(path: str, meta: BookMeta, index: int, total: int, cfg,
              cover=None) -> bool:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    for frame in ("TIT1", "TALB", "TIT3", "TPE1", "TPE2", "TCOM", "TCON", "TDRC",
                  "COMM", "TSOA", "TPUB", "TCOP", "WOAF", "MVNM", "MVIN", "TRCK", "TIT2"):
        tags.delall(frame)
    for desc in ("SERIES", "SERIES-PART", "ASIN", "ISBN", "SUBTITLE", "NARRATOR",
                 "LANGUAGE", "ABS_ITEM_ID"):
        tags.delall(f"TXXX:{desc}")
    if cfg.write_cover and cover and cover[0]:
        tags.delall("APIC")

    def add(frame, value):
        if value not in (None, "", []):
            tags.add(frame)

    title = meta.track_title(path, index, total, cfg.track_title_mode)
    add(TIT2(encoding=3, text=[title]), title)
    add(TALB(encoding=3, text=[meta.title]), meta.title)
    add(TIT3(encoding=3, text=[meta.subtitle]), meta.subtitle)
    add(TPE1(encoding=3, text=[meta.artist]), meta.artist)
    add(TPE2(encoding=3, text=[meta.author]), meta.author)
    add(TCOM(encoding=3, text=[meta.narrator]), meta.narrator)
    add(TCON(encoding=3, text=[meta.genre]), meta.genre)
    add(TDRC(encoding=3, text=[meta.year]), meta.year)
    add(TSOA(encoding=3, text=[meta.album_sort]), meta.album_sort)
    add(TPUB(encoding=3, text=[meta.publisher]), meta.publisher)
    add(TIT1(encoding=3, text=[meta.content_group]), meta.content_group)
    add(WOAF(url=meta.audible_url), meta.audible_url)
    tags.add(TRCK(encoding=3, text=[f"{index}/{total}"]))
    if meta.publisher:
        tags.add(TCOP(encoding=3, text=[f"{meta.year} {meta.publisher}".strip()]))
    if cfg.write_comment and meta.description:
        tags.add(COMM(encoding=3, lang="fra", desc="", text=[meta.description]))
    if meta.series:
        tags.add(MVNM(encoding=3, text=[meta.series]))
        if meta.series_part:
            tags.add(MVIN(encoding=3, text=[meta.series_part]))

    for desc, value in (("SERIES", meta.series), ("SERIES-PART", meta.series_part),
                        ("ASIN", meta.asin), ("ISBN", meta.isbn),
                        ("SUBTITLE", meta.subtitle), ("NARRATOR", meta.narrator),
                        ("LANGUAGE", meta.language), ("ABS_ITEM_ID", meta.id)):
        if value:
            tags.add(TXXX(encoding=3, desc=desc, text=[value]))

    if cfg.write_cover and cover and cover[0]:
        tags.add(APIC(encoding=3, mime=cover[2], type=3, desc="Cover", data=cover[0]))

    if cfg.dry_run:
        return False
    tags.save(path, v2_version=3)
    return True
