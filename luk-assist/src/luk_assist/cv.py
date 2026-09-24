"""Seed empty profile answers from your CV (PDF). Best effort, local, and never invents anything.

- `text_from_pdf(path)`: plain text with whichever reader is installed (lazy imports, in order:
  pypdf, PyPDF2, pdfminer.six); '' when none is installed or the PDF has no text layer.
- `profile_from_text(text)`: pure. Returns only keys it actually found in the text:
    experiencia  <- the summary section ("Resumen", "Perfil profesional", "Perfil", "Summary", ...)
    titulo       <- a "Título:" / "Título profesional:" / "Profesión:" line
    comuna       <- a "Comuna:" / "Residencia:" / "Dirección:" / "Ciudad:" line
    idioma       <- ONLY an explicit "Idiomas:" / "Idioma(s):" / "Languages:" line; items that say "no",
                    "ninguno", "sin", "not" or "none" are dropped. A mere mention of English (a school
                    name, a course, "Inglés: No") never becomes an answer.
- `merge(answers, derived)`: fills only profile keys that are empty in your answers file;
  your answers file always wins.

The CV text never leaves your machine: nothing here does network I/O.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from luk_assist.answers import PROFILE_KEYS, Answers, normalize

log = logging.getLogger("luk_assist.cv")

MAX_SUMMARY = 600
MAX_LINE_VALUE = 120

_SUMMARY_HEADING = re.compile(
    r"(?im)^[ \t]*(?:resumen(?:[ \t]+profesional)?|perfil(?:[ \t]+profesional)?|extracto|"
    r"(?:professional[ \t]+)?summary|profile|about[ \t]+me|acerca[ \t]+de[ \t]+m[ií])[ \t]*(?::|$)"
)
_TITLE_LINE = re.compile(r"(?im)^[ \t]*(?:t[ií]tulo(?:[ \t]+profesional)?|profesi[oó]n)[ \t]*[:\-][ \t]*(\S.*)$")
_PLACE_LINE = re.compile(r"(?im)^[ \t]*(?:comuna|residencia|direcci[oó]n|ciudad)[ \t]*[:\-][ \t]*(\S.*)$")
_LANG_LINE = re.compile(r"(?im)^[ \t]*(?:idioma(?:s|\(s\))?|languages?)[ \t]*[:\-][ \t]*(\S.*)$")
_LANG_ITEM_SPLIT = re.compile(r"[,;/|•]")
_NEGATION = re.compile(r"\b(?:no|ningun[oa]?s?|sin|not|none|nada)\b")  # on normalized (accent-free) text
PDF_INSTALL_HINT = (
    "No PDF reader installed. From your luk-job-assistant checkout (luk-assist is not on PyPI):\n"
    '  python -m pip install -U "pip>=21.3"\n'
    '  python -m pip install -e "<path to luk-job-assistant>/luk-assist[pdf]"'
)
_LABEL_VALUE_LINE = re.compile(r"^[^\W\d_][^:\n]{0,30}:[ \t]*\S")
_SECTION_HEADINGS = frozenset(normalize(h) for h in (
    "experiencia", "experiencia laboral", "experiencia profesional", "educación", "formación",
    "formación académica", "estudios", "habilidades", "competencias", "conocimientos", "idiomas",
    "certificaciones", "cursos", "referencias", "contacto", "datos personales", "logros",
    "experience", "work experience", "education", "skills", "languages", "certifications",
    "references", "contact",
))


def _pypdf(path: Path) -> str:
    from pypdf import PdfReader  # type: ignore[import-not-found]

    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def _pypdf2(path: Path) -> str:
    from PyPDF2 import PdfReader  # type: ignore[import-not-found]

    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def _pdfminer(path: Path) -> str:
    from pdfminer.high_level import extract_text  # type: ignore[import-not-found]

    return extract_text(str(path)) or ""


_READERS = (_pypdf, _pypdf2, _pdfminer)


def text_from_pdf(path: Path | str | None) -> str:
    """Plain text of a PDF, or '' (missing file, no reader installed, unreadable or image-only PDF)."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    any_reader = False
    for reader in _READERS:
        try:
            text = reader(p)
        except ImportError:
            continue
        except Exception as exc:  # noqa: BLE001 - a broken PDF must not stop the run
            any_reader = True
            log.debug("%s could not read %s: %s", reader.__name__, p.name, exc)
            continue
        any_reader = True
        if text.strip():
            return text
    if not any_reader:
        log.warning("%s", PDF_INSTALL_HINT)
    return ""


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut at a word boundary to at most `limit` characters."""
    s = " ".join(text.split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    head = cut.rsplit(" ", 1)[0]
    return head if head else cut


def _is_heading(line: str) -> bool:
    return normalize(line).rstrip(":").strip() in _SECTION_HEADINGS or bool(_LABEL_VALUE_LINE.match(line))


def _summary(text: str) -> str:
    m = _SUMMARY_HEADING.search(text)
    if not m:
        return ""
    collected: list[str] = []
    for index, line in enumerate(text[m.end():].split("\n")):
        s = line.strip()
        if not s:
            if collected:
                break
            continue
        if index > 0 and _is_heading(s):
            break  # the next section (or an empty summary followed by one)
        collected.append(s)
    return _clip(" ".join(collected), MAX_SUMMARY)


def _languages(text: str) -> str:
    """The value of an explicit languages line, without negated items; '' otherwise (never a guess)."""
    m = _LANG_LINE.search(text)
    if not m:
        return ""
    value = m.group(1).strip()
    items = [item.strip() for item in _LANG_ITEM_SPLIT.split(value)]
    kept = [item for item in items if item and not _NEGATION.search(normalize(item))]
    if len(kept) == len([item for item in items if item]):
        return _clip(value, MAX_LINE_VALUE)  # nothing negated: keep your own wording
    return _clip(", ".join(kept), MAX_LINE_VALUE)


def profile_from_text(text: str | None) -> dict[str, str]:
    """Profile keys found in the CV text (pure). Only what is actually there; empty dict if nothing."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not t.strip():
        return {}
    out: dict[str, str] = {}
    summary = _summary(t)
    if summary:
        out["experiencia"] = summary
    m = _TITLE_LINE.search(t)
    if m:
        out["titulo"] = _clip(m.group(1), MAX_LINE_VALUE)
    m = _PLACE_LINE.search(t)
    if m:
        out["comuna"] = _clip(m.group(1), MAX_LINE_VALUE)
    languages = _languages(t)
    if languages:
        out["idioma"] = languages
    return {k: v for k, v in out.items() if v}


def merge(answers: Answers, derived: Mapping[str, str]) -> Answers:
    """Fill only EMPTY profile keys from the CV. Your answers file always wins."""
    profile = dict(answers.profile)
    for key, value in derived.items():
        if key in PROFILE_KEYS and value and not profile.get(key):
            profile[key] = value
    return replace(answers, profile=profile)


def seeded_keys(answers: Answers, derived: Mapping[str, str]) -> list[str]:
    """Which keys `merge` would take from the CV (for a one-line report)."""
    return sorted(k for k, v in derived.items() if k in PROFILE_KEYS and v and not answers.profile.get(k))
