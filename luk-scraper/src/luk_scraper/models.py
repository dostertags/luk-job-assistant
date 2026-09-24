"""The exported record: one :class:`Offer` per job offer, keyed by Luk's slug."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now_iso() -> str:
    """Current UTC time, ISO 8601 with offset, second precision: ``2026-09-24T12:00:00+00:00``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Offer:
    """A public job offer.

    Listing-card fields are always attempted; ``description``, ``salary_*``, ``published_at``,
    ``valid_through`` and ``external_id`` come from the detail page (enrichment) and stay None
    when enrichment is off or failed.
    """

    slug: str                               # primary key: Luk's canonical URL key
    title: str = ""
    company: Optional[str] = None
    location_text: Optional[str] = None     # as shown on the card: "Comuna, Provincia, Región, País"
    region: Optional[str] = None            # canonical Chilean region when recognised
    comuna: Optional[str] = None
    country: Optional[str] = None
    workday: Optional[str] = None           # "Jornada Completa", "Jornada Parcial", ...
    modality: Optional[str] = None          # "Presencial", "Remoto", "Híbrido"
    posted_relative: Optional[str] = None   # "Hace 2 horas" (the card has no absolute date)
    url: str = ""                           # canonical detail URL
    description: Optional[str] = None       # plain text (detail)
    salary_min: Optional[int] = None        # detail, only when published and > 0
    salary_max: Optional[int] = None
    published_at: Optional[str] = None      # YYYY-MM-DD, JSON-LD datePosted
    valid_through: Optional[str] = None     # YYYY-MM-DD, JSON-LD validThrough
    external_id: Optional[str] = None       # JSON-LD identifier.value (Luk's numeric id)
    fetched_at: str = field(default_factory=utc_now_iso)  # UTC ISO 8601

    def to_dict(self) -> dict[str, Any]:
        """Plain dict in :data:`FIELDS` order (JSON-ready)."""
        return {name: getattr(self, name) for name in FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Offer":
        """Inverse of :meth:`to_dict`. Unknown keys are ignored; ``""`` becomes None; the salary
        columns are converted back to int (so a CSV row round-trips too)."""
        values: dict[str, Any] = {}
        for name in FIELDS:
            if name not in data:
                continue
            v = data[name]
            if v == "" and name not in ("slug", "title", "url"):
                v = None
            if name in _INT_FIELDS and isinstance(v, str):
                v = int(v) if v.strip().lstrip("-").isdigit() else None
            values[name] = v
        if not values.get("slug"):
            raise ValueError("an offer needs a slug")
        return cls(**values)


#: Column order for CSV (and key order for JSONL).
FIELDS: tuple[str, ...] = tuple(f.name for f in fields(Offer))
_INT_FIELDS = frozenset({"salary_min", "salary_max"})
#: Fields filled from the detail page.
DETAIL_FIELDS = frozenset({"description", "salary_min", "salary_max", "published_at",
                           "valid_through", "external_id"})
