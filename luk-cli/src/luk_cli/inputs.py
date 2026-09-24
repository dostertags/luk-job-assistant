"""User-input validation shared by CLI, MCP and api (spec §4.7, §5.1).

Everything here is pure and raises `InvalidArgument` (exit 1 / INVALID_ARGUMENT) before any
request exists. Clients only ever request paths rebuilt by `target_path` from a validated slug.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Literal, get_args
from urllib.parse import urlsplit

from luk_cli.errors import InvalidArgument
from luk_cli.models import CountryCode, Currency, JobType, PostedWithin

TargetKind = Literal["job", "company"]
LocationMode = Literal["default", "areas", "countries", "worldwide"]

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SLUG_LEN = 200
URL_HOSTS = frozenset({"www.takealuk.com", "takealuk.com", "luk.cl", "www.luk.cl"})
RESERVED_COMPANY_SLUGS = frozenset(
    {"home", "pricing", "integrations", "registration", "sign_in", "profile", "autocomplete", "job_offers"}
)
_PREFIX: dict[TargetKind, str] = {"job": "job_offers", "company": "companies"}

MAX_PILL_CHARS = 50  # site JS MAX_PILL_CHARACTERS
MAX_PILLS_BYTES = 512  # job_positions data-max-bytes

POSTED_WITHIN_PARAM: dict[PostedWithin, str] = {
    "24h": "last_day",
    "3d": "last_3_days",
    "1w": "last_week",
    "1m": "last_month",
    "3m": "last_3_months",
    "6m": "last_6_months",
}
COUNTRY_PARAM: dict[CountryCode, str] = {
    "CL": "Chile",
    "CO": "Colombia",
    "MX": "México",
    "PE": "Perú",
    "BR": "Brasil",
}
JOB_TYPES: tuple[JobType, ...] = get_args(JobType)
CURRENCIES: tuple[Currency, ...] = get_args(Currency)

_CAPTURE_PATH_RE = re.compile(
    r"/(job_offers(/[a-z0-9-]+)?|companies(/[a-z0-9-]+)?|saved_jobs|profile/application_histories|profile/cvs)?"
)
_CAPTURE_DENY_RE = re.compile(
    r"sign_out|/auth/|save_later|apply|postul|one_tap|destroy|delete|confirm|unsubscribe", re.IGNORECASE
)


def parse_slug_or_url(value: str, kind: TargetKind) -> str:
    """Return the slug from a bare slug or an https Luk URL of the `kind` page (§4.7)."""
    text = value.strip()
    shown = text[:80]
    slug = _slug_from_url(text, kind) if ":" in text else text
    if len(slug) > MAX_SLUG_LEN or not SLUG_RE.fullmatch(slug):
        raise InvalidArgument(f"not a Luk {kind} slug or URL: '{shown}'")
    if kind == "company" and slug in RESERVED_COMPANY_SLUGS:
        raise InvalidArgument(f"'{slug}' is a reserved Luk path, not a company")
    return slug


def _slug_from_url(text: str, kind: TargetKind) -> str:
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise InvalidArgument("only https:// Luk URLs are accepted")
    if parts.netloc.lower() not in URL_HOSTS:
        raise InvalidArgument("not a Luk URL (www.takealuk.com, takealuk.com, luk.cl or www.luk.cl)")
    prefix = f"/{_PREFIX[kind]}/"
    path = parts.path.rstrip("/")
    if not path.startswith(prefix):
        raise InvalidArgument(f"expected a Luk {prefix}<slug> URL")
    return path[len(prefix):]


def target_path(kind: TargetKind, slug: str) -> str:
    """`/job_offers/<slug>` or `/companies/<slug>`; `slug` must come from parse_slug_or_url."""
    return f"/{_PREFIX[kind]}/{slug}"


def target_url(base_url: str, kind: TargetKind, slug: str) -> str:
    """The only URL `luk open` may pass to the browser."""
    return base_url + target_path(kind, slug)


def normalize_roles(roles: Iterable[str]) -> list[str]:
    """One pill per role: NFC, stripped, no ',', ≤50 chars; joined with ',' ≤512 UTF-8 bytes (§5.1)."""
    pills: list[str] = []
    for raw in roles:
        role = unicodedata.normalize("NFC", raw).strip()
        if not role:
            raise InvalidArgument("empty role")
        if "," in role:
            raise InvalidArgument(f"role '{role[:60]}' contains ',' (the pill delimiter); quote each role separately")
        if len(role) > MAX_PILL_CHARS:
            raise InvalidArgument(f"role '{role[:60]}' is longer than {MAX_PILL_CHARS} characters")
        pills.append(role)
    if len(",".join(pills).encode("utf-8")) > MAX_PILLS_BYTES:
        raise InvalidArgument(f"roles exceed {MAX_PILLS_BYTES} bytes (UTF-8) when joined")
    return pills


def fold(text: str) -> str:
    """Accent/case-insensitive key for area names: NFKD, drop combining marks, casefold ("nunoa" = "Ñuñoa")."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def location_mode(
    *,
    locations: Sequence[str] = (),
    location_ids: Sequence[int] = (),
    countries: Sequence[str] = (),
    worldwide: bool = False,
) -> LocationMode:
    """Exactly one §5.1 location mode; 'default' means the explicit Chile `locations=1021`."""
    modes: list[LocationMode] = []
    if locations or location_ids:
        modes.append("areas")
    if countries:
        modes.append("countries")
    if worldwide:
        modes.append("worldwide")
    if len(modes) > 1:
        raise InvalidArgument("choose one location mode: a location (name or id), countries, or worldwide")
    return modes[0] if modes else "default"


def bounded_int(name: str, value: int, lo: int, hi: int | None = None) -> int:
    """`value` if lo ≤ value (≤ hi), else InvalidArgument naming `name`."""
    if value < lo or (hi is not None and value > hi):
        bounds = f"between {lo} and {hi}" if hi is not None else f"at least {lo}"
        raise InvalidArgument(f"{name} must be {bounds}")
    return value


def validate_capture_path(path: str) -> str:
    """`luk debug capture` target: an allowlisted path (query allowed), never a URL or a mutating path."""
    route = path.split("?", 1)[0]
    if not _CAPTURE_PATH_RE.fullmatch(route) or _CAPTURE_DENY_RE.search(path):
        raise InvalidArgument(
            "capture path not allowed; use /, /job_offers[/<slug>], /companies[/<slug>], "
            "/saved_jobs, /profile/application_histories or /profile/cvs"
        )
    return path
