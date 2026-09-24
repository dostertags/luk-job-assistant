"""HTTP for luk-scraper: throttled, honest, GET-only, back off then stop.

- One ``requests.Session`` with the honest User-Agent (:func:`luk_scraper.config.user_agent`).
  A ``user_agent`` argument must have that same format
  (:func:`luk_scraper.config.is_honest_user_agent`), otherwise the Fetcher refuses to exist; the
  User-Agent cannot be changed afterwards.
- No session, ever (public pages only). Before *every* request the session is scrubbed: its
  cookie jar is emptied, ``Cookie`` / ``Authorization`` / ``Proxy-Authorization`` headers are
  dropped and its ``auth`` is replaced by a hook that strips those headers from the prepared
  request (which also keeps requests from adding ``~/.netrc`` credentials). The cookie policy
  refuses every ``Set-Cookie``. This holds for a ``session`` passed in by the caller too, even
  one preloaded with a Luk login cookie.
- Throttle: at least ``delay_s`` (floor :data:`~luk_scraper.config.MIN_DELAY_S`) between the
  start of two requests.
- Retries: 403, 429, 5xx and network errors are retried with exponential backoff
  (``BACKOFF_BASE_S * 2**attempt``), honouring ``Retry-After`` (seconds or HTTP date). A
  ``Retry-After`` longer than ``RETRY_AFTER_MAX_S`` is honoured by stopping right away.
- When retries run out the error is raised: :class:`Blocked` for 403/429 (Luk is refusing us:
  stop, do not work around it), :class:`FetchError` otherwise.
- Guard rails: only ``https://www.takealuk.com`` URLs, never the robots-disallowed
  ``sort_by`` / ``locale`` parameters. Redirects are followed by hand and only to the same path,
  the listing (``/job_offers``) or an offer page (``/job_offers/{slug}``, one segment). Once the
  crawl installs the site's robots.txt (:meth:`Fetcher.set_robots`), every URL, including every
  redirect hop, is checked against it before it is requested.

``sleep`` and ``clock`` are injectable so tests never really wait.
"""
from __future__ import annotations

import http.cookiejar
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import requests
from requests.auth import AuthBase

from . import config

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({403, 429, 500, 502, 503, 504})
BLOCK_STATUS = frozenset({403, 429})
#: Headers that would carry a session or credentials. Never sent.
CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})

Params = Optional[Iterable[tuple[str, str]]]


class FetchError(Exception):
    """A request failed for good (after retries, or with a non-retryable status)."""

    def __init__(self, message: str, *, url: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.url = url
        self.status = status


class Blocked(FetchError):
    """Luk kept answering 403/429 (or asked us to wait too long). The crawl must stop."""


def build_list_params(page: int = 1, countries: Optional[Iterable[str]] = None,
                      date_filter: Optional[str] = None) -> list[tuple[str, str]]:
    """Listing query as a list of pairs: ``countries[]`` (repeated, canonical names, Chile by
    default), then ``worldwide=1`` (without it Luk ANDs the countries with the visitor's geo-IP
    country), ``date`` and ``page`` (only when > 1). Raises ``ValueError`` for an unknown
    country or an empty list (see :func:`luk_scraper.config.normalize_countries`)."""
    params = [("countries[]", c) for c in config.normalize_countries(countries)]
    params.append(config.WORLDWIDE_PARAM)
    if date_filter:
        params.append(("date", str(date_filter)))
    if page and int(page) > 1:
        params.append(("page", str(int(page))))
    return params


def _param_name(name: str) -> str:
    return name.split("[", 1)[0].strip().lower()


def check_url(url: str, params: Params = None) -> None:
    """Raise ValueError unless ``url`` is on Luk over https and sends no forbidden parameter."""
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != config.HOST:
        raise ValueError(f"refusing to fetch {url!r}: only {config.BASE} is crawled")
    names = {_param_name(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    names |= {_param_name(k) for k, _ in (params or [])}
    bad = names & config.FORBIDDEN_PARAMS
    if bad:
        raise ValueError(f"refusing to send {sorted(bad)}: disallowed by Luk's robots.txt")


def _drop_credential_headers(headers) -> None:
    for name in [k for k in headers if k.lower() in CREDENTIAL_HEADERS]:
        del headers[name]


class _NoCredentials(AuthBase):
    """Session ``auth`` hook: runs after requests has merged the cookie jar into the prepared
    request and removes any Cookie / Authorization header. Having an ``auth`` set also stops
    requests from falling back to ``~/.netrc`` credentials."""

    def __call__(self, r):
        _drop_credential_headers(r.headers)
        return r


def _redirect_path_ok(path: str, original_path: str) -> bool:
    """A redirect may only land on the same path, the listing or a single offer page."""
    if path == original_path or path == config.LIST_PATH:
        return True
    prefix = config.LIST_PATH + "/"
    slug = path[len(prefix):] if path.startswith(prefix) else ""
    return bool(slug) and "/" not in slug


def _retry_after_seconds(value: Optional[str], now: Callable[[], datetime]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - now()).total_seconds())


class Fetcher:
    """Throttled GET client for Luk's public pages.

    ``user_agent`` defaults to :func:`luk_scraper.config.user_agent`; anything that is not
    ``luk-scraper/<version> (+<http(s) contact URL without '@'>)`` raises ``ValueError``. A
    ``session`` passed in is taken over: its cookies and credentials are dropped before every
    request, so it can never carry a login.
    """

    def __init__(self, *, delay_s: float = config.RATE_LIMIT_S,
                 timeout_s: float = config.TIMEOUT_S,
                 max_retries: int = config.BACKOFF_MAX_RETRIES,
                 backoff_base_s: float = config.BACKOFF_BASE_S,
                 user_agent: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if user_agent is None:
            user_agent = config.user_agent()
        if not config.is_honest_user_agent(user_agent):
            raise ValueError("refusing this User-Agent: luk-scraper only identifies itself as "
                             f"'{config.PRODUCT_TOKEN}/<version> (+<http(s) contact URL without "
                             "'@'>)', never as a browser and never with an e-mail address")
        self._user_agent = user_agent
        self.delay_s = config.clamp_delay(delay_s)
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.backoff_base_s = max(config.MIN_DELAY_S, float(backoff_base_s))
        self.sleep = sleep
        self.clock = clock
        self.now = now
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "es-CL,es;q=0.9",
        })
        # Public pages only: never store or send cookies (no session, ever).
        self.session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self._scrub_session()
        self._robots = None
        self.requests_made = 0
        self._last_start: Optional[float] = None

    @property
    def user_agent(self) -> str:
        """The honest User-Agent every request carries (read-only)."""
        return self._user_agent

    def _scrub_session(self) -> None:
        """Drop anything that could carry a session or credentials, and restore our UA."""
        s = self.session
        _drop_credential_headers(s.headers)
        s.headers["User-Agent"] = self._user_agent
        s.cookies.clear()
        s.auth = _NoCredentials()

    # -- politeness --------------------------------------------------------------------------
    def set_min_delay(self, delay_s: float) -> None:
        """Raise the pause (e.g. to a robots.txt Crawl-delay); never lowers it."""
        self.delay_s = max(self.delay_s, config.clamp_delay(delay_s))

    def set_robots(self, policy) -> None:
        """Install the site's robots.txt policy (the crawl does this right after reading it).
        From then on every URL, including every redirect hop, must be allowed by it. Only a
        parsed :class:`~luk_scraper.robots.RobotsPolicy` is accepted (``None`` would switch
        the check off)."""
        from .robots import RobotsPolicy   # robots imports this module: import at call time
        if not isinstance(policy, RobotsPolicy):
            raise TypeError("set_robots() takes a luk_scraper.robots.RobotsPolicy, "
                            f"not {type(policy).__name__}")
        self._robots = policy

    def _check_robots(self, url: str, params: Params = None) -> None:
        """Check the full URL (path and query, as the crawl does) against robots.txt."""
        if self._robots is None:
            return
        full = url
        if params:
            full += ("&" if urlsplit(url).query else "?") + urlencode(list(params))
        if not self._robots.allowed(full, self._user_agent):
            raise FetchError(f"robots.txt disallows {urlsplit(url).path}; not requested",
                             url=url)

    def _throttle(self) -> None:
        if self._last_start is not None:
            wait = self.delay_s - (self.clock() - self._last_start)
            if wait > 0:
                self.sleep(wait)
        self._last_start = self.clock()

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base_s * (2 ** attempt)

    # -- core --------------------------------------------------------------------------------
    def _get_once(self, url: str, params: Params) -> requests.Response:
        """One GET (throttled). Redirects are followed by hand, at most MAX_REDIRECTS times,
        and only to the same path, the listing or a single offer page on Luk's host; every hop
        is checked against robots.txt (once installed) before it is requested. A redirect to a
        sign-in page, a sub-page, another site or a disallowed URL is an error, never a
        request."""
        original_path = urlsplit(url).path
        for _ in range(config.MAX_REDIRECTS + 1):
            check_url(url, params)
            self._check_robots(url, params)
            self._scrub_session()
            self._throttle()
            self.requests_made += 1
            r = self.session.get(url, params=list(params) if params else None,
                                 timeout=self.timeout_s, allow_redirects=False)
            if not r.is_redirect:
                return r
            target = urljoin(r.url or url, r.headers.get("Location", ""))
            parts = urlsplit(target)
            same_host = parts.scheme == "https" and (parts.hostname or "").lower() == config.HOST
            if not same_host or not _redirect_path_ok(parts.path, original_path):
                raise FetchError(f"unexpected redirect from {url} to {parts.path or target!r}",
                                 url=url, status=r.status_code)
            if self._robots is not None and not self._robots.allowed(target, self._user_agent):
                raise FetchError(f"robots.txt disallows the redirect from {url} to {parts.path}; "
                                 "not requested", url=url, status=r.status_code)
            url, params = target, None
        raise FetchError(f"too many redirects from {url}", url=url)

    def fetch(self, url: str, params: Params = None) -> requests.Response:
        """GET with throttle + retries/backoff. Returns the final response (any non-retry
        status, including 404); raises :class:`Blocked` / :class:`FetchError` when retries run
        out."""
        params = list(params) if params else None
        check_url(url, params)
        error: Optional[FetchError] = None
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                r = self._get_once(url, params)
            except requests.RequestException as e:
                error = FetchError(f"network error for {url}: {type(e).__name__}", url=url)
                if last:
                    break
                wait = self._backoff(attempt)
                log.warning("network error (%s); retry %d in %.0fs", type(e).__name__,
                            attempt + 1, wait)
                self.sleep(wait)
                continue

            if r.status_code not in RETRY_STATUS:
                return r

            cls = Blocked if r.status_code in BLOCK_STATUS else FetchError
            error = cls(f"HTTP {r.status_code} for {url}", url=url, status=r.status_code)
            if last:
                break
            retry_after = _retry_after_seconds(r.headers.get("Retry-After"), self.now)
            if retry_after is not None and retry_after > config.RETRY_AFTER_MAX_S:
                log.warning("HTTP %s with Retry-After %.0fs: stopping instead of waiting",
                            r.status_code, retry_after)
                error = Blocked(f"HTTP {r.status_code} for {url}; Retry-After {retry_after:.0f}s",
                                url=url, status=r.status_code)
                break
            wait = max(retry_after, self.delay_s) if retry_after is not None \
                else self._backoff(attempt)
            log.warning("HTTP %s; backing off %.0fs (retry %d of %d)", r.status_code, wait,
                        attempt + 1, self.max_retries)
            self.sleep(wait)
        assert error is not None
        raise error

    def get_html(self, url: str, params: Params = None) -> str:
        """GET an HTML page; raises :class:`FetchError` on any 4xx/5xx."""
        r = self.fetch(url, params)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} for {url}", url=url, status=r.status_code)
        if "charset" not in (r.headers.get("Content-Type") or "").lower():
            r.encoding = config.ENCODING
        return r.text

    def get_text_status(self, url: str) -> tuple[int, str]:
        """GET a text resource (robots.txt) -> ``(status, text)`` without raising on 4xx."""
        r = self.fetch(url)
        if "charset" not in (r.headers.get("Content-Type") or "").lower():
            r.encoding = config.ENCODING
        return r.status_code, r.text if r.status_code < 400 else ""

    def list_page(self, page: int = 1, countries: Optional[Iterable[str]] = None,
                  date_filter: Optional[str] = None) -> str:
        """HTML of one listing page (Chile unless ``countries`` says otherwise)."""
        return self.get_html(config.LIST_URL, build_list_params(page, countries, date_filter))
