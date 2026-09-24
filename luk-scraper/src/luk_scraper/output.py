"""Write offers to JSONL or CSV (and read them back).

- JSONL: one JSON object per line, UTF-8, keys in :data:`~luk_scraper.models.FIELDS` order.
  Exact data; the format to feed other tools with.
- CSV: UTF-8 with BOM (``utf-8-sig``) so Excel shows accents correctly; columns in
  :data:`~luk_scraper.models.FIELDS` order. Offer text comes from third parties, so a text
  cell starting with ``=``, ``+``, ``-``, ``@``, tab or CR gets a leading ``'`` to keep
  spreadsheets from running it as a formula (CSV injection).

Files are written to a temporary sibling and then renamed, so an interrupted write never
leaves a truncated file behind.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Iterable, Union

from .models import FIELDS, Offer

JSONL_SUFFIXES = (".jsonl", ".ndjson")
CSV_SUFFIXES = (".csv",)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

OfferLike = Union[Offer, dict]
PathLike = Union[str, os.PathLike]


def _as_dict(offer: OfferLike) -> dict[str, Any]:
    return offer.to_dict() if isinstance(offer, Offer) else {k: offer.get(k) for k in FIELDS}


def format_for(path: PathLike) -> str:
    """``'jsonl'`` or ``'csv'`` from the file extension; ValueError for anything else."""
    suffix = Path(path).suffix.lower()
    if suffix in JSONL_SUFFIXES:
        return "jsonl"
    if suffix in CSV_SUFFIXES:
        return "csv"
    raise ValueError(f"unsupported output extension {suffix or '(none)'!r}: "
                     f"use {', '.join(JSONL_SUFFIXES + CSV_SUFFIXES)}")


def _atomic_write(path: PathLike, write, *, encoding: str, newline: str | None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as fh:
            write(fh)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_jsonl(offers: Iterable[OfferLike], path: PathLike) -> int:
    """Write offers as JSON Lines. Returns the number written."""
    rows = [_as_dict(o) for o in offers]

    def _write(fh):
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    _atomic_write(path, _write, encoding="utf-8", newline="\n")
    return len(rows)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def write_csv(offers: Iterable[OfferLike], path: PathLike) -> int:
    """Write offers as CSV (utf-8-sig, header row). Returns the number written."""
    rows = [{k: _csv_cell(v) for k, v in _as_dict(o).items()} for o in offers]

    def _write(fh):
        writer = csv.DictWriter(fh, fieldnames=list(FIELDS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    _atomic_write(path, _write, encoding="utf-8-sig", newline="")
    return len(rows)


def write(offers: Iterable[OfferLike], path: PathLike) -> int:
    """Write by extension: ``.jsonl``/``.ndjson`` -> JSONL, ``.csv`` -> CSV."""
    if format_for(path) == "csv":
        return write_csv(offers, path)
    return write_jsonl(offers, path)


def read_jsonl(path: PathLike) -> list[dict[str, Any]]:
    """Read a JSONL file back into dicts (blank lines skipped)."""
    with open(path, encoding="utf-8-sig") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_csv(path: PathLike) -> list[dict[str, str]]:
    """Read a CSV written by :func:`write_csv` into dicts of strings (use
    :meth:`Offer.from_dict` to get typed offers back)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))
