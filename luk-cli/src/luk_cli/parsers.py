"""Pure parsers: Luk HTML/JSON → models (spec §2, §5.1, §5.2, §5.5). selectolax only.

Reverse-engineered page mechanism — verified live 2026-09-23 (anonymous recon + Phase 0, docs/endpoints.md):
- search `/job_offers`: frame `turbo-frame#job_offers_results`; total = integer in
  `.job-offers-results-count b`; 15 cards `div.job-offer-card[data-scroll-restore-slug]` (relevance
  order); title `h2.job-offer-card__title` (collapse whitespace); company `p.item-title` (empty →
  null); location `span.break-words`; salary `span.tag-primary` ("CLP $650.000 - $850.000", COP with a
  decimal comma "COP $2.150.000,00 - $2.150.000,00"); labels `span.tag-navy` (Jornada Completa,
  Presencial, Prácticas, "30 horas"…); age = the `.item-subtitle > span` starting "Hace" (may be
  absent). Pagination `nav.pagination-nav` inside the frame: other pages `a[aria-label="Página N"]`,
  the current page an unlabelled `span[aria-current="page"]` (so on the last page the highest label
  is N-1: last_page = max(labels, current)); next `a[aria-label="Página siguiente"]` (a span when
  disabled); ≤15 results → no nav (one page). Locale footer links sit outside the nav. Past the last
  page: 200, same total, 0 cards. Zero results: total 0, no cards, nav or pills. Related roles
  `a.similar-role-suggestion-pill[data-role-name]`; applied location `input#locations` (`value` +
  `data-initial-area-options` JSON; worldwide=1 → value "" and no such attribute).
- job detail `/job_offers/{slug}`: JSON-LD `JobPosting` + BreadcrumbList (position 3 = company);
  DOM `.job-offer-show-title h1`, company `.job-offer-show-title a[href^="/companies/"]`; the `.card`
  around the title block holds the full location `span.break-words`, `span.tag-navy` labels, the
  salary tag, "N vacante(s)" and the age; `h3` Descripción / Requerimientos are each followed by a
  block holding `[data-text-toggle-target="fullText"]`. Nav/footer `/companies/home`,
  `/integrations`, `/pricing` are never the company. An unknown slug answers 410 with a bare error
  page (the api maps 404/410 before parsing); no closed-offer marker has been observed, so status
  is "expired" (validThrough < today) or "open".
- companies `/companies`: frame `turbo-frame#companies_marketplace_results`, total in its first
  `b.text-primary`; 24 cards `a.company-card[href^="/companies/"]` — 24 hidden
  `div.company-card.loading-company` skeletons come first and are never selected. A real card may
  have an empty `.company-card__name`.
- company `/companies/{slug}`: `h1`; location `div.text-caption.text-gray span` (bare
  `.text-caption span` hits the breadcrumb "›"); count `#section-job-offers h2.subsection-header`;
  20 search cards per page inside `turbo-frame#company_job_offers_results`, whose nav pages `?page=N`.
- areas / similar roles: JSON (`areas` list; `items` list + `resolved_name` + `next_page`); Algolia:
  `results[0].hits[]` as `{query, popularity}`.
- root `/`: Algolia config `[data-search-pills-application-id-value]`,
  `[data-search-pills-search-api-key-value]`, `input[data-query-suggestions-index]`; anonymous marker
  `header#main-header a[href="/users/sign_in"]`; logged-in markers `[aria-label^="Avatar de"]`,
  `#header-user-menu .header-dropdown-name`, `.header-dropdown-email` (unverified — seen during design, never captured).
- private pages (`/saved_jobs`, `/profile/application_histories`, `/profile/cvs`): logged-in markup
  NOT verified yet. Their parsers are multi-strategy (the search-card partial, then offer links or
  `.<word>-*` boxes outside header/nav/footer), keep raw text only, warn, and accept 0 items.

Contract for every parser: pure (no I/O, no clock — callers pass `today = config.santiago_today()`);
a missing required anchor (§5.5) raises `missing_anchor(...)` (Blocked for a challenge page,
SiteChanged otherwise); a missing optional field is None/[]; JSON parsers take (body, content_type)
and raise SiteChanged on non-JSON or a missing key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser, Node

from luk_cli.config import DEFAULT_ALGOLIA_INDEX, AlgoliaConfig
from luk_cli.errors import AuthRequired, Blocked, InvalidArgument, LukError, SiteChanged
from luk_cli.http import ANONYMOUS_MARKER, looks_like_challenge
from luk_cli.inputs import (
    MAX_SLUG_LEN,
    RESERVED_COMPANY_SLUGS,
    SLUG_RE,
    URL_HOSTS,
    fold,
    target_url,
)
from luk_cli.models import (
    Address,
    Application,
    Area,
    AreaRef,
    Company,
    CompanyCard,
    Cv,
    JobCard,
    JobPosting,
    JobType,
    Modality,
    Salary,
    SimilarRoles,
    Suggestion,
    WhoAmI,
)

T = TypeVar("T")
PrivatePage = Literal["saved_jobs", "application_histories", "cvs"]

# Private parsers ship against synthetic fixtures (§8.1). Flip to True only after deriving the
# selectors from a real scrubbed capture (and requiring its list container); while False they warn
# and treat 0 items as valid.
VERIFIED: dict[PrivatePage, bool] = {"saved_jobs": False, "application_histories": False, "cvs": False}
PRIVATE_PATHS: dict[PrivatePage, str] = {
    "saved_jobs": "/saved_jobs",
    "application_histories": "/profile/application_histories",
    "cvs": "/profile/cvs",
}

CARD = "div.job-offer-card[data-scroll-restore-slug]"
CARD_TITLE = "h2.job-offer-card__title"
AVATAR_PREFIX = "Avatar de"
LOGGED_IN_MARKERS = ("#header-user-menu", f'[aria-label^="{AVATAR_PREFIX}"]', ".header-dropdown-name", ".header-dropdown-email")
CHALLENGE_MESSAGE = "Blocked by Luk (challenge page) — stopping; not retrying (repo red line: no evasion)"
APP_ID_ATTR = "data-search-pills-application-id-value"
API_KEY_ATTR = "data-search-pills-search-api-key-value"
INDEX_ATTR = "data-query-suggestions-index"

_EMPLOYMENT_PAIRS: tuple[tuple[str, JobType], ...] = (
    ("Jornada Completa", "full_time"), ("Jornada Parcial", "part_time"), ("Freelance / Por contrato", "contractor"),
    ("Prácticas", "intern"), ("Por horas", "per_diem"), ("Otro", "other"),
)
_EMPLOYMENT_LABELS: dict[str, JobType] = {fold(label): value for label, value in _EMPLOYMENT_PAIRS}
_MODALITY_LABELS: dict[str, Modality] = {"presencial": "on_site", "remoto": "remote", "hibrido": "hybrid"}
_JSONLD_EMPLOYMENT: dict[str, JobType] = {value.upper(): value for value in _EMPLOYMENT_LABELS.values()}

_INT_RE = re.compile(r"\d{1,3}(?:\.\d{3})+|\d+", re.ASCII)
_PAGE_LABEL_RE = re.compile(r"Página (\d+)", re.ASCII)
_SALARY_RE = re.compile(r"(?P<currency>[A-Z]{3})\s*\$?\s*(?P<low>\d[\d.,]*)(?:\s*-\s*\$?\s*(?P<high>\d[\d.,]*))?", re.ASCII)
_DECIMALS_RE = re.compile(r",\d{1,2}$")
_HOURS_RE = re.compile(r"hace (?:menos de 1 hora|\d+ horas?)")
_DAYS_RE = re.compile(r"hace (\d+) dias?")
_MONTHS_RE = re.compile(r"hace (\d+) mes(?:es)?")
_VACANCIES_RE = re.compile(r"(\d+)\s+vacantes?\b", re.IGNORECASE)
_OFFERS_RE = re.compile(r"(\d[\d.]*) ofertas? activas?")
_SIZE_RE = re.compile(r"\d[\d.]*(\s*-\s*\d[\d.]*)?\+?\s+empleados", re.IGNORECASE)
_JOB_PATH_RE = re.compile(r"/job_offers/([^/]+)/?")
_COMPANY_PATH_RE = re.compile(r"/companies/([^/]+)/?")
_FILE_NAME_RE = re.compile(r"\S(?:.*\S)?\.(?:pdf|docx?|odt|rtf)", re.IGNORECASE)
_DATE_RE = re.compile(r"hace \d|\d{1,2} de [a-z]+|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
_APPLIED_PATTERNS = (re.compile(r"postul"), _DATE_RE)
_UPDATED_PATTERNS = (re.compile(r"actualiz|subid|cargad"), _DATE_RE)
_HEADINGS = "h1, h2, h3, h4, .card-title"
_CHROME_TAGS = frozenset({"header", "nav", "footer"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "tr"}
)


@dataclass(frozen=True)
class Pagination:
    last_page: int | None  # max("Página N" labels, current page) inside nav.pagination-nav; no nav → 1
    has_next: bool  # a[aria-label="Página siguiente"] is a link (not a disabled span)


@dataclass(frozen=True)
class SearchPage:
    total: int
    cards: list[JobCard]
    pagination: Pagination
    effective_location: AreaRef | None
    related_roles: list[str]


@dataclass(frozen=True)
class CompaniesPage:
    total: int
    cards: list[CompanyCard]
    pagination: Pagination


@dataclass(frozen=True)
class Parsed(Generic[T]):
    value: T
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrivateList(Generic[T]):
    items: list[T]
    pagination: Pagination
    verified: bool
    warnings: list[str] = field(default_factory=list)  # incl. "parser unverified — run `luk debug capture <path>`"


def missing_anchor(html: str, *, page: str, selector: str, capture_path: str) -> LukError:
    """The error for a missing required anchor: `Blocked` if `http.looks_like_challenge(html)`,
    else `SiteChanged.missing_anchor(page, selector, capture_path)`. Callers `raise` the result."""
    if looks_like_challenge(html):
        return Blocked(CHALLENGE_MESSAGE)
    return SiteChanged.missing_anchor(page, selector, capture_path)


@dataclass(frozen=True)
class _Page:
    """Where a parse failure happened, for missing_anchor()."""

    html: str
    name: str
    capture_path: str

    def missing(self, selector: str) -> LukError:
        return missing_anchor(self.html, page=self.name, selector=selector, capture_path=self.capture_path)


def parse_search(html: str, *, page: int, today: date, base_url: str, capture_path: str) -> SearchPage:
    """`/job_offers` full page. Required: the results frame, an integer total, and per card
    `data-scroll-restore-slug` + non-empty title. Total 0 → no cards (ok); total > 0 with 0 cards on
    `page` ≤ last_page → SiteChanged. Cards: tags typed by value (employment_type/modality/labels),
    `posted_at_approx` = today − parsed age, card salary best-effort ("CLP $650.000 - $850.000")."""
    where = _Page(html, "search", capture_path)
    tree = HTMLParser(html)
    frame = tree.css_first("turbo-frame#job_offers_results")
    if frame is None:
        raise where.missing("turbo-frame#job_offers_results")
    total = _int(_text(frame.css_first(".job-offers-results-count b")))
    if total is None:
        raise where.missing(".job-offers-results-count b (an integer)")
    cards = _job_cards(frame, where, base_url=base_url, today=today)
    pagination = _pagination(frame)
    if total and not cards and page <= (pagination.last_page or 1):
        raise where.missing(CARD)
    return SearchPage(
        total=total, cards=cards, pagination=pagination,
        effective_location=_effective_location(tree), related_roles=_related_roles(tree),
    )


def parse_job(html: str, *, slug: str, base_url: str, today: date, capture_path: str) -> Parsed[JobPosting]:
    """`/job_offers/{slug}` (§5.2): merge JSON-LD and DOM. Required: JSON-LD JobPosting OR
    `.job-offer-show-title h1` (DOM-only → warning). `slug` is the requested slug (the api sets
    `canonical_slug` after a redirect). status: validThrough < today → "expired"; a closed-state
    marker (Phase 0) → "closed"; else "open". `text_truncated` is False here (the api caps text)."""
    tree = HTMLParser(html)
    blocks = _json_ld(tree)
    posting = _of_type(blocks, "JobPosting")
    ld = posting or {}
    title = _text(tree.css_first(".job-offer-show-title h1")) or _str(ld.get("title"))
    if not title:
        raise missing_anchor(
            html, page="job", selector=".job-offer-show-title h1 (or a JSON-LD JobPosting)", capture_path=capture_path
        )
    header = _job_header(tree)
    tags = _labels(header)
    age = _age(header)
    raw_salary = _text(_first(header, "span.tag-primary"))
    vacancies = _VACANCIES_RE.search(_text(header))
    organization = ld.get("hiringOrganization")
    organization = organization if isinstance(organization, dict) else {}
    company_link = tree.css_first('.job-offer-show-title a[href^="/companies/"]')
    company_slug = _company_slug(
        organization.get("sameAs"), _breadcrumb_company(blocks), company_link.attributes.get("href") if company_link else None
    )
    valid_through = _date(ld.get("validThrough"))
    direct_apply = ld.get("directApply")
    value = JobPosting(
        slug=slug,
        url=target_url(base_url, "job", slug),
        title=title,
        company=_str(organization.get("name")) or _opt(_text(company_link)),
        location=_opt(_text(_first(header, "span.break-words"))),
        salary=_ld_salary(ld.get("baseSalary"), raw=_opt(raw_salary)) or _card_salary(raw_salary),
        employment_type=_jsonld_employment(ld.get("employmentType")) or _label_type(tags, _EMPLOYMENT_LABELS),
        modality=_label_type(tags, _MODALITY_LABELS),
        labels=tags,
        posted_ago=age,
        posted_at_approx=_posted_at(age, today),
        offer_id=_offer_id(ld.get("identifier")),
        company_slug=company_slug,
        company_url=target_url(base_url, "company", company_slug) if company_slug else None,
        address=_address(ld.get("jobLocation")),
        work_hours=_str(ld.get("workHours")),
        vacancies=int(vacancies[1]) if vacancies else None,
        date_posted=_date(ld.get("datePosted")),
        valid_through=valid_through,
        status="expired" if valid_through is not None and valid_through < today else "open",
        direct_apply=direct_apply if isinstance(direct_apply, bool) else None,
        description_text=_section(tree, "descripcion") or _ld_description(ld.get("description")),
        requirements_text=_section(tree, "requerimientos"),
    )
    warnings = [] if posting else ["the job page has no JSON-LD JobPosting; parsed the visible page only"]
    return Parsed(value=value, warnings=warnings)


def parse_companies(html: str, *, base_url: str, capture_path: str) -> CompaniesPage:
    """`/companies`. Required: `turbo-frame#companies_marketplace_results`, an integer in its first
    `b.text-primary`, per card the slug + `.company-card__name`. Skeletons ignored; total 0 → []."""
    where = _Page(html, "companies", capture_path)
    frame = HTMLParser(html).css_first("turbo-frame#companies_marketplace_results")
    if frame is None:
        raise where.missing("turbo-frame#companies_marketplace_results")
    total = _int(_text(frame.css_first("b.text-primary")))
    if total is None:
        raise where.missing("b.text-primary (an integer) in the results frame")
    cards: list[CompanyCard] = []
    for node in frame.css('a.company-card[href^="/companies/"]'):
        slug = _company_slug(node.attributes.get("href"))
        name = node.css_first(".company-card__name")
        if slug is None:
            raise where.missing('a.company-card[href^="/companies/"] (a company slug)')
        if name is None:
            raise where.missing(".company-card__name")
        cards.append(_company_card(node, slug=slug, name=_text(name), base_url=base_url))
    return CompaniesPage(total=total, cards=cards, pagination=_pagination(frame))


def parse_company(html: str, *, slug: str, page: int, base_url: str, today: date, capture_path: str) -> Company:
    """`/companies/{slug}`. Required: `h1` and `turbo-frame#company_job_offers_results` (0 cards is valid);
    jobs use the search card parser; `has_more`/`next_page` from a pagination nav inside the jobs frame."""
    where = _Page(html, "company", capture_path)
    tree = HTMLParser(html)
    heading = tree.css_first("h1")
    if heading is None:
        raise where.missing("h1")
    frame = tree.css_first("turbo-frame#company_job_offers_results")
    if frame is None:
        raise where.missing("turbo-frame#company_job_offers_results")
    pagination = _pagination(frame)
    return Company(
        slug=slug,
        url=target_url(base_url, "company", slug),
        name=_text(heading),
        location=_opt(_text(tree.css_first("div.text-caption.text-gray span"))),
        active_offers=_offer_count(_text(tree.css_first("#section-job-offers h2.subsection-header"))),
        jobs=_job_cards(frame, where, base_url=base_url, today=today),
        page=page,
        has_more=pagination.has_next,
        next_page=page + 1 if pagination.has_next else None,
    )


def parse_areas(body: str, content_type: str) -> list[Area]:
    """`/flexible_search/areas?q=` JSON: `{"areas": [...]}` in server order (offer_count desc)."""
    what = "areas response"
    data = _json(body, content_type, what)
    items = data.get("areas") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise _json_changed(body, what, "an 'areas' list")
    return [_area(item) for item in items]


def parse_similar_roles(body: str, content_type: str, *, role: str) -> SimilarRoles:
    """`/job_titles/similar_roles` JSON: `{"items": [str], "next_page", "next_ring", "resolved_name"}`."""
    what = "similar-roles response"
    data = _json(body, content_type, what)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise _json_changed(body, what, "an 'items' list")
    return SimilarRoles(
        role=role,
        resolved_name=_str(data.get("resolved_name")),
        items=[name for item in items if (name := _str(item))],
        next_page=_int_value(data.get("next_page")),
    )


def parse_suggestions(body: str, content_type: str) -> list[Suggestion]:
    """Algolia `*/queries` JSON: `results[0].hits[]` as `{query, popularity}`."""
    what = "Algolia response"
    data = _json(body, content_type, what)
    results = data.get("results") if isinstance(data, dict) else None
    first = results[0] if isinstance(results, list) and results else None
    hits = first.get("hits") if isinstance(first, dict) else None
    if not isinstance(hits, list):
        raise _json_changed(body, what, "a results[0].hits list")
    return [
        Suggestion(query=query, popularity=_int_value(hit.get("popularity")))
        for hit in hits
        if isinstance(hit, dict) and (query := _str(hit.get("query")))
    ]


def parse_algolia_config(html: str) -> AlgoliaConfig:
    """Root page `/` → AlgoliaConfig (app id, public search key, suggestions index; the index falls back
    to config.DEFAULT_ALGOLIA_INDEX). Missing attributes or an app id failing ^[A-Z0-9]{10}$ → SiteChanged."""
    tree = HTMLParser(html)
    values = {name: _attribute(tree, name) for name in (APP_ID_ATTR, API_KEY_ATTR)}
    for name, value in values.items():
        if not value:
            raise missing_anchor(html, page="home", selector=f"[{name}]", capture_path="/")
    index = _attribute(tree, INDEX_ATTR, tag="input") or DEFAULT_ALGOLIA_INDEX
    try:
        return AlgoliaConfig(values[APP_ID_ATTR], values[API_KEY_ATTR], index)
    except InvalidArgument:
        raise missing_anchor(
            html, page="home", selector=f"[{APP_ID_ATTR}] (10 characters A-Z/0-9)", capture_path="/"
        ) from None


def avatar_name(tree: HTMLParser) -> str | None:
    """The name in the header avatar's `aria-label="Avatar de …"`, if any."""
    avatar = tree.css_first(f'[aria-label^="{AVATAR_PREFIX}"]')
    label = (avatar.attributes.get("aria-label") or "") if avatar is not None else ""
    return _opt(" ".join(label[len(AVATAR_PREFIX):].split()))


def header_identity(tree: HTMLParser) -> tuple[str | None, str | None]:
    """(name, email) shown by the logged-in header, best effort; (None, None) on an anonymous page."""
    name = _opt(_text(tree.css_first(".header-dropdown-name"))) or avatar_name(tree)
    return name, _opt(_text(tree.css_first(".header-dropdown-email")))


def parse_whoami(html: str) -> WhoAmI:
    """Root page `/` fetched with the session: logged-in marker → WhoAmI(logged_in=True, name, email)
    (best-effort from the header dropdown); anonymous marker → AuthRequired; neither → SiteChanged."""
    tree = HTMLParser(html)
    if tree.css_first(ANONYMOUS_MARKER) is not None:
        raise AuthRequired()
    if all(tree.css_first(marker) is None for marker in LOGGED_IN_MARKERS):
        raise missing_anchor(
            html, page="home", selector=f'the logged-in header (#header-user-menu or [aria-label^="{AVATAR_PREFIX}"])',
            capture_path="/",
        )
    name, email = header_identity(tree)
    return WhoAmI(logged_in=True, name=name, email=email)


def parse_saved_jobs(html: str, *, today: date, base_url: str) -> PrivateList[JobCard]:
    """`/saved_jobs` (§5.5 private row; verified per VERIFIED["saved_jobs"]). Strategy 1: the search-card
    partial; strategy 2: offer links outside the page chrome (title = heading in the link, or its text)."""
    body = _private_body(html, "saved_jobs")
    cards = [_job_card(node, base_url=base_url, today=today) for node in body.css(CARD) if _card_problem(node) is None]
    if not cards:
        cards = [
            JobCard(slug=slug, url=target_url(base_url, "job", slug), title=title)
            for slug, link in _offer_links(body).items()
            if (title := _text(link.css_first(_HEADINGS)) or _text(link))
        ]
    return _private_list("saved_jobs", body, cards)


def parse_applications(html: str, *, base_url: str) -> PrivateList[Application]:
    """`/profile/application_histories`: raw text only (title, company, applied_at_text, status_text).
    Strategy 1: one item per offer link (its list item or card); strategy 2: `.application-*` boxes."""
    body = _private_body(html, "application_histories")
    items = [
        application
        for slug, link in _offer_links(body).items()
        if (application := _application(_item_box(link), slug=slug, url=target_url(base_url, "job", slug), fallback=_text(link)))
    ]
    if not items:
        items = [application for box in _class_boxes(body, "application") if (application := _application(box))]
    return _private_list("application_histories", body, items)


def parse_cvs(html: str) -> PrivateList[Cv]:
    """`/profile/cvs`: CV names and updated-at text only; never file URLs. Strategy 1: text shaped like
    a file name (`*.pdf|doc|docx|odt|rtf`), one per list item or card; strategy 2: `.cv-*` boxes."""
    body = _private_body(html, "cvs")
    items: list[Cv] = []
    boxes: set[int] = set()
    for leaf in _leaves(body):
        name = _text(leaf)
        if len(name) <= 255 and _FILE_NAME_RE.fullmatch(name) and not _in_chrome(leaf):
            box = _item_box(leaf)
            if _node_id(box) not in boxes:
                boxes.add(_node_id(box))
                items.append(Cv(name=name, updated_at_text=_leaf_text(box, _UPDATED_PATTERNS)))
    if not items:
        items = [
            Cv(name=name, updated_at_text=_leaf_text(box, _UPDATED_PATTERNS))
            for box in _class_boxes(body, "cv")
            if (name := _text(box.css_first(_HEADINGS)))
        ]
    return _private_list("cvs", body, items)


# --- shared helpers ---------------------------------------------------------------------------


def _text(node: Node | None) -> str:
    """The node's text with whitespace collapsed ('' when the node is absent)."""
    return " ".join(node.text(separator=" ").split()) if node is not None else ""


def _opt(text: str) -> str | None:
    return text or None


def _str(value: object) -> str | None:
    """A non-blank JSON string, stripped."""
    return (value.strip() or None) if isinstance(value, str) else None


def _int_value(value: object) -> int | None:
    """A JSON integer (booleans excluded)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | None:
    """An amount or id given as a JSON number or a digit string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value)
    return None


def _int(text: str) -> int | None:
    """An integer shown on a page ("273", "1.828")."""
    return int(text.replace(".", "")) if _INT_RE.fullmatch(text) else None


def _is_slug(value: str) -> bool:
    return len(value) <= MAX_SLUG_LEN and SLUG_RE.fullmatch(value) is not None


def _first(scope: Node | None, selector: str) -> Node | None:
    return scope.css_first(selector) if scope is not None else None


def _path_slug(url: object, pattern: re.Pattern[str]) -> str | None:
    """The slug of a Luk URL or path matching `pattern` (`/job_offers/<slug>` or `/companies/<slug>`)."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    match = pattern.fullmatch(parts.path)
    if match is None or (parts.netloc and parts.netloc.lower() not in URL_HOSTS) or not _is_slug(match[1]):
        return None
    return match[1]


def _company_slug(*candidates: object) -> str | None:
    """The first candidate URL/path naming a real company (never /companies/home, /pricing, …)."""
    for candidate in candidates:
        for url in candidate if isinstance(candidate, list) else [candidate]:
            slug = _path_slug(url, _COMPANY_PATH_RE)
            if slug is not None and slug not in RESERVED_COMPANY_SLUGS:
                return slug
    return None


def _pagination(scope: Node) -> Pagination:
    nav = scope.css_first("nav.pagination-nav")
    if nav is None:
        return Pagination(last_page=1, has_next=False)
    labels = [link.attributes.get("aria-label") or "" for link in nav.css("a[aria-label]")]
    pages = [int(match[1]) for label in labels if (match := _PAGE_LABEL_RE.fullmatch(label))]
    current = _int(_text(nav.css_first('[aria-current="page"]')))
    if current is not None:
        pages.append(current)
    return Pagination(last_page=max(pages, default=None), has_next="Página siguiente" in labels)


# --- job cards ----------------------------------------------------------------------------------


def _card_problem(card: Node) -> str | None:
    """The required anchor a search card lacks, if any."""
    if not _is_slug(card.attributes.get("data-scroll-restore-slug") or ""):
        return "div.job-offer-card[data-scroll-restore-slug] (a job slug)"
    if not _text(card.css_first(CARD_TITLE)):
        return CARD_TITLE
    return None


def _job_cards(scope: Node, where: _Page, *, base_url: str, today: date) -> list[JobCard]:
    """Every search card in `scope`; a card without its slug or title is a missing anchor."""
    nodes = scope.css(CARD)
    for node in nodes:
        problem = _card_problem(node)
        if problem is not None:
            raise where.missing(problem)
    return [_job_card(node, base_url=base_url, today=today) for node in nodes]


def _job_card(card: Node, *, base_url: str, today: date) -> JobCard:
    """A search-card partial that has no `_card_problem`."""
    slug = card.attributes.get("data-scroll-restore-slug") or ""
    tags = _labels(card)
    age = _age(card)
    return JobCard(
        slug=slug,
        url=target_url(base_url, "job", slug),
        title=_text(card.css_first(CARD_TITLE)),
        company=_opt(_text(card.css_first("p.item-title"))),
        location=_opt(_text(card.css_first("span.break-words"))),
        salary=_card_salary(_text(card.css_first("span.tag-primary"))),
        employment_type=_label_type(tags, _EMPLOYMENT_LABELS),
        modality=_label_type(tags, _MODALITY_LABELS),
        labels=tags,
        posted_ago=age,
        posted_at_approx=_posted_at(age, today),
    )


def _labels(scope: Node | None) -> list[str]:
    return [text for tag in (scope.css("span.tag-navy") if scope is not None else []) if (text := _text(tag))]


def _label_type(tags: list[str], table: dict[str, T]) -> T | None:
    """The first tag whose folded text is in `table` (tags are typed by value, never position)."""
    return next((table[key] for key in map(fold, tags) if key in table), None)


def _age(scope: Node | None) -> str | None:
    """The relative age: the first `.item-subtitle > span` reading "Hace …"."""
    for span in scope.css(".item-subtitle > span") if scope is not None else []:
        text = _text(span)
        if fold(text).startswith("hace"):
            return text
    return None


def _posted_at(age: str | None, today: date) -> date | None:
    """today − age: "menos de 1 hora"/"N horas" → today, "N días", "N mes(es)" → 30·N days; else None."""
    key = fold(age or "")
    if _HOURS_RE.fullmatch(key):
        return today
    if match := _DAYS_RE.fullmatch(key):
        return today - timedelta(days=int(match[1]))
    if match := _MONTHS_RE.fullmatch(key):
        return today - timedelta(days=30 * int(match[1]))
    return None


def _card_salary(raw: str) -> Salary | None:
    """Best-effort salary tag: "CLP $650.000 - $850.000"; one amount → min = max; unparsable → raw only."""
    if not raw:
        return None
    match = _SALARY_RE.fullmatch(raw)
    if match is None:
        return Salary(raw=raw)
    low = _amount(match["low"])
    return Salary(raw=raw, currency=match["currency"], min=low, max=_amount(match["high"]) if match["high"] else low)


def _amount(text: str) -> int:
    """"650.000" → 650000; "2.150.000,00" (COP decimal comma) → 2150000."""
    return int(re.sub(r"[.,]", "", _DECIMALS_RE.sub("", text)))


def _effective_location(tree: HTMLParser) -> AreaRef | None:
    """The applied area echoed in `input#locations` (value + data-initial-area-options); None when worldwide."""
    node = tree.css_first("input#locations")
    if node is None:
        return None
    applied = (node.attributes.get("value") or "").split(",")[0].strip()
    try:
        options = json.loads(node.attributes.get("data-initial-area-options") or "[]")
    except ValueError:
        return None
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict) and applied and str(option.get("id")) == applied:
            return _area_ref(option)
    return None


def _area_ref(option: dict[str, Any]) -> AreaRef | None:
    area_id, path = _int_value(option.get("id")), _str(option.get("display_path"))
    if area_id is None or path is None:
        return None
    return AreaRef(id=area_id, display_path=path, area_type_label=_str(option.get("area_type_label")))


def _related_roles(tree: HTMLParser) -> list[str]:
    names = (_str(pill.attributes.get("data-role-name")) for pill in tree.css("a.similar-role-suggestion-pill[data-role-name]"))
    return list(dict.fromkeys(name for name in names if name))


# --- job detail ---------------------------------------------------------------------------------


def _json_ld(tree: HTMLParser) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page; unparsable blocks are skipped (the fields are optional)."""
    found: list[dict[str, Any]] = []
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(), strict=False)
        except ValueError:
            continue
        found.extend(item for item in (data if isinstance(data, list) else [data]) if isinstance(item, dict))
    return found


def _of_type(blocks: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((block for block in blocks if block.get("@type") == kind), None)


def _breadcrumb_company(blocks: list[dict[str, Any]]) -> object:
    """BreadcrumbList position 3 `item` (the company page)."""
    items = (_of_type(blocks, "BreadcrumbList") or {}).get("itemListElement")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("position") == 3:
            target = item.get("item")
            return target.get("@id") if isinstance(target, dict) else target
    return None


def _job_header(tree: HTMLParser) -> Node | None:
    """The `.card` around `.job-offer-show-title` (title block, tags, salary, vacancies, age)."""
    block = tree.css_first(".job-offer-show-title")
    node = block
    while node is not None and "card" not in (node.attributes.get("class") or "").split():
        node = node.parent
    return node or block


def _ld_salary(value: object, *, raw: str | None) -> Salary | None:
    """JSON-LD baseSalary {currency, value{minValue, maxValue, unitText}}; absent when the salary is hidden."""
    if not isinstance(value, dict):
        return None
    quantity = value.get("value")
    quantity = quantity if isinstance(quantity, dict) else {"value": quantity}
    low = _number(quantity.get("minValue", quantity.get("value")))
    high = _number(quantity.get("maxValue", quantity.get("value")))
    currency, unit = _str(value.get("currency")), _str(quantity.get("unitText"))
    if low is None and high is None and currency is None:
        return None
    return Salary(raw=raw, currency=currency, min=low, max=high, period=unit.lower() if unit else None)


def _jsonld_employment(value: object) -> JobType | None:
    """employmentType "FULL_TIME" (or a list of them) → "full_time"."""
    values = value if isinstance(value, list) else [value]
    return next((_JSONLD_EMPLOYMENT[v.upper()] for v in values if isinstance(v, str) and v.upper() in _JSONLD_EMPLOYMENT), None)


def _offer_id(identifier: object) -> int | None:
    """identifier.value ("30814") of the JobPosting."""
    for item in identifier if isinstance(identifier, list) else [identifier]:
        number = _number(item.get("value") if isinstance(item, dict) else item)
        if number is not None:
            return number
    return None


def _address(location: object) -> Address | None:
    place = location[0] if isinstance(location, list) and location else location
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return None
    country = address.get("addressCountry")
    result = Address(
        locality=_str(address.get("addressLocality")),
        region=_str(address.get("addressRegion")),
        country=_str(country.get("name") if isinstance(country, dict) else country),
    )
    return result if result != Address() else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _section(tree: HTMLParser, heading: str) -> str | None:
    """Text of the `[data-text-toggle-target="fullText"]` block right after the h3 whose folded text is `heading`."""
    for h3 in tree.css("h3"):
        if fold(h3.text()) != heading:
            continue
        sibling = h3.next
        while sibling is not None and sibling.tag in ("-text", "_comment"):
            sibling = sibling.next
        body = _first(sibling, '[data-text-toggle-target="fullText"]')
        return (_rich_text(body) or None) if body is not None else None
    return None


def _ld_description(value: object) -> str:
    """JSON-LD description (HTML) as text, without its boilerplate first <p> ("Cargo: … Empresa: …")."""
    body = HTMLParser(value).body if isinstance(value, str) else None
    if body is None:
        return ""
    first = next(body.iter(), None)
    if first is not None and first.tag == "p" and _text(first).startswith("Cargo:"):
        first.decompose()
    return _rich_text(body)


def _rich_text(node: Node) -> str:
    """Block text: paragraphs separated by a blank line, `<li>` as "- item" lines, `<br>` as a newline."""
    blocks: list[tuple[bool, str]] = []  # (is a list item, text)
    inline: list[str] = []

    def flush() -> None:
        lines = (" ".join(line.split()) for line in "".join(inline).split("\n"))
        text = "\n".join(line for line in lines if line)
        if text:
            blocks.append((False, text))
        inline.clear()

    def walk(parent: Node) -> None:
        for child in parent.iter(include_text=True):
            if child.tag == "-text":
                inline.append(child.text(deep=False).replace("\n", " "))
            elif child.tag == "br":
                inline.append("\n")
            elif child.tag == "li":
                flush()
                if item := _text(child):
                    blocks.append((True, f"- {item}"))
            elif child.tag in _BLOCK_TAGS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    walk(node)
    flush()
    return "".join(("\n" if is_item else "\n\n") * bool(index) + text for index, (is_item, text) in enumerate(blocks))


# --- companies ----------------------------------------------------------------------------------


def _company_card(node: Node, *, slug: str, name: str, base_url: str) -> CompanyCard:
    tags = [text for tag in node.css(".company-card__tags span.tag-navy") if (text := _text(tag))]
    size = next((tag for tag in tags if _SIZE_RE.fullmatch(tag)), None)
    return CompanyCard(
        slug=slug,
        url=target_url(base_url, "company", slug),
        name=name,
        location=_opt(_text(node.css_first(".company-card__location span"))),
        sector=next((tag for tag in tags if tag != size), None),
        size=size,
        active_offers=_offer_count(_text(node.css_first(".company-card__offers"))),
        # the span's own text: its `strong.company-card__location-more` ("+ 7 más") is left out
        offer_locations=[
            text for span in node.css(".company-card__offer-locations span") if (text := " ".join(span.text(deep=False).split()))
        ],
    )


def _offer_count(text: str) -> int | None:
    """"29 ofertas activas" / "1 oferta activa" → N; "Sin ofertas activas" → 0."""
    key = fold(text)
    if key == "sin ofertas activas":
        return 0
    match = _OFFERS_RE.fullmatch(key)
    return int(match[1].replace(".", "")) if match else None


# --- JSON ---------------------------------------------------------------------------------------


def _json(body: str, content_type: str, what: str) -> Any:
    if content_type.split(";", 1)[0].strip().lower() == "application/json":
        try:
            return json.loads(body)
        except ValueError:
            pass
    raise _json_changed(body, what, "a JSON body")


def _json_changed(body: str, what: str, expected: str) -> LukError:
    """§5.5 for JSON endpoints: a challenge page → Blocked, anything else → SiteChanged."""
    if looks_like_challenge(body):
        return Blocked(CHALLENGE_MESSAGE)
    return SiteChanged(f"Luk {what} changed: expected {expected}")


def _area(item: object) -> Area:
    if not isinstance(item, dict) or _int_value(item.get("id")) is None or not _str(item.get("name")) or not _str(item.get("display_path")):
        raise SiteChanged("Luk areas response changed: expected areas with id, name and display_path")
    match = item.get("visitor_country_match")
    return Area(
        id=item["id"],
        name=item["name"].strip(),
        display_path=item["display_path"].strip(),
        area_type=_str(item.get("area_type")),
        area_type_label=_str(item.get("area_type_label")),
        offer_count=_int_value(item.get("offer_count")),
        depth=_int_value(item.get("depth")),
        visitor_country_match=match if isinstance(match, bool) else None,
    )


def _attribute(tree: HTMLParser, name: str, *, tag: str = "") -> str:
    node = tree.css_first(f"{tag}[{name}]")
    return (node.attributes.get(name) or "").strip() if node is not None else ""


# --- private pages ------------------------------------------------------------------------------


def _private_body(html: str, page: PrivatePage) -> Node:
    """§5.5 private row: the anonymous page means the session is gone; else the page's <body>."""
    tree = HTMLParser(html)
    if tree.css_first(ANONYMOUS_MARKER) is not None:
        raise AuthRequired()
    if tree.body is None:  # selectolax always builds one (typed Optional)
        raise missing_anchor(html, page=page, selector="body", capture_path=PRIVATE_PATHS[page])
    return tree.body


def _private_list(page: PrivatePage, body: Node, items: list[T]) -> PrivateList[T]:
    verified = VERIFIED[page]
    warnings = [] if verified else [f"parser unverified — run `luk debug capture {PRIVATE_PATHS[page]}`"]
    return PrivateList(items=items, pagination=_pagination(body), verified=verified, warnings=warnings)


def _in_chrome(node: Node) -> bool:
    """Inside the site header, a nav or the footer (never page content)."""
    parent = node.parent
    while parent is not None:
        if parent.tag in _CHROME_TAGS:
            return True
        parent = parent.parent
    return False


def _offer_links(scope: Node) -> dict[str, Node]:
    """The first content link to each `/job_offers/<slug>` (never save_later or other sub-paths)."""
    links: dict[str, Node] = {}
    for link in scope.css("a[href]"):
        slug = _path_slug(link.attributes.get("href"), _JOB_PATH_RE)
        if slug is not None and slug not in links and not _in_chrome(link):
            links[slug] = link
    return links


def _item_box(node: Node) -> Node:
    """The list item around `node`: the nearest li/tr/article or `.card`/`.*-card` ancestor, else its parent."""
    ancestor = node.parent
    while ancestor is not None and ancestor.tag != "body":
        classes = (ancestor.attributes.get("class") or "").split()
        if ancestor.tag in ("li", "tr", "article") or any(c == "card" or c.endswith("-card") for c in classes):
            return ancestor
        ancestor = ancestor.parent
    return node.parent if node.parent is not None else node


def _class_boxes(scope: Node, word: str) -> list[Node]:
    """Innermost elements with a `word` / `word-*` class (BEM `__` parts excluded) that hold a heading."""

    def named(node: Node) -> bool:
        classes = (node.attributes.get("class") or "").split()
        return any(c == word or (c.startswith(f"{word}-") and "__" not in c) for c in classes)

    candidates = [node for node in _subtree(scope) if named(node) and node.css_first(_HEADINGS) is not None]
    ids = {_node_id(node) for node in candidates}
    return [
        node for node in candidates
        if not any(_node_id(inner) in ids and _node_id(inner) != _node_id(node) for inner in _subtree(node))
    ]


def _node_id(node: Node) -> int:
    """The DOM node's identity (Python wrappers are rebuilt on every access). `mem_id` is an int
    property at runtime; the selectolax 0.4 stub declares it as a method."""
    return node.mem_id  # type: ignore[return-value]


def _subtree(node: Node) -> list[Node]:
    """`node` and its descendant elements in document order. Never `Node.traverse()`: in selectolax
    0.4 it runs on past the node into its following siblings."""
    return node.css("*")


def _leaves(scope: Node) -> list[Node]:
    """Elements without element children, in document order."""
    return [node for node in _subtree(scope) if next(node.iter(), None) is None]


def _leaf_text(box: Node, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """Text of the first leaf in `box` whose folded text matches a pattern (patterns tried in order)."""
    texts = [text for leaf in _leaves(box) if (text := _text(leaf))]
    for pattern in patterns:
        for text in texts:
            if pattern.search(fold(text)):
                return text
    return None


def _status_text(box: Node) -> str | None:
    """An element whose class mentions status/estado, else a coloured tag (not a navy label or salary)."""
    for node in _subtree(box):
        classes = (node.attributes.get("class") or "").split()
        if any("status" in c or "estado" in c for c in classes) and (text := _text(node)):
            return text
    for node in box.css('span[class*="tag-"]'):
        tags = set((node.attributes.get("class") or "").split())
        if not tags & {"tag-navy", "tag-primary"} and (text := _text(node)):
            return text
    return None


def _application(box: Node, *, slug: str | None = None, url: str | None = None, fallback: str = "") -> Application | None:
    title = _text(box.css_first(_HEADINGS)) or fallback
    if not title:
        return None
    company = _text(box.css_first("p.item-title")) or _text(box.css_first('a[href^="/companies/"]'))
    return Application(
        slug=slug, url=url, title=title, company=_opt(company),
        applied_at_text=_leaf_text(box, _APPLIED_PATTERNS), status_text=_status_text(box),
    )
