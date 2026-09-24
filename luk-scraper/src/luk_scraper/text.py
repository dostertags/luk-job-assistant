"""Pure text helpers: comparison keys, HTML to text, dates, integers and Chilean regions.

No network, no state. :func:`norm_region` maps the many ways Chilean sites write a region
("Región Metropolitana", "Region de Valparaíso", "R.Metropolitana", "Bíobío", "RM") to one of
the 16 canonical names in :data:`REGIONES`.
"""
from __future__ import annotations

import html
import math
import re
import unicodedata
from datetime import date, datetime
from typing import Optional

#: The 16 Chilean regions, canonical spelling.
REGIONES: tuple[str, ...] = (
    "Metropolitana", "Arica y Parinacota", "Tarapacá", "Antofagasta", "Atacama",
    "Coquimbo", "Valparaíso", "O'Higgins", "Maule", "Ñuble", "Bío Bío",
    "La Araucanía", "Los Ríos", "Los Lagos", "Aysén", "Magallanes y Antártica Chilena",
)

#: Country names Luk puts at the end of a location (comparison keys, see :func:`key`).
COUNTRY_KEYS = frozenset({
    "chile", "peru", "colombia", "mexico", "brasil", "brazil", "argentina", "bolivia",
    "ecuador", "uruguay", "paraguay", "venezuela", "espana", "spain", "estadosunidos",
    "unitedstates", "costarica", "panama", "guatemala",
})


def key(value) -> str:
    """Comparison key: lower-case ASCII letters and digits only (no accents, spaces, punctuation).

    >>> key("Bío Bío") == key("Biobío") == "biobio"
    True
    """
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def normalize_space(value) -> str:
    """Collapse runs of whitespace (incl. newlines and NBSP) to one space and strip."""
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


# --- HTML ---------------------------------------------------------------------------------

_BLOCK_END = re.compile(r"(?i)</(?:p|div|li|ul|ol|h[1-6]|tr|table|section)\s*>")


def html_to_text(value) -> str:
    """HTML fragment -> plain text: one line per paragraph, line break or list item (items
    prefixed "- "), entities decoded, no tags, no blank lines."""
    if not isinstance(value, str) or not value.strip():
        return ""
    s = re.sub(r"(?i)<br\s*/?>", "\n", value)
    s = _BLOCK_END.sub("\n", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in s.split("\n"))
    return "\n".join(line for line in lines if line)   # one line per paragraph/item


# --- dates --------------------------------------------------------------------------------

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DMY_FORMATS = ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%y", "%d/%m/%Y")


def parse_date_any(value) -> Optional[str]:
    """``'2026-09-23'`` / ``'2026-09-23T10:00:00-03:00'`` / ``'23/09/26'`` -> ``'2026-09-23'``.

    Returns None for anything else, including impossible dates such as ``2026-13-45``.
    """
    if not isinstance(value, (str, date)):
        return None
    if isinstance(value, date):
        return value.isoformat()[:10]
    s = normalize_space(value)
    if not s:
        return None
    m = _ISO_DATE.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    for fmt in _DMY_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --- numbers ------------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d.,]*")


def to_int(value) -> Optional[int]:
    """Amount or count -> int. ``'$1.200.000'`` -> 1200000, ``1200000.0`` -> 1200000.

    Chilean thousands separators (``.``) and English ones (``,``) are both accepted; a trailing
    1-2 digit decimal part is dropped. Only the FIRST number of a text counts, so
    ``'Entre 800.000 y 1.000.000'`` gives 800000 instead of gluing the digits together.
    Anything that is not a str/int/float (dicts, lists, booleans) gives None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value)) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    m = _NUMBER.search(value)
    if not m:
        return None
    token = m.group(0).rstrip(".,")
    token = re.sub(r"[.,]\d{1,2}$", "", token)   # decimal part: '1.200.000,50' / '1200000.0'
    digits = re.sub(r"\D", "", token)
    return int(digits) if digits else None


# --- regions ------------------------------------------------------------------------------

_REGION_BY_KEY = {key(r): r for r in REGIONES}
_EXACT_ALIASES = {"rm": "Metropolitana", "aisen": "Aysén"}
_CONTAINS_ALIASES = (
    ("magallanes", "Magallanes y Antártica Chilena"),   # "... y de la Antártica Chilena"
    ("antartica", "Magallanes y Antártica Chilena"),
    ("aisen", "Aysén"),
    ("ohiggins", "O'Higgins"),                          # "Libertador General Bernardo O'Higgins"
    ("biobio", "Bío Bío"),
)
# "Región ", "Region de ", "Región del ", "Región de la " (only as a leading word).
_REGION_WORD = re.compile(r"^regi[oó]n\s+(?:del?\s+(?:la\s+)?)?", re.IGNORECASE)
# "R.Metropolitana", "R. Maule", "R Maule": an R prefix followed by a dot or whitespace.
_R_PREFIX = re.compile(r"^R(?:\.\s*|\s+)", re.IGNORECASE)


def strip_region_word(value) -> str:
    """``'Región Metropolitana'`` / ``'Region de Valparaíso'`` -> ``'Metropolitana'`` / ``'Valparaíso'``."""
    s = normalize_space(unicodedata.normalize("NFC", str(value or "")))
    return _REGION_WORD.sub("", s).strip()


def norm_region(value) -> Optional[str]:
    """Any spelling of a Chilean region -> its canonical name in :data:`REGIONES`, else None.

    The "Región" word is stripped first; an ``R.`` prefix only when a dot or whitespace follows,
    so the leading "R" of "Región ..." is never eaten.
    """
    if value is None:
        return None
    raw = normalize_space(unicodedata.normalize("NFC", str(value)))
    if not raw:
        return None
    k = key(raw)
    if k in _EXACT_ALIASES:
        return _EXACT_ALIASES[k]
    cleaned = _R_PREFIX.sub("", strip_region_word(raw)).strip()
    k = key(cleaned)
    if not k or k in COUNTRY_KEYS:
        return None
    if k in _REGION_BY_KEY:
        return _REGION_BY_KEY[k]
    if k in _EXACT_ALIASES:
        return _EXACT_ALIASES[k]
    for alias, canon in _CONTAINS_ALIASES:
        if alias in k:
            return canon
    if len(k) >= 4:                     # 'Metropolitana de Santiago', 'Araucanía', 'Arica'
        for rk, canon in _REGION_BY_KEY.items():
            if rk in k or k in rk:
                return canon
    return None
