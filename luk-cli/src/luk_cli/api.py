"""The only layer the CLI and the MCP server call (spec §5, §7.2).

Every data function takes an `ApiContext` and returns an `Envelope` (`kind` per §5.4) or raises a
`LukError`. Notes for the caller (the `Ubicación:` line and its alternatives, the locale guard, an
unverified private parser, a page past the end) go into `Envelope.warnings`; the CLI prints them on
stderr. Inputs are validated with `luk_cli.inputs` before any request (zero requests on invalid
input), and a §2.2 param is sent only if `config.VERIFIED_PARAMS` lists it.

Public functions use `ctx.anonymous()` (never a cookie); private ones (`saved_jobs`, `applications`,
`cvs`, `whoami`, `session_status(check=True)`, `account_status(check=True)`) use `ctx.private()`,
which re-reads session.json before every request (§4.5). Luk's JSON/HTML is parsed only by
`luk_cli.parsers`, called through the module so tests can fake it.

Wire facts this layer relies on (Phase 0, 2026-09-23 — docs/endpoints.md §4): with no location param
the server applies a geo-IP default, so a search always sends one location mode; `countries[]` works
only together with `worldwide=1`; an unknown job slug answers 410; pages past the last one answer
200 with 0 cards.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import sysconfig
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.parse import parse_qsl, urlencode

import httpx
from pydantic import ValidationError

from luk_cli import __version__, auth, config, parsers, scrub
from luk_cli.config import (
    CACHE_TTL_S, CHILE_AREA_ID, AlgoliaConfig, OptionalParam, Settings, load_settings, santiago_today,
)
from luk_cli.errors import (
    AlgoliaRejected, AuthRequired, Blocked, BudgetExceeded, InvalidArgument, LukError, NetworkError, NotFound,
    RateLimited, SessionBusy, SiteChanged,
)
from luk_cli.http import AlgoliaClient, Fetched, LukClient, Params
from luk_cli.inputs import (
    COUNTRY_PARAM, CURRENCIES, JOB_TYPES, POSTED_WITHIN_PARAM, TargetKind, bounded_int, fold, location_mode,
    normalize_roles, parse_slug_or_url, target_path, target_url, validate_capture_path,
)
from luk_cli.models import (
    KIND_MODELS, AccountStatus, ApplicationList, Area, AreaContext, AreaList, AreaRef, Company, CompanyList,
    CountryCode, Currency, CvList, Envelope, ErrorEnvelope, JobList, JobPosting, Jobs, JobSearch, JobType,
    PostedWithin, RelatedRoles, SearchFilters, SearchQuery, SessionStatus, SimilarRoles, Suggestion, SuggestionList,
    WhoAmI,
)
from luk_cli.ratelimit import RateLimiter
from luk_cli.session import SessionStore, atomic_write, describe_cookies

TEXT_CAP = 4000  # get_jobs: description/requirements chars unless full=True (§7.2)
MAX_PAGES_PER_CALL = 10  # §5.1 pagination stop
SAVED_DETAILS_CAP = 20  # `luk saved --details`
MAX_LIMIT = 150  # --limit of every list command (§5, §5.1)
MAX_JOBS = 10  # get_jobs (§7.2)
MAX_FULL_JOBS = 3  # get_jobs with full=True
MAX_SUGGESTIONS = 20  # luk suggest
MAX_SIMILAR = 9  # similar_roles limit; larger values are unverified
MAX_RELATED_SUGGESTIONS = 5  # related_roles
MAX_OPEN = 5  # luk open
MAX_ALTERNATIVES = 3  # --location alternatives
SAVED_PATH = "/saved_jobs"
APPLICATIONS_PATH = "/profile/application_histories"
CVS_PATH = "/profile/cvs"
LOGIN_INSTRUCTIONS = (
    "To log in, the user runs `luk login` in their own terminal (from Claude Code: in the background): a "
    "browser window opens and they type their own credentials. Never ask for passwords, cookies or tokens."
)
_DEPENDENCIES = ("typer", "rich", "httpx", "pydantic", "selectolax", "platformdirs", "filelock", "playwright")
_CAPTURE_NAME_RE = re.compile(r"[^a-z0-9]+")

T = TypeVar("T")
S = TypeVar("S", bound=str)


@dataclass
class ApiContext:
    """Builds clients from config and the session file. Tests inject transports and `today`.

    Thread-safe (clients are built under a lock). The MCP server builds one per tool call; the rate
    limiter is shared across threads and processes either way.
    """

    settings: Settings
    store: SessionStore
    limiter: RateLimiter
    verbose: bool = False
    luk_transport: httpx.BaseTransport | None = None
    algolia_transport: httpx.BaseTransport | None = None
    today: Callable[[], date] = santiago_today
    _anonymous: LukClient | None = field(default=None, init=False, repr=False)
    _private: LukClient | None = field(default=None, init=False, repr=False)
    _algolia: AlgoliaClient | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_env(cls, *, verbose: bool = False) -> ApiContext:
        """load_settings() + SessionStore + RateLimiter.from_settings()."""
        settings = load_settings()
        return cls(settings, SessionStore(settings), RateLimiter.from_settings(settings), verbose=verbose)

    def anonymous(self) -> LukClient:
        """A cookie-less client for public commands (reused within the context)."""
        with self._lock:
            if self._anonymous is None:
                self._anonymous = LukClient.anonymous(
                    self.settings, self.limiter, transport=self.luk_transport, verbose=self.verbose
                )
            return self._anonymous

    def private(self) -> LukClient:
        """§4.5 reload before each private request: `store.reload(current)`; changed → rebuild the private
        client; missing → AuthRequired with no request. The client's own cookie write-back is its
        `session`, so it never counts as a change."""
        with self._lock:
            current = self._private.session if self._private is not None else None
            latest, changed = self.store.reload(current)
            if self._private is not None and (latest is None or changed):
                self._private.close()
                self._private = None
            if latest is None:
                raise AuthRequired()
            if self._private is None:
                self._private = LukClient.private(
                    self.settings, self.limiter, latest, store=self.store, transport=self.luk_transport,
                    verbose=self.verbose,
                )
            return self._private

    def algolia(self) -> AlgoliaClient:
        """Resolver order: settings.algolia_override → `algolia.json` (24 h) → discovery via GET `/` +
        parsers.parse_algolia_config (cached); refresh=True drops the cache and rediscovers."""
        with self._lock:
            if self._algolia is None:
                self._algolia = AlgoliaClient(
                    self.settings, self._algolia_config, transport=self.algolia_transport, verbose=self.verbose
                )
            return self._algolia

    def _algolia_config(self, refresh: bool) -> AlgoliaConfig:
        if self.settings.algolia_override is not None:
            return self.settings.algolia_override
        path = self.settings.algolia_cache_path
        cached = None if refresh else _read_cache(path)
        if isinstance(cached, dict):
            try:
                return AlgoliaConfig(**cached)
            except (TypeError, InvalidArgument):
                pass
        found = parsers.parse_algolia_config(_ok(self.anonymous().get_html("/")).text)
        _write_cache(path, {"app_id": found.app_id, "api_key": found.api_key, "index": found.index})
        return found

    def close(self) -> None:
        with self._lock:
            for client in (self._anonymous, self._private, self._algolia):
                if client is not None:
                    client.close()
            self._anonymous = self._private = self._algolia = None


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool | None  # None = informational
    detail: str


# -- shared helpers ---------------------------------------------------------------------------------

def _require(param: OptionalParam, what: str) -> None:
    """Refuse a §2.2 feature whose Phase-0 check did not pass (it is never sent)."""
    if param not in config.VERIFIED_PARAMS:
        raise InvalidArgument(f"{what} is not available: Luk's `{param}` parameter is unverified (spec §2.2)")


def _choice(name: str, value: S, allowed: Sequence[S]) -> S:
    if value not in allowed:
        raise InvalidArgument(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _text(value: str, name: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidArgument(f"{name} must not be empty")
    return text


def _ok(fetched: Fetched, *, not_found: str | None = None) -> Fetched:
    """A 200, or the §5.2/§5.3 error for what Luk answered (statuses the client did not classify)."""
    if fetched.status == 200:
        return fetched
    if not_found is not None and fetched.status in (404, 410):
        raise NotFound(f"{not_found} (HTTP {fetched.status})")
    if fetched.status >= 500:
        raise NetworkError(f"Luk answered HTTP {fetched.status} for {fetched.path}")
    raise SiteChanged(f"Luk answered HTTP {fetched.status} for {fetched.path}")


def _landed_slug(fetched: Fetched, kind: TargetKind, slug: str, gone: str) -> str | None:
    """After a redirect to another `kind` page, the slug Luk landed on (None if it is still `slug`);
    NotFound(gone) if the redirect left that page type."""
    if not fetched.redirected:
        return None
    prefix = target_path(kind, "")
    landed = fetched.path.rstrip("/")[len(prefix):] if fetched.path.startswith(prefix) else ""
    try:
        other = parse_slug_or_url(landed, kind)
    except InvalidArgument:
        raise NotFound(gone) from None
    return None if other == slug else other


def _capture_path(path: str, params: Params) -> str:
    """The requested path+query, so a SiteChanged message names `luk debug capture <path>`."""
    return f"{path}?{urlencode(list(params))}" if params else path


def _page_param(page: int) -> list[tuple[str, str | int]]:
    return [("page", page)] if page > 1 else []


def _read_cache(path: Path) -> Any:
    """The `data` of a cache file younger than 24 h, else None (missing, stale, future-dated or corrupt)."""
    try:
        raw = json.loads(path.read_text("utf-8"))
        age = time.time() - float(raw["fetched_at"])
        data = raw["data"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data if 0 <= age < CACHE_TTL_S else None


def _write_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False).encode("utf-8"))


def _envelope(kind: Any, data: Any, warnings: Sequence[str] = ()) -> Envelope[Any]:
    return Envelope(kind=kind, data=data, warnings=list(dict.fromkeys(warnings)))


# -- pagination (§5.1) --------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Page(Generic[T]):
    items: list[T]
    pagination: parsers.Pagination
    warnings: Sequence[str]


# The cursor offset counts results within ONE page (Luk pages hold ≤24), never across the list.
MAX_OFFSET = 49


def _collect(
    fetch: Callable[[int], _Page[T]], key: Callable[[T], str | None], *, start: int, offset: int, limit: int
) -> tuple[list[T], dict[str, Any], list[str]]:
    """Read from result `offset` of page `start` until `limit` items, no next page, or MAX_PAGES_PER_CALL
    pages; dedupe by `key` (first wins; None never dedupes). Returns (items, ListMeta fields except total,
    warnings).

    The cursor (§5.1, §5.4): (`next_page`, `next_offset`) is the position of the first result not
    returned, so continuing there with the same arguments returns every result once. Positions are
    counted on Luk's pages as served (results before `offset` on the start page count as returned, and
    so do repeats of a returned key); the cursor steps over the repeats that follow the last result. A
    page read to its end continues at (page + 1, 0); both are None exactly when nothing is left. A
    stateless cursor cannot remember earlier calls, so an offer Luk lists on two pages comes back
    twice whenever a call boundary falls between its two listings; it is never skipped."""
    items: list[T] = []
    seen: set[str] = set()
    warnings: list[str] = []
    per_page: int | None = None
    last_page: int | None = None
    cursor: tuple[int, int] | None = None
    current, skip, start_count = start, offset, 0
    while True:
        page = fetch(current)
        warnings.extend(page.warnings)
        if per_page is None and page.pagination.has_next:
            per_page = len(page.items)  # measured on a full page, never hardcoded
        if page.pagination.last_page is not None:
            last_page = max(last_page or 0, page.pagination.last_page)
        if current == start:
            start_count = len(page.items)
            if 0 < start_count <= offset:  # the page shrank since the cursor was made (or a typo)
                warnings.append(f"page {start} has only {start_count} results; offset {offset} skipped them all")
        for index, item in enumerate(page.items):
            k = key(item)
            if index >= skip and (k is None or k not in seen):
                if len(items) == limit:
                    cursor = (current, index)
                    break
                items.append(item)
            if k is not None:
                seen.add(k)
        skip = 0
        if cursor is not None or not page.pagination.has_next:
            break
        if len(items) == limit or current - start + 1 >= MAX_PAGES_PER_CALL:
            cursor = (current + 1, 0)
            break
        current += 1
    if start > 1 and start_count == 0:
        past = f" ({last_page})" if last_page is not None and last_page < start else ""
        warnings.append(f"page {start} is past the last page{past}")
    meta = {
        "page": start, "offset": offset, "last_fetched_page": current, "per_page": per_page,
        "last_page": last_page, "has_more": cursor is not None,
        "next_page": cursor[0] if cursor else None, "next_offset": cursor[1] if cursor else None,
    }
    return items, meta, warnings


# -- locations (§5.1) -----------------------------------------------------------------------------------

def _areas(ctx: ApiContext, text: str, context: AreaContext, *, cache: bool = True) -> list[Area]:
    """`/flexible_search/areas?q=` (+ `context=companies`), through the 24 h `areas/` cache."""
    q = _text(text, "location text")
    _choice("context", context, ("jobs", "companies"))
    if context == "companies":
        _require("areas_companies", "area search for companies")
    key = hashlib.sha256(f"{context}\n{q}".encode()).hexdigest()[:32]
    path = ctx.settings.areas_cache_dir / f"{key}.json"
    cached = _read_cache(path) if cache else None
    if isinstance(cached, list):
        try:
            return [Area.model_validate(a) for a in cached]
        except ValidationError:
            pass
    params: list[tuple[str, str | int]] = [("q", q)]
    if context == "companies":
        params.append(("context", "companies"))
    fetched = _ok(ctx.anonymous().get_json("/flexible_search/areas", params))
    areas = parsers.parse_areas(fetched.text, fetched.content_type)
    _write_cache(path, [a.model_dump(mode="json") for a in areas])
    return areas


def _resolve_location(ctx: ApiContext, text: str, context: AreaContext) -> tuple[Area, list[Area]]:
    """Folded exact name match (visitor country first, then server order), else the first result."""
    areas = _areas(ctx, text, context)
    if not areas:
        raise NotFound(f"No Luk area matches '{text.strip()}'")
    wanted = fold(text)
    exact = [a for a in areas if fold(a.name) == wanted]
    chosen = next((a for a in exact if a.visitor_country_match), exact[0] if exact else areas[0])
    return chosen, [a for a in areas if a is not chosen][:MAX_ALTERNATIVES]


def _area_label(area: Area) -> str:
    kind = f" [{area.area_type_label}]" if area.area_type_label else ""
    return f"{area.display_path}{kind} ({area.id})"


def _location_notes(chosen: Area, alternatives: Sequence[Area]) -> list[str]:
    notes = [f"Ubicación: {_area_label(chosen)}"]
    if alternatives:
        notes.append("Alternativas: " + "; ".join(_area_label(a) for a in alternatives))
    return notes


def _ref(area: Area) -> AreaRef:
    return AreaRef(id=area.id, display_path=area.display_path, area_type_label=area.area_type_label)


# -- search (§5.1) --------------------------------------------------------------------------------------

def _search_filters(
    job_types: Sequence[JobType], posted_within: PostedWithin | None, min_salary: int | None,
    max_salary: int | None, currency: Currency | None,
) -> tuple[SearchFilters, list[tuple[str, str | int]]]:
    params: list[tuple[str, str | int]] = []
    types = list(dict.fromkeys(job_types))
    if types:
        _require("job_types", "the job type filter")
        for t in types:
            _choice("job type", t, JOB_TYPES)
        params += [("job_types[]", t) for t in types]
    if posted_within is not None:
        _require("posted_within", "the posted-within filter")
        _choice("posted_within", posted_within, tuple(POSTED_WITHIN_PARAM))
        params.append(("date", POSTED_WITHIN_PARAM[posted_within]))
    bounded = min_salary is not None or max_salary is not None
    if bounded or currency is not None:
        _require("salary", "the salary filter")
        if currency is not None:
            _choice("currency", currency, CURRENCIES)
        if not bounded:
            raise InvalidArgument("a currency needs a minimum or maximum salary")
        for name, bound in (("min_salary", min_salary), ("max_salary", max_salary)):
            if bound is not None:
                params.append((name, bounded_int(name, bound, 1)))
        currency = currency or "CLP"
        params.append(("salary_currency", currency))
    filters = SearchFilters(job_types=types, posted_within=posted_within, min_salary=min_salary,
                            max_salary=max_salary, currency=currency)
    return filters, params


def search_jobs(
    ctx: ApiContext,
    *,
    roles: Sequence[str] = (),
    locations: Sequence[str] = (),
    location_ids: Sequence[int] = (),
    countries: Sequence[CountryCode] = (),
    worldwide: bool = False,
    posted_within: PostedWithin | None = None,
    job_types: Sequence[JobType] = (),
    min_salary: int | None = None,
    max_salary: int | None = None,
    currency: Currency | None = None,
    page: int = 1,
    offset: int = 0,
    limit: int = 15,
) -> Envelope[JobSearch]:
    """kind `job_search` (§5.1): default `locations=1021`; `--location` resolved via areas (24 h cache,
    folded exact match, `visitor_country_match` first) with ≤3 alternatives; `countries[]` always with
    `worldwide=1` (Phase 0); from result `offset` of `page` until `limit` (1–150), no next link, or 10
    pages; dedupe by slug; the `_collect` cursor. `query` echoes what was sent."""
    pills = normalize_roles(roles)
    mode = location_mode(locations=locations, location_ids=location_ids, countries=countries, worldwide=worldwide)
    bounded_int("page", page, 1)
    bounded_int("offset", offset, 0, MAX_OFFSET)
    bounded_int("limit", limit, 1, MAX_LIMIT)
    ids = [bounded_int("location id", i, 1) for i in location_ids]
    codes = [_choice("country", c, tuple(COUNTRY_PARAM)) for c in dict.fromkeys(countries)]
    if mode in ("countries", "worldwide"):
        _require("worldwide", "a search outside the default location")
    if mode == "countries":
        _require("countries", "the country filter")
    filters, filter_params = _search_filters(job_types, posted_within, min_salary, max_salary, currency)

    notes: list[str] = []
    alternatives: list[AreaRef] = []
    location_params: list[tuple[str, str | int]]
    if mode == "default":
        ids = [CHILE_AREA_ID]
        location_params = [("locations", CHILE_AREA_ID)]
    elif mode == "areas":
        resolved: list[int] = []
        for text in locations:
            chosen, others = _resolve_location(ctx, text, "jobs")
            resolved.append(chosen.id)
            alternatives += [_ref(a) for a in others]
            notes += _location_notes(chosen, others)
        ids = list(dict.fromkeys([*resolved, *ids]))
        location_params = [("locations", ",".join(map(str, ids)))]
    else:
        location_params = [("worldwide", 1), *(("countries[]", COUNTRY_PARAM[c]) for c in codes)]
    params = [*([("job_positions", ",".join(pills))] if pills else []), *location_params, *filter_params]

    client = ctx.anonymous()
    pages: list[parsers.SearchPage] = []

    def fetch(number: int) -> _Page[Any]:
        page_params = [*params, *_page_param(number)]
        fetched = _ok(client.get_html("/job_offers", page_params))
        parsed = parsers.parse_search(fetched.text, page=number, today=ctx.today(), base_url=ctx.settings.base_url,
                                      capture_path=_capture_path("/job_offers", page_params))
        pages.append(parsed)
        return _Page(parsed.cards, parsed.pagination, fetched.warnings)

    cards, meta, warnings = _collect(fetch, lambda c: c.slug, start=page, offset=offset, limit=limit)
    head = pages[0]
    query = SearchQuery(roles=pills, location_ids=ids, countries=codes, worldwide=mode in ("countries", "worldwide"),
                        filters=filters)
    data = JobSearch(total=head.total, results=cards, query=query, effective_location=head.effective_location,
                     location_alternatives=alternatives, related_roles=head.related_roles, **meta)
    return _envelope("job_search", data, [*notes, *warnings])


# -- job detail (§5.2) ----------------------------------------------------------------------------------

def _job(ctx: ApiContext, slug: str) -> tuple[JobPosting, list[str]]:
    path = target_path("job", slug)
    fetched = _ok(ctx.anonymous().get_html(path), not_found=f"offer '{slug}' not found on Luk")
    canonical = _landed_slug(fetched, "job", slug,
                             f"offer '{slug}' no longer available (Luk redirected to {fetched.path})")
    parsed = parsers.parse_job(fetched.text, slug=slug, base_url=ctx.settings.base_url, today=ctx.today(),
                               capture_path=path)
    posting = parsed.value if canonical is None else parsed.value.model_copy(update={"canonical_slug": canonical})
    return posting, [*fetched.warnings, *parsed.warnings]


def get_job(ctx: ApiContext, slug_or_url: str) -> Envelope[JobPosting]:
    """kind `job` (§5.2): 404/410 → NotFound; redirect to another /job_offers slug → canonical_slug;
    redirect elsewhere → NotFound "offer no longer available"."""
    posting, warnings = _job(ctx, parse_slug_or_url(slug_or_url, "job"))
    return _envelope("job", posting, warnings)


def _capped(posting: JobPosting) -> JobPosting:
    description, requirements = posting.description_text, posting.requirements_text
    if len(description) <= TEXT_CAP and (requirements is None or len(requirements) <= TEXT_CAP):
        return posting
    return posting.model_copy(update={
        "description_text": description[:TEXT_CAP],
        "requirements_text": requirements[:TEXT_CAP] if requirements is not None else None,
        "text_truncated": True,
    })


def get_jobs(ctx: ApiContext, slugs_or_urls: Sequence[str], *, full: bool = False) -> Envelope[Jobs]:
    """kind `jobs` (MCP get_jobs): 1–10 targets (≤3 with full); texts capped at TEXT_CAP unless full
    (`text_truncated`); missing slugs go to `not_found`."""
    cap = MAX_FULL_JOBS if full else MAX_JOBS
    if not 1 <= len(slugs_or_urls) <= cap:
        raise InvalidArgument(f"get 1 to {MAX_JOBS} jobs per call (1 to {MAX_FULL_JOBS} with full text)")
    slugs = list(dict.fromkeys(parse_slug_or_url(v, "job") for v in slugs_or_urls))
    results: list[JobPosting] = []
    not_found: list[str] = []
    warnings: list[str] = []
    for slug in slugs:
        try:
            posting, notes = _job(ctx, slug)
        except NotFound:
            not_found.append(slug)
            continue
        results.append(posting if full else _capped(posting))
        warnings += notes
    return _envelope("jobs", Jobs(results=results, not_found=not_found), warnings)


# -- suggestions, areas, roles --------------------------------------------------------------------------

def _suggestions(ctx: ApiContext, query: str, limit: int) -> list[Suggestion]:
    """The client already handled retries and 401/403; a 4xx left means the request shape or index that
    Luk's page describes is no longer accepted (SiteChanged), a 5xx is a network error."""
    fetched = ctx.algolia().query_suggestions(query, limit)
    if fetched.status != 200:
        error = NetworkError if fetched.status >= 500 else SiteChanged
        raise error(f"Algolia answered HTTP {fetched.status}")
    return parsers.parse_suggestions(fetched.text, fetched.content_type)[:limit]


def suggest(ctx: ApiContext, prefix: str, *, limit: int = 5) -> Envelope[SuggestionList]:
    """kind `suggestion_list`: Algolia query suggestions, limit 1–20."""
    query = _text(prefix, "prefix")
    bounded_int("limit", limit, 1, MAX_SUGGESTIONS)
    return _envelope("suggestion_list", SuggestionList(query=query, results=_suggestions(ctx, query, limit)))


def find_areas(
    ctx: ApiContext, text: str, *, context: AreaContext = "jobs", limit: int | None = None
) -> Envelope[AreaList]:
    """kind `area_list`: `/flexible_search/areas?q=` (+ `context=companies` if verified); MCP caps at 10."""
    if limit is not None:
        bounded_int("limit", limit, 1)
    query = _text(text, "text")
    return _envelope("area_list", AreaList(query=query, context=context, results=_areas(ctx, query, context)[:limit]))


def _similar(ctx: ApiContext, role: str, limit: int) -> SimilarRoles:
    name = _text(role, "role")
    bounded_int("limit", limit, 1, MAX_SIMILAR)
    params: list[tuple[str, str | int]] = [("role_name", name), ("page", 1), ("ring", 1), ("limit", limit)]
    fetched = _ok(ctx.anonymous().get_json("/job_titles/similar_roles", params))
    return parsers.parse_similar_roles(fetched.text, fetched.content_type, role=name)


def similar_roles(ctx: ApiContext, role: str, *, limit: int = 9) -> Envelope[SimilarRoles]:
    """kind `similar_roles`: `/job_titles/similar_roles?role_name=&page=1&ring=1&limit=` (limit 1–9)."""
    return _envelope("similar_roles", _similar(ctx, role, limit))


def related_roles(ctx: ApiContext, role: str, *, limit: int = 9) -> Envelope[RelatedRoles]:
    """kind `related_roles` (MCP): similar roles + ≤5 Algolia suggestions; an Algolia failure gives
    `suggestions: []` plus a warning (a Luk block or the hourly budget still stops the call)."""
    similar = _similar(ctx, role, limit)
    warnings: list[str] = []
    try:
        suggestions = _suggestions(ctx, similar.role, MAX_RELATED_SUGGESTIONS)
    except (AlgoliaRejected, NetworkError, RateLimited, SiteChanged) as err:
        suggestions = []
        warnings.append(f"query suggestions unavailable: {err.message}")
    data = RelatedRoles(role=similar.role, resolved_name=similar.resolved_name, similar=similar.items,
                        suggestions=suggestions)
    return _envelope("related_roles", data, warnings)


# -- companies ------------------------------------------------------------------------------------------

def search_companies(
    ctx: ApiContext, *, query: str | None = None, location: str | None = None, page: int = 1, offset: int = 0,
    limit: int = 24,
) -> Envelope[CompanyList]:
    """kind `company_list`: `/companies?q=&page=`; limit 1–150; the `_collect` cursor; `location`
    (resolved with `context=companies`) only if verified. No location by default, as on the site."""
    bounded_int("page", page, 1)
    bounded_int("offset", offset, 0, MAX_OFFSET)
    bounded_int("limit", limit, 1, MAX_LIMIT)
    if location is not None:
        _require("companies_location", "the company location filter")
    params: list[tuple[str, str | int]] = [("q", query.strip())] if query and query.strip() else []
    notes: list[str] = []
    if location is not None:
        chosen, others = _resolve_location(ctx, location, "companies")
        params.append(("locations", chosen.id))
        notes = _location_notes(chosen, others)

    client = ctx.anonymous()
    totals: list[int] = []

    def fetch(number: int) -> _Page[Any]:
        page_params = [*params, *_page_param(number)]
        fetched = _ok(client.get_html("/companies", page_params))
        parsed = parsers.parse_companies(fetched.text, base_url=ctx.settings.base_url,
                                         capture_path=_capture_path("/companies", page_params))
        totals.append(parsed.total)
        return _Page(parsed.cards, parsed.pagination, fetched.warnings)

    cards, meta, warnings = _collect(fetch, lambda c: c.slug, start=page, offset=offset, limit=limit)
    return _envelope("company_list", CompanyList(total=totals[0], results=cards, **meta), [*notes, *warnings])


def _landed_page(fetched: Fetched, requested: int) -> int:
    """The `?page=` Luk served after a redirect (none → 1); the requested page when not redirected."""
    if not fetched.redirected:
        return requested
    value = httpx.URL(fetched.url).params.get("page", "1")
    return int(value) if value.isascii() and value.isdigit() and int(value) >= 1 else 1


def get_company(ctx: ApiContext, slug_or_url: str, *, page: int = 1) -> Envelope[Company]:
    """kind `company`: one page of the company's jobs (`?page=N`); unknown slug → NotFound. Luk
    redirects a page past the last one to its last page (live 2026-09-24): the result then carries
    the page Luk served, plus the §5.1 note naming the last page."""
    slug = parse_slug_or_url(slug_or_url, "company")
    bounded_int("page", page, 1)
    path = target_path("company", slug)
    params = _page_param(page)
    fetched = _ok(ctx.anonymous().get_html(path, params), not_found=f"company '{slug}' not found on Luk")
    landed = _landed_slug(fetched, "company", slug, f"company '{slug}' not found on Luk (redirected to {fetched.path})")
    served = _landed_page(fetched, page)
    company = parsers.parse_company(fetched.text, slug=landed or slug, page=served, base_url=ctx.settings.base_url,
                                    today=ctx.today(), capture_path=_capture_path(path, _page_param(served)))
    notes = [f"page {page} is past the last page ({served})"] if served < page else []
    return _envelope("company", company, [*fetched.warnings, *notes])


# -- private pages (§4.5, §5.5) ------------------------------------------------------------------------

def _private_pages(
    ctx: ApiContext, path: str, parse: Callable[[str], parsers.PrivateList[T]], key: Callable[[T], str | None], *,
    page: int, offset: int, limit: int,
) -> tuple[list[T], dict[str, Any], list[str]]:
    bounded_int("page", page, 1)
    bounded_int("offset", offset, 0, MAX_OFFSET)
    bounded_int("limit", limit, 1, MAX_LIMIT)

    def fetch(number: int) -> _Page[T]:
        fetched = _ok(ctx.private().get_html(path, _page_param(number)))
        parsed = parse(fetched.text)
        return _Page(parsed.items, parsed.pagination, [*fetched.warnings, *parsed.warnings])

    return _collect(fetch, key, start=page, offset=offset, limit=limit)


def saved_jobs(
    ctx: ApiContext, *, page: int = 1, offset: int = 0, limit: int = 15, details: bool = False,
    details_limit: int = SAVED_DETAILS_CAP,
) -> Envelope[JobList]:
    """kind `job_list` (private): `/saved_jobs`; `details` adds JobPostings for ≤details_limit cards
    through the anonymous client (public pages never carry the session)."""
    bounded_int("details_limit", details_limit, 1, SAVED_DETAILS_CAP)
    cards, meta, warnings = _private_pages(
        ctx, SAVED_PATH,
        lambda text: parsers.parse_saved_jobs(text, today=ctx.today(), base_url=ctx.settings.base_url),
        lambda c: c.slug, page=page, offset=offset, limit=limit,
    )
    postings: list[JobPosting] | None = None
    not_found: list[str] = []
    if details:
        postings = []
        for card in cards[:details_limit]:
            try:
                posting, notes = _job(ctx, parse_slug_or_url(card.slug, "job"))
            except (InvalidArgument, NotFound):
                not_found.append(card.slug)
                continue
            postings.append(posting)
            warnings += notes
    data = JobList(total=None, results=cards, details=postings, not_found=not_found, **meta)
    return _envelope("job_list", data, warnings)


def applications(ctx: ApiContext, *, page: int = 1, offset: int = 0, limit: int = 15) -> Envelope[ApplicationList]:
    """kind `application_list` (private): `/profile/application_histories`. Rows are never deduped (the
    same offer can appear with several statuses)."""
    rows, meta, warnings = _private_pages(
        ctx, APPLICATIONS_PATH, lambda text: parsers.parse_applications(text, base_url=ctx.settings.base_url),
        lambda a: None, page=page, offset=offset, limit=limit,
    )
    return _envelope("application_list", ApplicationList(total=None, results=rows, **meta), warnings)


def cvs(ctx: ApiContext) -> Envelope[CvList]:
    """kind `cv_list` (private): `/profile/cvs`, metadata only; never downloads files."""
    fetched = _ok(ctx.private().get_html(CVS_PATH))
    parsed = parsers.parse_cvs(fetched.text)
    return _envelope("cv_list", CvList(results=parsed.items), [*fetched.warnings, *parsed.warnings])


def whoami(ctx: ApiContext) -> Envelope[WhoAmI]:
    """kind `whoami` (private): GET `/`; logged in → name/email (meta updated under session.lock)."""
    fetched = _ok(ctx.private().get_html("/"))
    me = parsers.parse_whoami(fetched.text)
    warnings = list(fetched.warnings)
    known = {k: v for k, v in (("name", me.name), ("email", me.email)) if v}
    if known:
        try:
            ctx.store.update_meta(**known)
        except SessionBusy:
            warnings.append("meta.json is busy in another luk process; name/email not saved")
    return _envelope("whoami", me, warnings)


def _validate(ctx: ApiContext) -> None:
    """The §4.3 probe: ONE GET /saved_jobs with the session, never following a redirect (AuthRequired on
    any 3xx), then last_validated_at."""
    _ok(ctx.private().get_html(auth.PROBE_PATH, follow_redirects=False))
    with suppress(SessionBusy):  # best effort, like last_auth_ok_at
        ctx.store.update_meta(last_validated_at=datetime.now(timezone.utc))


def _age_seconds(path: Path) -> int | None:
    try:
        return max(0, int(time.time() - path.stat().st_mtime))
    except FileNotFoundError:
        return None


def session_status(ctx: ApiContext, *, check: bool = False) -> Envelope[SessionStatus]:
    """kind `session_status` (§4.3): local files only (session.describe_cookies — never values);
    `check` adds GET /saved_jobs without following redirects (200 → valid + last_validated_at)."""
    if check:
        _validate(ctx)
    loaded = ctx.store.load()
    meta = ctx.store.load_meta()
    data = SessionStatus(
        path=str(ctx.store.path),
        exists=loaded is not None,
        age_seconds=_age_seconds(ctx.store.path) if loaded is not None else None,
        cookies=describe_cookies(loaded.state) if loaded is not None else [],
        meta=meta,
        last_auth_ok_at=meta.last_auth_ok_at if meta else None,
        last_validated_at=meta.last_validated_at if meta else None,
        valid=True if check else None,
    )
    return _envelope("session_status", data)


def account_status(ctx: ApiContext, *, check: bool = False) -> Envelope[AccountStatus]:
    """kind `account_status` (MCP, §7.2): name/email omitted when settings.mcp_private is False; `check`
    probes /saved_jobs (an expired session is `valid: false`, not an error)."""
    present = ctx.store.exists()
    valid: bool | None = None
    if check:
        valid = False
        if present:
            try:
                _validate(ctx)
                valid = True
            except AuthRequired:
                pass
    meta = ctx.store.load_meta() if present else None
    private = ctx.settings.mcp_private
    data = AccountStatus(
        session_present=present,
        valid=valid,
        name=meta.name if meta and private else None,
        email=meta.email if meta and private else None,
        logged_in_at=meta.logged_in_at if meta else None,
        last_auth_ok_at=meta.last_auth_ok_at if meta else None,
        private_tools_enabled=private,
        login_instructions=LOGIN_INSTRUCTIONS,
    )
    return _envelope("account_status", data)


# -- session commands (§4.2–§4.4) -----------------------------------------------------------------------

def login(
    ctx: ApiContext,
    *,
    browser: auth.BrowserChoice = "auto",
    timeout_s: int | None = None,
    force: bool = False,
    paste_cookie: bool = False,
    notify: Callable[[str], None],
) -> auth.LoginResult:
    """`luk login` → auth.login / auth.paste_cookie."""
    if paste_cookie:
        return auth.paste_cookie(ctx.settings, ctx.store, ctx.limiter, notify=notify, transport=ctx.luk_transport)
    return auth.login(ctx.settings, ctx.store, ctx.limiter, browser=browser, timeout_s=timeout_s, force=force,
                      notify=notify, transport=ctx.luk_transport)


def refresh_session(
    ctx: ApiContext, *, browser: auth.BrowserChoice = "auto", notify: Callable[[str], None]
) -> auth.LoginResult:
    """`luk session refresh` → auth.refresh."""
    return auth.refresh(ctx.settings, ctx.store, ctx.limiter, browser=browser, notify=notify)


def logout(ctx: ApiContext) -> bool:
    """`luk logout` → store.delete(); local-only; True if a session existed."""
    return ctx.store.delete()


def open_targets(
    ctx: ApiContext,
    targets: Sequence[str],
    *,
    company: bool = False,
    opener: Callable[[str], object] = webbrowser.open,
) -> list[str]:
    """`luk open`: 1–5 slug-or-urls → rebuilt `inputs.target_url` only; returns the URLs opened.
    Every target is validated before the first one opens. `webbrowser.open` answers False when no
    browser starts (SSH without a display, a container): those URLs are an InvalidArgument (exit 1)
    naming them for the user to open by hand, never a success."""
    if not 1 <= len(targets) <= MAX_OPEN:
        raise InvalidArgument(f"give 1 to {MAX_OPEN} jobs or companies to open")
    kind: TargetKind = "company" if company else "job"
    urls = [target_url(ctx.settings.base_url, kind, parse_slug_or_url(t, kind)) for t in targets]
    failed = [url for url in urls if opener(url) is False]
    if failed:
        these = "this URL" if len(failed) == 1 else "these URLs"
        raise InvalidArgument(f"could not open a browser — open {these} yourself: {' '.join(failed)}")
    return urls


# -- debug capture (§8.1) --------------------------------------------------------------------------------

def _identity(ctx: ApiContext, html: str) -> scrub.Identity:
    """Name/email from meta.json, completed from the captured page header."""
    meta = ctx.store.load_meta()
    try:
        header = parsers.parse_whoami(html)
    except LukError:
        header = WhoAmI(logged_in=False)
    return scrub.Identity(name=(meta.name if meta else None) or header.name,
                          email=(meta.email if meta else None) or header.email)


def debug_capture(ctx: ApiContext, path: str, *, out_dir: Path | None = None) -> Path:
    """`luk debug capture` (§8.1): inputs.validate_capture_path; GET (private client for private paths, and
    for `/` when a session exists, so the logged-in header is captured); scrub.scrub in memory with the
    identity (public pages are anonymous, so none); writes `<name>.html` + `.meta.json` {path, final_url,
    status, captured_at, luk_cli_version, sha256} under out_dir (default settings.captures_dir)."""
    target = validate_capture_path(path)
    route, _, query = target.partition("?")
    private = route.startswith((SAVED_PATH, "/profile/")) or (route == "/" and ctx.store.exists())
    client = ctx.private() if private else ctx.anonymous()
    fetched = client.get_html(route, parse_qsl(query, keep_blank_values=True))
    identity = _identity(ctx, fetched.text) if private else scrub.Identity(name=None, email=None)
    body = scrub.scrub(fetched.text, path=route, identity=identity).encode("utf-8")

    captured_at = datetime.now(timezone.utc)
    directory = out_dir or ctx.settings.captures_dir
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{_CAPTURE_NAME_RE.sub('-', target.lower()).strip('-')[:80] or 'root'}-{captured_at:%Y%m%dT%H%M%SZ}"
    html_path = directory / f"{stem}.html"
    meta = {
        "path": target, "final_url": fetched.url, "status": fetched.status,
        "captured_at": captured_at.isoformat(timespec="seconds"), "luk_cli_version": __version__,
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    atomic_write(html_path, body)
    atomic_write(directory / f"{stem}.meta.json", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
    return html_path


# -- doctor / MCP config / schemas -----------------------------------------------------------------------

def _dependency(name: str) -> DoctorCheck:
    try:
        return DoctorCheck(name, True, importlib.metadata.version(name))
    except importlib.metadata.PackageNotFoundError:
        return DoctorCheck(name, False, "not installed")


def _mcp_extra() -> DoctorCheck:
    try:
        importlib.import_module("mcp.server.fastmcp")
    except ImportError:
        return DoctorCheck("mcp extra", False, 'not importable: from the repository root run '
                           'python -m pip install -e "./luk-cli[mcp]" (luk-cli is not on PyPI)')
    return DoctorCheck("mcp extra", True, f"mcp {importlib.metadata.version('mcp')}")


def _luk_on_path() -> DoctorCheck:
    found = shutil.which("luk")
    scripts = Path(sysconfig.get_path("scripts")).resolve()
    if found is None:
        return DoctorCheck("luk on PATH", False, f"not found; expected in {scripts}")
    ok = Path(found).resolve().parent == scripts
    return DoctorCheck("luk on PATH", ok, found if ok else f"{found} is not this interpreter's ({scripts})")


def _msedge() -> DoctorCheck:
    candidates = [shutil.which(name) for name in ("msedge", "microsoft-edge", "microsoft-edge-stable")]
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(var):
            candidates.append(str(Path(os.environ[var], "Microsoft", "Edge", "Application", "msedge.exe")))
    candidates.append("/Applications/Microsoft Edge.app")
    found = next((c for c in candidates if c and Path(c).exists()), None)
    return DoctorCheck("msedge", True if found else None, found or "not found (optional login fallback)")


def _bundled_chromium(edge: DoctorCheck) -> DoctorCheck:
    """The Chromium revision this Playwright expects, looked up on disk (Playwright is never imported)."""
    spec = importlib.util.find_spec("playwright")
    if spec is None or spec.origin is None:
        return DoctorCheck("chromium", False, "playwright is not installed")
    try:
        browsers = json.loads((Path(spec.origin).parent / "driver" / "package" / "browsers.json").read_text("utf-8"))
        revision = next(b["revision"] for b in browsers["browsers"] if b["name"] == "chromium")
    except (OSError, ValueError, KeyError, StopIteration):
        return DoctorCheck("chromium", None, "cannot read Playwright's browsers.json")
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root == "0":
        return DoctorCheck("chromium", None, "PLAYWRIGHT_BROWSERS_PATH=0 (package-local browsers; not checked)")
    if root:
        base = Path(root)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"
    folder = base / f"chromium-{revision}"
    if folder.is_dir():
        return DoctorCheck("chromium", True, str(folder))
    install = f'"{sys.executable}" -m playwright install chromium'
    fallback = " (login falls back to msedge)" if edge.ok else ""
    return DoctorCheck("chromium", None if edge.ok else False, f"missing {folder}; run {install}{fallback}")


def _session_check(ctx: ApiContext) -> DoctorCheck:
    try:
        status = session_status(ctx).data
    except LukError as err:
        return DoctorCheck("session", False, err.message)
    if not status.exists:
        return DoctorCheck("session", None, f"none at {status.path} (run `luk login` for private commands)")
    names = ", ".join(c.name for c in status.cookies) or "no cookies"
    return DoctorCheck("session", None, f"{status.path} — {names}; saved {status.age_seconds} s ago")


# Luk (or Algolia) said "stop": after one of these, no further live check may send a request (ADR-0001).
_STOP_LIVE_CHECKS = (Blocked, RateLimited, BudgetExceeded)


def _live(name: str, run: Callable[[], str | None]) -> DoctorCheck:
    """One live check: detail on success, None → skipped, a LukError → failed with its message.

    Blocked / RateLimited / BudgetExceeded propagate so the caller stops every later check.
    """
    try:
        detail = run()
    except _STOP_LIVE_CHECKS:
        raise
    except LukError as err:
        return DoctorCheck(f"live: {name}", False, err.message)
    return DoctorCheck(f"live: {name}", None if detail is None else True, detail or "skipped")


def _live_checks(ctx: ApiContext) -> list[DoctorCheck]:
    found: dict[str, str] = {}

    def discovery() -> str:
        if ctx.settings.algolia_override is not None:
            return "LUK_ALGOLIA_* override set; discovery skipped"
        cfg = ctx._algolia_config(True)
        return f"app {cfg.app_id}, index {cfg.index}"

    def search() -> str:
        data = search_jobs(ctx, roles=["analista"], limit=1).data
        if data.results:
            found["job"] = data.results[0].slug
        return f"{data.total} offers for 'analista' in Chile"

    def job() -> str | None:
        return get_job(ctx, found["job"]).data.title if "job" in found else None

    def companies() -> str:
        data = search_companies(ctx, limit=1).data
        if data.results:
            found["company"] = data.results[0].slug
        return f"{data.total} companies"

    def company() -> str | None:
        return get_company(ctx, found["company"]).data.name if "company" in found else None

    steps: list[tuple[str, Callable[[], str | None]]] = [
        ("algolia discovery", discovery),
        ("search", search),
        ("job detail", job),
        ("areas", lambda: f"{len(_areas(ctx, 'santiago', 'jobs', cache=False))} areas for 'santiago'"),
        ("similar roles", lambda: f"{len(similar_roles(ctx, 'analista').data.items)} roles for 'analista'"),
        ("companies", companies),
        ("company", company),
        ("suggest", lambda: f"{len(suggest(ctx, 'analista').data.results)} suggestions for 'analista'"),
    ]
    checks: list[DoctorCheck] = []
    stopped: LukError | None = None
    for name, run in steps:
        if stopped is not None:  # never send another request after a block / rate limit / spent budget
            checks.append(DoctorCheck(f"live: {name}", None, f"not run: stopped after {stopped.code}"))
            continue
        try:
            checks.append(_live(name, run))
        except _STOP_LIVE_CHECKS as err:
            stopped = err
            checks.append(DoctorCheck(f"live: {name}", False, err.message))
    return checks


def doctor(ctx: ApiContext, *, live: bool = False) -> list[DoctorCheck]:
    """`luk doctor` (§5): interpreter, deps, [mcp] import, `luk` on PATH, bundled Chromium/msedge,
    paths, session status (never values); `live` adds GET `/` (Algolia discovery) + one parse per public page."""
    edge = _msedge()
    checks = [
        DoctorCheck("python", None, f"{sys.executable} (Python {platform.python_version()})"),
        DoctorCheck("luk-cli", None, __version__),
        *(_dependency(name) for name in _DEPENDENCIES),
        _mcp_extra(),
        _luk_on_path(),
        _bundled_chromium(edge),
        edge,
        DoctorCheck("config dir", None, str(ctx.settings.config_dir)),
        DoctorCheck("cache dir", None, str(ctx.settings.cache_dir)),
        _session_check(ctx),
    ]
    return checks + _live_checks(ctx) if live else checks


def mcp_config() -> dict[str, Any]:
    """`{"mcpServers": {"luk": {"type": "stdio", "command": sys.executable, "args": ["-m", "luk_cli", "mcp"]}}}`."""
    return {"mcpServers": {"luk": {"type": "stdio", "command": sys.executable, "args": ["-m", "luk_cli", "mcp"]}}}


def schemas() -> dict[str, dict[str, Any]]:
    """kind → `model_json_schema(mode="serialization")` of its Envelope (plus `error`), for `luk schema` /
    docs/schema/."""
    found: dict[str, dict[str, Any]] = {
        kind: Envelope[model].model_json_schema(mode="serialization")  # type: ignore[valid-type]
        for kind, model in KIND_MODELS.items()
    }
    found["error"] = ErrorEnvelope.model_json_schema(mode="serialization")
    return found
