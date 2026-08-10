"""Moteur de gabarits pour le nommage des fichiers exportés.

Syntaxe
-------
    {variable}      remplacé par la valeur
    <...>           bloc optionnel : rendu seulement si TOUTES les variables
                    qu'il contient sont renseignées
    /               séparateur de dossiers

Exemple :
    {auteur}/<{serie}/><{annee} - >{titre}<  [{serie} {serie_num}]>< {asin}>< [{region}]>

donne, pour un livre en série :
    J.K. Rowling/Le Monde des sorciers/2017 - Harry Potter à l'École des Sorciers  [Le Monde des sorciers 1] B06Y64F73B [us]

et, pour un livre hors série sans ASIN :
    Bernard Werber/1991 - Les Fourmis
"""

import logging
import os
import re
import unicodedata

log = logging.getLogger("naming")

PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")

# Caractères interdits sous Windows/SMB, et leur remplacement
CHAR_MAP = {
    ":": " -",
    "/": "-",
    "\\": "-",
    "|": "-",
    "?": "",
    "*": "",
    '"': "'",
    "<": "",
    ">": "",
}
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Langue Audiobookshelf -> région Audible
LANG_TO_REGION = {
    "french": "fr", "français": "fr", "francais": "fr", "fr": "fr",
    "english": "us", "anglais": "us", "en": "us",
    "german": "de", "allemand": "de", "deutsch": "de", "de": "de",
    "spanish": "es", "espagnol": "es", "español": "es", "es": "es",
    "italian": "it", "italien": "it", "italiano": "it", "it": "it",
    "japanese": "jp", "japonais": "jp", "jp": "jp", "ja": "jp",
}


# ------------------------------------------------------------------ variables
def region_for(meta, configured: str) -> str:
    """Région Audible : valeur fixe, ou déduite de la langue si 'auto'."""
    if (configured or "").lower() != "auto":
        return (configured or "").lower()
    lang = (meta.language or "").strip().lower()
    return LANG_TO_REGION.get(lang, "us")


def _pad(value: str, width: int = 2) -> str:
    m = re.match(r"^(\d+)(\.\d+)?$", (value or "").strip())
    if not m:
        return (value or "").strip()
    return f"{int(m.group(1)):0{width}d}{m.group(2) or ''}"


def build_vars(meta, region: str = "us", index: int = 1, total: int = 1,
               ext: str = "") -> dict:
    """Construit le dictionnaire de variables disponibles dans les gabarits."""
    return {
        "auteur": meta.author,
        "auteur1": meta.authors[0] if meta.authors else "",
        "auteur_lf": meta.author_lf or meta.author,
        "titre": meta.title,
        "titre_sans_prefixe": meta.title_sort or meta.title,
        "sous_titre": meta.subtitle,
        "serie": meta.series,
        "serie_num": meta.series_part,
        "serie_num2": _pad(meta.series_part, 2),
        "narrateur": meta.narrator,
        "narrateur1": meta.narrators[0] if meta.narrators else "",
        "annee": meta.year,
        "editeur": meta.publisher,
        "asin": meta.asin,
        "isbn": meta.isbn,
        "langue": meta.language,
        "genre": meta.genres[0] if meta.genres else "",
        "genres": "/".join(meta.genres),
        "region": region,
        "piste": str(index),
        "piste2": f"{index:02d}",
        "total": str(total),
        "ext": ext.lstrip("."),
    }


# -------------------------------------------------------------------- rendu
def _render_segment(text: str, values: dict, missing: set) -> tuple:
    """Remplace les {variables} d'un fragment. Retourne (texte, a_des_variables,
    toutes_renseignees)."""
    found = PLACEHOLDER_RE.findall(text)
    complete = True
    for name in found:
        if name not in values:
            missing.add(name)
            complete = False
        elif not str(values[name]).strip():
            complete = False

    def sub(m):
        return str(values.get(m.group(1), "") or "")

    return PLACEHOLDER_RE.sub(sub, text), bool(found), complete


def render(template: str, values: dict) -> str:
    """Rend un gabarit en gérant les blocs optionnels <...>, imbriqués compris."""
    missing = set()
    stack = [{"text": "", "has_var": False, "complete": True}]
    i = 0
    while i < len(template):
        ch = template[i]
        if ch == "<":
            stack.append({"text": "", "has_var": False, "complete": True})
            i += 1
            continue
        if ch == ">" and len(stack) > 1:
            frame = stack.pop()
            keep = frame["complete"] if frame["has_var"] else True
            if keep:
                stack[-1]["text"] += frame["text"]
                stack[-1]["has_var"] = stack[-1]["has_var"] or frame["has_var"]
            i += 1
            continue
        # fragment littéral jusqu'au prochain marqueur
        j = i
        while j < len(template) and template[j] not in "<>":
            j += 1
        text, has_var, complete = _render_segment(template[i:j], values, missing)
        stack[-1]["text"] += text
        stack[-1]["has_var"] = stack[-1]["has_var"] or has_var
        stack[-1]["complete"] = stack[-1]["complete"] and complete
        i = j

    while len(stack) > 1:  # '<' non refermé : on garde le contenu
        frame = stack.pop()
        stack[-1]["text"] += frame["text"]

    if missing:
        log.warning("Variables inconnues dans le gabarit : %s", ", ".join(sorted(missing)))
    return stack[0]["text"]


# --------------------------------------------------------------- assainissement
def sanitize_component(name: str, max_len: int = 180,
                       collapse_spaces: bool = False) -> str:
    """Rend un nom de dossier ou de fichier compatible Windows/SMB/DSM."""
    name = unicodedata.normalize("NFC", str(name or ""))
    for src, dst in CHAR_MAP.items():
        name = name.replace(src, dst)
    name = "".join(c for c in name if ord(c) >= 32)
    if collapse_spaces:
        name = re.sub(r" {2,}", " ", name)
    name = re.sub(r"^[ .]+", "", name)
    name = re.sub(r"[ .]+$", "", name)
    if not name:
        return "Sans titre"
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


def render_path(template: str, values: dict, max_len: int = 180,
                collapse_spaces: bool = False) -> str:
    """Rend un gabarit puis assainit chaque composant du chemin relatif."""
    rendered = render(template, values)
    parts = [p for p in rendered.replace("\\", "/").split("/") if p.strip()]
    clean = [sanitize_component(p, max_len, collapse_spaces) for p in parts]
    return os.path.join(*clean) if clean else "Sans titre"


def render_filename(template: str, values: dict, ext: str, max_len: int = 180,
                    collapse_spaces: bool = False) -> str:
    """Rend un nom de fichier et lui recolle son extension."""
    base = render(template, values).replace("/", "-").replace("\\", "-")
    ext = ext if ext.startswith(".") else f".{ext}" if ext else ""
    base = sanitize_component(base, max(1, max_len - len(ext)), collapse_spaces)
    return base + ext
