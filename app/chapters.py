"""Synchronisation optionnelle des chapitres Audiobookshelf vers le fichier m4b.

Nécessite ffmpeg. Le fichier est remuxé en copie de flux (-c copy) : rapide,
sans réencodage, mais le fichier est réécrit (nouvel inode).
"""

import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("chapters")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg_version() -> str:
    """Première ligne de `ffmpeg -version`, ou '' si indisponible."""
    if not shutil.which("ffmpeg"):
        return ""
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                             text=True, timeout=15).stdout
        return out.splitlines()[0].strip() if out else ""
    except Exception:
        return ""


def _escape(text: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        text = text.replace(ch, "\\" + ch if ch != "\n" else " ")
    return text


def build_ffmetadata(chapters: list, total_duration: float) -> str:
    lines = [";FFMETADATA1"]
    for i, ch in enumerate(chapters):
        start = float(ch.get("start", 0))
        end = ch.get("end")
        if end is None:
            end = float(chapters[i + 1]["start"]) if i + 1 < len(chapters) else total_duration
        end = float(end)
        if end <= start:
            continue
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(start * 1000))}",
            f"END={int(round(end * 1000))}",
            f"title={_escape(str(ch.get('title') or f'Chapitre {i + 1}'))}",
        ]
    return "\n".join(lines) + "\n"


def read_existing_chapters(path: str) -> list:
    """Retourne [(start_ms, titre)] du fichier, via ffprobe."""
    import json
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_chapters", path],
            capture_output=True, text=True, check=True, timeout=120).stdout
        data = json.loads(out or "{}")
    except Exception as e:
        log.debug("ffprobe a échoué sur %s : %s", path, e)
        return []
    res = []
    for c in data.get("chapters", []):
        start = int(round(float(c.get("start_time", 0)) * 1000))
        title = (c.get("tags") or {}).get("title", "")
        res.append((start, title))
    return res


def chapters_match(path: str, chapters: list) -> bool:
    existing = read_existing_chapters(path)
    if len(existing) != len(chapters):
        return False
    for (start, title), ch in zip(existing, chapters):
        if abs(start - int(round(float(ch.get("start", 0)) * 1000))) > 1000:
            return False
        if (title or "").strip() != str(ch.get("title") or "").strip():
            return False
    return True


def sync_chapters(path: str, chapters: list, total_duration: float,
                  dry_run: bool = False) -> bool:
    """Réécrit les chapitres du fichier. Retourne True si modifié."""
    if not chapters:
        return False
    if not ffmpeg_available():
        log.warning("ffmpeg absent : synchronisation des chapitres ignorée.")
        return False
    if chapters_match(path, chapters):
        log.debug("Chapitres déjà à jour : %s", os.path.basename(path))
        return False
    if dry_run:
        log.info("      [dry-run] %d chapitres seraient réécrits", len(chapters))
        return False

    folder = os.path.dirname(path)
    st = os.stat(path)
    meta_fd, meta_path = tempfile.mkstemp(suffix=".ffmeta", dir=folder)
    tmp_out = os.path.join(folder, f".{os.path.basename(path)}.tagtmp{os.path.splitext(path)[1]}")
    try:
        with os.fdopen(meta_fd, "w", encoding="utf-8") as fh:
            fh.write(build_ffmetadata(chapters, total_duration))

        def run(maps: list) -> subprocess.CompletedProcess:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-i", path, "-i", meta_path,
                   *maps, "-map_metadata", "0", "-map_chapters", "1",
                   "-c", "copy", tmp_out]
            return subprocess.run(cmd, check=True, capture_output=True, timeout=3600)

        try:
            # Première tentative : tout conserver, y compris la pochette embarquée.
            run(["-map", "0"])
        except subprocess.CalledProcessError as first:
            # Certaines pochettes MP4 ne se remuxent pas (mjpeg mal formé, dimensions
            # absentes). On repart sans le flux image : les tags, pochette comprise,
            # sont réécrits juste après par le tagger.
            log.debug("      remux complet refusé (%s), nouvelle tentative sans pochette",
                      (first.stderr or b"").decode(errors="replace").strip()[:120])
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            run(["-map", "0:a"])
            log.debug("      pochette retirée par ffmpeg, réécrite ensuite par le tagger")

        os.chmod(tmp_out, st.st_mode)
        try:
            os.chown(tmp_out, st.st_uid, st.st_gid)
        except (PermissionError, AttributeError):
            pass
        os.replace(tmp_out, path)
        log.info("      %d chapitres écrits", len(chapters))
        return True
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg a échoué sur %s : %s", path,
                  (e.stderr or b"").decode(errors="replace")[:400])
        return False
    finally:
        for tmp in (meta_path, tmp_out):
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
