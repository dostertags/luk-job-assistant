"""Which offers to open: read a listings file and turn each row into a Luk apply URL.

Accepted files (format picked by extension):
  - ``.jsonl`` / ``.ndjson``: one JSON object per line, e.g. luk-scraper's JSONL export;
  - ``.csv``: a header row, e.g. luk-scraper's CSV export;
  - ``.json``: a list of objects, or a ``{"data": {"results": [...]}}`` document such as
    ``luk search ... --json`` from luk-cli;
  - anything else (``.txt``): one offer URL or slug per line; blank lines and ``#`` comments are skipped.

Encodings: a UTF-8, UTF-16 (LE/BE) or UTF-32 (LE/BE) byte-order mark picks the encoding (Windows
PowerShell 5.1 writes ``luk ... --json > offers.json`` as UTF-16 LE with a BOM); without one the file
is read as UTF-8. A CSV that is not valid UTF-8 is read as cp1252 (Excel's "CSV" in Spanish Windows),
and its delimiter is sniffed from the header row among ``,``, ``;`` (Excel in Spanish locales) and tab.

Columns / keys used: the slug from ``slug``, ``url``, ``apply_url``, ``detail_url``, ``link`` or
``source_id`` (the first that holds a valid slug or a ``/job_offers/{slug}`` URL); ``title`` and
``company`` for display only. Rows without a slug are skipped; duplicates keep the first row.

The URL opened is always built here, ``https://www.takealuk.com/job_offers/{slug}?tab=apply``, from a
validated slug, never taken verbatim from the file.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

log = logging.getLogger("luk_assist.targets")

LUK_BASE_URL = "https://www.takealuk.com"
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$")
_JOB_PATH_RE = re.compile(r"/job_offers/([^/?#\s]+)")
_SLUG_KEYS = ("slug", "url", "apply_url", "detail_url", "link", "source_id")
_TITLE_KEYS = ("title", "titulo", "cargo")
_COMPANY_KEYS = ("company", "empresa", "company_name")


class TargetsError(Exception):
    """The listings file cannot be read."""


@dataclass(frozen=True)
class Target:
    slug: str
    title: str = ""
    company: str = ""

    @property
    def url(self) -> str:
        return apply_url(self.slug)


def slug_from(value: object) -> str | None:
    """The offer slug in a ``/job_offers/{slug}`` URL or path, or `value` itself if it is a bare slug."""
    text = str(value or "").strip()
    if not text:
        return None
    m = _JOB_PATH_RE.search(text)
    candidate = unquote(m.group(1)) if m else text
    return candidate if _SLUG_RE.match(candidate) else None


def apply_url(slug: str) -> str:
    """slug -> the offer's apply tab (``?tab=apply`` opens "Postular")."""
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(f"not a Luk offer slug: {slug!r}")
    return f"{LUK_BASE_URL}/job_offers/{slug}?tab=apply"


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name") or ""
    if value is None or isinstance(value, (list, tuple, dict)):
        return ""
    return " ".join(str(value).split())


def _first(row: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def target_from_row(row: Mapping[str, Any]) -> Target | None:
    """A `Target` from one record (keys are matched case-insensitively), or None without a slug."""
    low = {str(k or "").strip().lower(): v for k, v in row.items()}
    for key in _SLUG_KEYS:
        value = low.get(key)
        slug = slug_from(value) if isinstance(value, (str, int)) else None
        if slug:
            return Target(slug=slug, title=_first(low, _TITLE_KEYS), company=_first(low, _COMPANY_KEYS))
    return None


# Longest first: the UTF-32 LE BOM starts with the UTF-16 LE one.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_CSV_DELIMITERS = (",", ";", "\t")  # ties go to the first


def read_text(path: Path, *, fallback: str | None = None) -> str:
    """Decode a listings file: by its BOM if it has one, else UTF-8, else `fallback` (if given).

    The "utf-16" / "utf-32" codecs read the BOM for the byte order and drop it.
    """
    data = path.read_bytes()
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if fallback is None:
            raise
        log.info("%s is not UTF-8; reading it as %s", path.name, fallback)
        return data.decode(fallback)


def _lines(text: str) -> list[str]:
    """Split on line ends only (\\r\\n, \\r, \\n): never on U+2028 and friends, which JSON strings may hold."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _rows_jsonl(path: Path) -> Iterator[Mapping[str, Any]]:
    for lineno, line in enumerate(_lines(read_text(path)), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("%s:%d: not JSON, skipped", path.name, lineno)
            continue
        if isinstance(obj, Mapping):
            yield obj


def _rows_json(path: Path) -> Iterator[Mapping[str, Any]]:
    try:
        doc = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise TargetsError(f"{path.name}: not valid JSON (line {exc.lineno})") from exc
    items: Any = doc
    if isinstance(doc, Mapping):
        data = doc.get("data", doc)
        items = data.get("results", data.get("items", [])) if isinstance(data, Mapping) else data
    if not isinstance(items, list):
        raise TargetsError(f"{path.name}: expected a list of offers")
    yield from (item for item in items if isinstance(item, Mapping))


def sniff_delimiter(header: str) -> str:
    """The CSV delimiter used in the header row: the most frequent of , ; and tab outside quotes."""
    unquoted = re.sub(r'"[^"]*"', "", header)
    counts = {d: unquoted.count(d) for d in _CSV_DELIMITERS}
    best = max(_CSV_DELIMITERS, key=lambda d: counts[d])  # max keeps the first on ties
    return best if counts[best] else ","


def _rows_csv(path: Path) -> Iterator[Mapping[str, Any]]:
    text = read_text(path, fallback="cp1252")
    header = next((line for line in _lines(text) if line.strip()), "")
    yield from csv.DictReader(io.StringIO(text, newline=""), delimiter=sniff_delimiter(header))


def _rows_txt(path: Path) -> Iterator[Mapping[str, Any]]:
    for line in _lines(read_text(path)):
        s = line.strip()
        if s and not s.startswith("#"):
            yield {"url": s}


def dedupe(targets: Iterable[Target]) -> list[Target]:
    """Keep the first target per slug, in order."""
    seen: set[str] = set()
    out: list[Target] = []
    for t in targets:
        if t.slug not in seen:
            seen.add(t.slug)
            out.append(t)
    return out


def load_targets(path: Path | str, *, limit: int | None = None) -> list[Target]:
    """Targets from a listings file, deduplicated by slug, at most `limit` of them."""
    p = Path(path)
    if not p.is_file():
        raise TargetsError(f"listings file not found: {p}")
    suffix = p.suffix.lower()
    reader = {".jsonl": _rows_jsonl, ".ndjson": _rows_jsonl, ".json": _rows_json, ".csv": _rows_csv}.get(
        suffix, _rows_txt
    )
    try:
        out = dedupe(t for t in (target_from_row(row) for row in reader(p)) if t is not None)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise TargetsError(f"{p.name}: cannot be read ({exc})") from exc
    return out[:limit] if limit is not None else out
