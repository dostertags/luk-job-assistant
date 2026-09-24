"""Constants and crawl policy for Luk (takealuk.com).

How the site works (reverse-engineered; verified live 2026-09-23):

- Rails + Hotwire/Turbo, server-rendered HTML. There is no public JSON API
  (``/job_offers.json`` answers 406, ``/api/*`` answers 404, no GraphQL), so pages are parsed
  with BeautifulSoup.
- Listing: ``GET /job_offers?countries[]=Chile&worldwide=1&page=N``. 15 cards per page;
  ``page=1`` is the default and is not sent. The last page number appears in the pagination nav
  and the result count in ``.job-offers-results-count b``. Luk is multi-country;
  ``countries[]`` takes the country *name* exactly as one of the site's five checkboxes
  (:data:`COUNTRIES`: Brasil, Chile, Colombia, México, Perú; not an ISO code) and may repeat.
  We always send it (Chile by default) so the scope never depends on the operator's IP
  geolocation.
- ``worldwide=1`` must go with ``countries[]``. Without it Luk ANDs ``countries[]`` with the
  visitor's geo-IP country. Verified live on 2026-09-24 as a visitor located in Chile with our honest UA:
  ``countries[]=Chile`` gave 1890 offers and ``countries[]=Chile&worldwide=1`` 1894 (the same
  set; the difference is timing noise), but ``countries[]=Colombia`` gave 0 and
  ``countries[]=Colombia&worldwide=1`` gave 1430. So every listing URL carries
  :data:`WORLDWIDE_PARAM` right after the countries; robots.txt does not disallow it.
- Optional recency filter ``date=`` with the values in :data:`DATE_FILTERS`.
- Detail: ``GET /job_offers/{slug}``. It carries a ``schema.org/JobPosting`` JSON-LD block with
  the full ``description``, ``datePosted``, ``validThrough``, ``baseSalary`` (omitted when the
  salary is hidden) and ``identifier.value`` (the stable numeric offer id).
- The slug is Luk's canonical URL key (sitemap, card ``href``, ``data-scroll-restore-slug``) and
  is our primary key. The number in the card's ``company-avatar-N`` element is *not* the offer
  id.
- robots.txt (re-checked live 2026-09-24): ``User-agent: *`` / ``Allow: /``, then Disallows for
  private and account pages (``/profile``, ``/saved_jobs``, ``/users/*`` sign-in/up pages,
  ``/onboarding``, ``/admin/``, ``/flipper``), the employer back-office sub-paths
  (``/companies/profile``, ``/companies/job_offers/``, ``/companies/sign_in``,
  ``/companies/registration`` — public ``/companies/{slug}`` pages are allowed), Turbo fragments
  (``/job_offers/*/recommendations``), ``/blog``, ``/wp-*``-style probes and the crawl-budget
  variants ``?sort_by=`` / ``?locale=``. We never send those two parameters
  (:data:`FORBIDDEN_PARAMS`). ``page``, ``countries[]``, ``worldwide`` and ``date`` are
  allowed. The verbatim group is in
  ``tests/conftest.py`` (``LUK_LIKE_ROBOTS``).
- The site answered 200 to an honest, non-browser User-Agent on listing, pagination and
  detail pages.

Policy (ADR-0002): public pages only, never logged in; honest User-Agent; at least
:data:`MIN_DELAY_S` between requests (default :data:`RATE_LIMIT_S`); robots.txt checked before
every crawl; 403/429/5xx back off exponentially (honouring ``Retry-After``) and then the crawl
STOPS. No evasion of any kind.
"""
from __future__ import annotations

import logging
import math
import os
import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import quote

from . import __version__

log = logging.getLogger(__name__)

# --- network ------------------------------------------------------------------------------
BASE = "https://www.takealuk.com"
HOST = "www.takealuk.com"
LIST_PATH = "/job_offers"
LIST_URL = BASE + LIST_PATH
DETAIL_URL = BASE + "/job_offers/{slug}"
ROBOTS_URL = BASE + "/robots.txt"

#: Query parameters robots.txt disallows (crawl-budget protection). Never sent.
FORBIDDEN_PARAMS = frozenset({"sort_by", "locale"})

# --- identity -----------------------------------------------------------------------------
#: Where site operators can learn who is crawling. Override with LUK_SCRAPER_CONTACT (a URL).
DEFAULT_CONTACT = "https://github.com/dostertags/luk-job-assistant"
CONTACT_ENV = "LUK_SCRAPER_CONTACT"
PRODUCT_TOKEN = "luk-scraper"  # what robots.txt groups are matched against

# A contact must be a plain http(s) URL made of printable ASCII: no "@" (so no e-mail address
# and no userinfo), no whitespace, control characters, parentheses, quotes or backslashes (it goes
# inside the User-Agent comment).
_CONTACT_PATTERN = r"https?://(?:(?![@()<>\"\\])[\x21-\x7e])+"
_CONTACT_RE = re.compile(_CONTACT_PATTERN)
# The only User-Agent luk-scraper ever sends: ``luk-scraper/<version> (+<contact URL>)``.
_USER_AGENT_RE = re.compile(re.escape(PRODUCT_TOKEN) + r"/[0-9][0-9A-Za-z.+-]{0,31} \(\+"
                            + _CONTACT_PATTERN + r"\)")

# --- politeness ---------------------------------------------------------------------------
RATE_LIMIT_S = 2.0        # default pause between two requests
MIN_DELAY_S = 1.0         # hard floor, whatever the caller asks for
TIMEOUT_S = 25
BACKOFF_BASE_S = 2.0      # 2, 4, 8, 16 s ...
BACKOFF_MAX_RETRIES = 4
RETRY_AFTER_MAX_S = 300.0  # a longer Retry-After is honoured by stopping, not by waiting
MAX_REDIRECTS = 3
ENCODING = "utf-8"

# --- crawl --------------------------------------------------------------------------------
#: The ``countries[]`` values Luk accepts: its five country checkboxes, spelled as the site does.
COUNTRIES = ("Brasil", "Chile", "Colombia", "México", "Perú")
COUNTRIES_DEFAULT = ["Chile"]
#: Always sent with ``countries[]``; without it Luk ANDs them with the visitor's geo-IP country.
WORLDWIDE_PARAM = ("worldwide", "1")
PAGE_SIZE = 15            # fixed by the site (reference only)
MAX_PAGES = 400           # hard safety cap (Chile had ~123 pages on 2026-09-23)
MAX_PAGES_WITHOUT_NEW = 3  # stop if this many pages in a row bring no new slug

#: ``--date`` aliases -> the exact value of the site's recency filter.
DATE_FILTERS = {
    "1d": "last_day",
    "3d": "last_3_days",
    "1sem": "last_week",
    "1mes": "last_month",
    "3meses": "last_3_months",
    "6meses": "last_6_months",
}


def contact() -> str:
    """The contact URL for the User-Agent: ``$LUK_SCRAPER_CONTACT`` if it is a valid URL.

    Anything else (an e-mail address, text with spaces or control characters, non-ASCII text...)
    is ignored with a warning so that personal data never ends up in other people's server logs
    by accident.
    """
    value = (os.environ.get(CONTACT_ENV) or "").strip()
    if not value:
        return DEFAULT_CONTACT
    if _CONTACT_RE.fullmatch(value):
        return value
    log.warning("%s must be a printable-ASCII http(s) URL without '@'; using the default "
                "contact instead.", CONTACT_ENV)
    return DEFAULT_CONTACT


def user_agent(contact_url: str | None = None) -> str:
    """Honest User-Agent: ``luk-scraper/<version> (+<contact URL>)``. Never a browser's."""
    url = contact_url if contact_url and _CONTACT_RE.fullmatch(contact_url) else contact()
    return f"{PRODUCT_TOKEN}/{__version__} (+{url})"


def is_honest_user_agent(value: object) -> bool:
    """True only for ``luk-scraper/<version> (+<http(s) contact URL without '@'>)``: no browser
    imitation, no e-mail address, nothing before or after it."""
    return isinstance(value, str) and _USER_AGENT_RE.fullmatch(value) is not None


def clamp_delay(delay_s: float | None) -> float:
    """The pause actually used: the default when unset/invalid, never below :data:`MIN_DELAY_S`."""
    if delay_s is None:
        return RATE_LIMIT_S
    try:
        value = float(delay_s)
    except (TypeError, ValueError):
        return RATE_LIMIT_S
    if not math.isfinite(value):
        return RATE_LIMIT_S
    return max(value, MIN_DELAY_S)


def _country_key(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name.strip())
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()


_COUNTRY_BY_KEY = {_country_key(c): c for c in COUNTRIES}


def normalize_country(name: str) -> str:
    """One country as Luk's checkbox spells it (``"peru"`` -> ``"Perú"``); case and accents
    are ignored. Raises ``ValueError`` naming the valid countries for anything else."""
    canonical = _COUNTRY_BY_KEY.get(_country_key(str(name)))
    if canonical is None:
        raise ValueError(f"unknown country {str(name).strip()!r}; Luk's countries are "
                         + ", ".join(COUNTRIES))
    return canonical


def normalize_countries(names: Optional[Iterable[str]]) -> list[str]:
    """Canonical country names, in order, without blanks or repeats (``None`` -> the default,
    Chile). Raises ``ValueError`` for an unknown country or when no country is left."""
    if names is None:
        return list(COUNTRIES_DEFAULT)
    result: list[str] = []
    for name in names:
        if not str(name).strip():
            continue
        canonical = normalize_country(name)
        if canonical not in result:
            result.append(canonical)
    if not result:
        raise ValueError("give at least one country: " + ", ".join(COUNTRIES))
    return result


def detail_url(slug: str) -> str:
    """Canonical detail URL for an offer slug (percent-encoded, stays a single path segment)."""
    return DETAIL_URL.format(slug=quote(slug, safe="-._~"))
