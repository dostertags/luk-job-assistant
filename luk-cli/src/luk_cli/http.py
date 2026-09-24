"""HTTP layer for takealuk.com and Algolia (spec §1.3–§1.5, §4.5–§4.9).

Reverse-engineered mechanism — verified live 2026-09-23 (anonymous, honest UA, ≥1.6 s apart):

- takealuk.com is Rails 8 + Turbo. Every page is a full server-rendered HTML document with
  `html[lang="es-CL"]`. A `Turbo-Frame` request header makes Rails answer with the bare frame
  (no `html[lang]`, no header), so it is never sent. Rails GETs need no CSRF token.
- JSON endpoints (`/flexible_search/areas`, `/job_titles/similar_roles`) answer JSON only with
  `Accept: application/json`.
- Private pages (`/saved_jobs`, `/profile/application_histories`, `/profile/cvs`) answer an
  anonymous request with `302 Location: https://www.takealuk.com/users/sign_in`. The only credential
  is the browser-session cookie `_portal_de_empleos_session` (secure, httponly, lax, no Expires).
  Anonymous full pages carry `header#main-header a[href="/users/sign_in"]`.
- Query suggestions come from Algolia: `POST https://<APP_ID>-dsn.algolia.net/1/indexes/*/queries`
  with `X-Algolia-Application-Id` / `X-Algolia-API-Key` (the public search key scraped from `/`).
  Algolia answers without Origin/Referer, so none is sent.

Rules enforced here:

- LukClient: a request hook refuses anything but `GET https://<LUK_BASE_URL host>` (no port),
  `locale=`/`sort_by=` params and `Turbo-Frame`; anonymous clients also refuse any Cookie header and
  hold a jar that accepts no cookie. Redirects are followed by hand (≤5), each Location passing the
  same check; a Location under `/users/sign_in` is AuthRequired. The session probe
  (`follow_redirects=False`) follows none: any 3xx is AuthRequired after that one request.
- Private clients carry the stored session jar. AuthRequired on 401, sign-in redirects, the
  anonymous marker, or a final path other than the requested one (onboarding). Otherwise rotated
  cookies are written back (CAS) and a non-redirected 200 touches `meta.last_auth_ok_at`.
- Every attempt passes the limiter. ≤4 attempts on connect/read errors (including a connection the
  server closed before or during the response) and 429/502/503/504, backoff
  `min(30, 1.5·2^n)·U(0.75, 1.25)` or `Retry-After` (>60 s → RateLimited at once). 403,
  `cf-mitigated`, or challenge markers on 403/429/503 → Blocked after that single request.
- `--verbose` is only a response event hook: `METHOD url -> status (N ms) attempt=N` on stderr.
- On import, httpx/httpcore loggers are pinned to WARNING and log redaction is installed.
"""

from __future__ import annotations

import json
import random
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from functools import partial
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Protocol, TextIO

import httpx
from selectolax.parser import HTMLParser

from luk_cli import redact
from luk_cli.config import (
    CONNECT_TIMEOUT_S, MAX_ATTEMPTS, MAX_REDIRECTS, MAX_RETRY_AFTER_S, READ_TIMEOUT_S, SIGN_IN_PATH,
    AlgoliaConfig, Settings,
)
from luk_cli.errors import (
    AlgoliaRejected, AuthRequired, Blocked, NetworkError, RateLimited, SessionBusy, UnsafeRequest,
)
from luk_cli.ratelimit import ALGOLIA_LIMITER
from luk_cli.session import LoadedSession, SessionStore, build_cookiejar, cookie_updates

redact.pin_http_loggers()
redact.install_log_redaction()

HTML_ACCEPT = "text/html,application/xhtml+xml"
JSON_ACCEPT = "application/json"
ACCEPT_LANGUAGE = "es-CL"
RETRY_STATUSES = frozenset({429, 502, 503, 504})
CHALLENGE_STATUSES = frozenset({403, 429, 503})
CHALLENGE_MARKERS = ("<title>just a moment", "cf-chl", "challenge-platform", "captcha")
ANONYMOUS_MARKER = 'header#main-header a[href="/users/sign_in"]'
FORBIDDEN_PARAMS = frozenset({"locale", "sort_by"})
REDIRECTED = "Luk redirected to {path} — finish your profile in the browser, then retry"  # §4.5 final path
ALGOLIA_PATH = "/1/indexes/*/queries"
_ALGOLIA_HOST_RE = re.compile(r"[a-z0-9]{10}-dsn\.algolia\.net")
_TIMEOUT = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
# §4.9 "connect/read errors": also the peer closing the connection before or during the response (a stale
# keep-alive socket, a cut body), which httpx reports as RemoteProtocolError rather than a NetworkError.
_RETRIED_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

Params = Sequence[tuple[str, str | int]]
Sleep = Callable[[float], None]
Uniform = Callable[[float, float], float]
Clock = Callable[[], float]


class Limiter(Protocol):
    def acquire(self) -> None: ...


def looks_like_challenge(text: str) -> bool:
    """Cloudflare-style challenge markers; normal Luk pages contain none (§4.9)."""
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def has_anonymous_marker(html: str) -> bool:
    return HTMLParser(html).css_first(ANONYMOUS_MARKER) is not None


def backoff_delay(retry: int, uniform: Uniform = random.uniform) -> float:
    """Delay before retry number `retry` (0-based): min(30, 1.5·2^n)·U(0.75, 1.25)."""
    return min(30.0, 1.5 * 2.0**retry) * uniform(0.75, 1.25)


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait from a Retry-After of delta-seconds or HTTP-date; None if absent/unparsable."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - now)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


@dataclass(frozen=True)
class Fetched:
    """The final response of one logical request (after redirects); the body is kept out of repr."""

    url: str
    status: int
    content_type: str
    text: str = field(repr=False)
    redirected: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return httpx.URL(self.url).path

    @property
    def is_html(self) -> bool:
        return _media_type(self.content_type) == "text/html"

    @property
    def is_json(self) -> bool:
        return _media_type(self.content_type) == "application/json"

    def json(self) -> Any:
        return json.loads(self.text)


def luk_request_guard(host: str, *, cookies_allowed: bool) -> Callable[[httpx.Request], None]:
    """Request event hook for takealuk.com (§4.7): raises UnsafeRequest before anything is sent."""

    def guard(request: httpx.Request) -> None:
        url = request.url
        if request.method != "GET" or url.scheme != "https" or url.host != host or url.port is not None:
            raise UnsafeRequest(
                f"refused {request.method} {url.scheme}://{url.host}{url.path}: only GET https://{host} is allowed"
            )
        # Rails also reads `sort_by[]=` / `LOCALE=`-style keys as params[:sort_by] / params[:locale].
        names = {key.split("[", 1)[0].strip().lower() for key in url.params.keys()}
        if FORBIDDEN_PARAMS.intersection(names):
            raise UnsafeRequest("refused a request carrying locale= or sort_by=")
        if "turbo-frame" in request.headers:
            raise UnsafeRequest("refused a Turbo-Frame request (full pages only)")
        if not cookies_allowed and "cookie" in request.headers:
            raise UnsafeRequest("refused to send cookies from an anonymous client")

    return guard


def algolia_request_guard(request: httpx.Request) -> None:
    """Request event hook for Algolia (§4.7): POST https://<app>-dsn.algolia.net/1/indexes/*/queries only."""
    url = request.url
    if (
        request.method != "POST"
        or url.scheme != "https"
        or not _ALGOLIA_HOST_RE.fullmatch(url.host)
        or url.port is not None
        or url.path != ALGOLIA_PATH
    ):
        raise UnsafeRequest(f"refused {request.method} {url.scheme}://{url.host}{url.path} to Algolia")
    for header in ("cookie", "origin", "referer"):
        if header in request.headers:
            raise UnsafeRequest(f"refused to send a {header} header to Algolia")


def _verbose_hook(stream: TextIO | None) -> Callable[[httpx.Response], None]:
    def hook(response: httpx.Response) -> None:
        request = response.request
        elapsed_ms = (time.perf_counter() - request.extensions.get("luk_started", time.perf_counter())) * 1000
        attempt = request.extensions.get("luk_attempt", 1)
        line = f"{request.method} {request.url} -> {response.status_code} ({elapsed_ms:.0f} ms) attempt={attempt}"
        print(redact.redact(line), file=stream or sys.stderr)

    return hook


def _no_cookie_jar() -> CookieJar:
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def _httpx_client(
    settings: Settings,
    jar: CookieJar,
    guard: Callable[[httpx.Request], None],
    *,
    transport: httpx.BaseTransport | None,
    verbose: bool,
    stderr: TextIO | None,
    headers: dict[str, str],
) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": settings.user_agent, **headers},
        cookies=jar,
        follow_redirects=False,
        timeout=_TIMEOUT,
        verify=True,
        event_hooks={"request": [guard], "response": [_verbose_hook(stderr)] if verbose else []},
        transport=transport,
    )


class _Attempts:
    """≤4 attempts through the limiter with retries, backoff and (for Luk) block classification."""

    def __init__(
        self, client: httpx.Client, limiter: Limiter, *, what: str, classify_blocks: bool,
        sleep: Sleep, uniform: Uniform, clock: Clock,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._what = what
        self._classify_blocks = classify_blocks
        self._sleep = sleep
        self._uniform = uniform
        self._clock = clock

    def send(self, build: Callable[[], httpx.Request]) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._limiter.acquire()
            request = build()
            request.extensions["luk_attempt"] = attempt
            request.extensions["luk_started"] = time.perf_counter()
            try:
                response = self._client.send(request)
            except _RETRIED_ERRORS as exc:
                if attempt == MAX_ATTEMPTS:
                    raise NetworkError(f"network error talking to {self._what}: {type(exc).__name__}") from exc
                self._sleep(backoff_delay(attempt - 1, self._uniform))
                continue
            except httpx.HTTPError as exc:
                raise NetworkError(f"HTTP error talking to {self._what}: {type(exc).__name__}") from exc
            status = response.status_code
            if self._classify_blocks and (
                status == 403
                or "cf-mitigated" in response.headers
                or (status in CHALLENGE_STATUSES and looks_like_challenge(response.text))
            ):
                raise Blocked.for_status(status)
            if status not in RETRY_STATUSES:
                return response
            retry_after = parse_retry_after(response.headers.get("retry-after"), self._clock())
            if retry_after is not None and retry_after > MAX_RETRY_AFTER_S:
                raise RateLimited(
                    f"{self._what} asked to wait {retry_after:.0f} s (HTTP {status}); not waiting that long"
                )
            if attempt == MAX_ATTEMPTS:
                if status == 429:
                    raise RateLimited(f"{self._what} kept answering HTTP 429")
                raise NetworkError(f"{self._what} kept answering HTTP {status}")
            self._sleep(retry_after if retry_after is not None else backoff_delay(attempt - 1, self._uniform))


class LukClient:
    """takealuk.com client. Build it with `anonymous()` (public commands) or `private()` (§4.5)."""

    def __init__(
        self,
        settings: Settings,
        limiter: Limiter,
        *,
        session: LoadedSession | None,
        store: SessionStore | None,
        transport: httpx.BaseTransport | None,
        verbose: bool,
        stderr: TextIO | None,
        sleep: Sleep,
        uniform: Uniform,
        clock: Clock,
    ) -> None:
        self._base_url = settings.base_url
        self._host = settings.host
        self._session = session
        self._store = store
        self._write_lock = threading.Lock()
        jar = build_cookiejar(session.state) if session is not None else _no_cookie_jar()
        self._client = _httpx_client(
            settings, jar, luk_request_guard(settings.host, cookies_allowed=session is not None),
            transport=transport, verbose=verbose, stderr=stderr, headers={"Accept-Language": ACCEPT_LANGUAGE},
        )
        self._attempts = _Attempts(
            self._client, limiter, what="Luk", classify_blocks=True, sleep=sleep, uniform=uniform, clock=clock
        )

    @classmethod
    def anonymous(
        cls,
        settings: Settings,
        limiter: Limiter,
        *,
        transport: httpx.BaseTransport | None = None,
        verbose: bool = False,
        stderr: TextIO | None = None,
        sleep: Sleep = time.sleep,
        uniform: Uniform = random.uniform,
        clock: Clock = time.time,
    ) -> LukClient:
        """No cookies ever stored or sent (data minimisation, §1.4)."""
        return cls(
            settings, limiter, session=None, store=None, transport=transport, verbose=verbose, stderr=stderr,
            sleep=sleep, uniform=uniform, clock=clock,
        )

    @classmethod
    def private(
        cls,
        settings: Settings,
        limiter: Limiter,
        session: LoadedSession,
        *,
        store: SessionStore | None,
        transport: httpx.BaseTransport | None = None,
        verbose: bool = False,
        stderr: TextIO | None = None,
        sleep: Sleep = time.sleep,
        uniform: Uniform = random.uniform,
        clock: Clock = time.time,
    ) -> LukClient:
        """Sends `session`'s cookies; `store=None` (a probe) never writes cookies or meta back."""
        return cls(
            settings, limiter, session=session, store=store, transport=transport, verbose=verbose, stderr=stderr,
            sleep=sleep, uniform=uniform, clock=clock,
        )

    @property
    def is_private(self) -> bool:
        return self._session is not None

    @property
    def session(self) -> LoadedSession | None:
        """The session as last loaded or written back by this client (its CAS token)."""
        return self._session

    def get_html(self, path: str, params: Params | None = None, *, follow_redirects: bool = True) -> Fetched:
        """`follow_redirects=False` is the session probe (§4.2, §4.3): any 3xx is AuthRequired after that
        one request (sign-in → the default message, elsewhere → "Luk redirected to <path> …"), and its
        Location is never requested, so it never receives the session cookie."""
        return self._get(path, params, HTML_ACCEPT, follow_redirects=follow_redirects)

    def get_json(self, path: str, params: Params | None = None) -> Fetched:
        return self._get(path, params, JSON_ACCEPT)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LukClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: Params | None, accept: str, *, follow_redirects: bool = True) -> Fetched:
        if not path.startswith("/") or path.startswith("//"):
            raise UnsafeRequest("request targets must be absolute paths on the Luk host")
        url = httpx.URL(self._base_url + path)
        if params:
            url = url.copy_merge_params(list(params))
        luk_request_guard(self._host, cookies_allowed=True)(httpx.Request("GET", url))  # before the limiter
        requested_path = url.path
        redirects = 0
        while True:
            response = self._attempts.send(partial(self._client.build_request, "GET", url, headers={"Accept": accept}))
            if not response.is_redirect:
                break
            target = url.join(response.headers["location"])
            if target.path.startswith(SIGN_IN_PATH):
                raise AuthRequired()
            if not follow_redirects:
                raise AuthRequired(REDIRECTED.format(path=target.path))
            if target.scheme != "https" or target.host != self._host or target.port is not None:
                raise UnsafeRequest(f"refused to follow a redirect to {target.scheme}://{target.host}{target.path}")
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise NetworkError(f"Luk redirected more than {MAX_REDIRECTS} times")
            url = target

        content_type = response.headers.get("content-type", "")
        tree = HTMLParser(response.text) if _media_type(content_type) == "text/html" else None
        if self.is_private:
            self._classify_private(response, tree, url, requested_path)
            self._persist_cookies()
            if response.status_code == 200 and not redirects and self._store is not None:
                self._store.touch_auth_ok()
        warnings = _locale_warnings(tree) if tree is not None and response.status_code == 200 else ()
        return Fetched(
            url=str(url), status=response.status_code, content_type=content_type, text=response.text,
            redirected=redirects > 0, warnings=warnings,
        )

    @staticmethod
    def _classify_private(
        response: httpx.Response, tree: HTMLParser | None, url: httpx.URL, requested_path: str
    ) -> None:
        if response.status_code == 401:
            raise AuthRequired()
        if url.path.rstrip("/") != requested_path.rstrip("/"):
            raise AuthRequired(REDIRECTED.format(path=url.path))
        if response.status_code == 200 and tree is not None and tree.css_first(ANONYMOUS_MARKER) is not None:
            raise AuthRequired()

    def _persist_cookies(self) -> None:
        """Write rotated stored cookies back (CAS); after a lost CAS, drop the in-memory cookies."""
        with self._write_lock:
            session = self._session
            if session is None or self._store is None:
                return
            jar = self._client.cookies.jar
            updates = cookie_updates(jar, session.state)
            if not updates:
                return
            try:
                fresh = self._store.write_back(session, updates)
            except SessionBusy:
                return
            if fresh is None:
                jar.clear()
            else:
                self._session = fresh


def _locale_warnings(tree: HTMLParser) -> tuple[str, ...]:
    node = tree.css_first("html")
    lang = (node.attributes.get("lang") if node is not None else None) or ""
    if lang.lower().startswith("es"):
        return ()
    return (f"Luk served a page with lang='{lang or 'missing'}' instead of es-CL; some fields may not parse",)


class AlgoliaClient:
    """Query suggestions (§4.7): its own httpx.Client, no cookie jar, no Origin/Referer.

    `resolve(refresh)` returns the AlgoliaConfig (env override, 24 h cache or discovery from `/`);
    on 401/403 it is called once with refresh=True, then AlgoliaRejected (exit 4).
    """

    def __init__(
        self,
        settings: Settings,
        resolve: Callable[[bool], AlgoliaConfig],
        *,
        transport: httpx.BaseTransport | None = None,
        verbose: bool = False,
        stderr: TextIO | None = None,
        limiter: Limiter = ALGOLIA_LIMITER,
        sleep: Sleep = time.sleep,
        uniform: Uniform = random.uniform,
        clock: Clock = time.time,
    ) -> None:
        self._resolve = resolve
        self._client = _httpx_client(
            settings, _no_cookie_jar(), algolia_request_guard,
            transport=transport, verbose=verbose, stderr=stderr, headers={"Accept": JSON_ACCEPT},
        )
        self._attempts = _Attempts(
            self._client, limiter, what="Algolia", classify_blocks=False, sleep=sleep, uniform=uniform, clock=clock
        )

    def query_suggestions(self, query: str, hits_per_page: int) -> Fetched:
        """POST the `*/queries` body for `query`; the raw JSON goes to the Algolia parser."""
        config = self._resolve(False)
        rediscovered = False
        while True:
            response = self._attempts.send(partial(self._build, config, query, hits_per_page))
            if response.status_code not in (401, 403):
                break
            if rediscovered:
                raise AlgoliaRejected(
                    f"Algolia refused the suggestions request (HTTP {response.status_code}) — `suggest` is disabled"
                )
            config = self._resolve(True)
            rediscovered = True
        return Fetched(
            url=str(response.request.url), status=response.status_code,
            content_type=response.headers.get("content-type", ""), text=response.text,
        )

    def _build(self, config: AlgoliaConfig, query: str, hits_per_page: int) -> httpx.Request:
        body = {
            "requests": [
                {
                    "indexName": config.index,
                    "query": query,
                    "hitsPerPage": hits_per_page,
                    "attributesToRetrieve": ["query", "popularity"],
                }
            ]
        }
        return self._client.build_request(
            "POST", config.queries_url, json=body,
            headers={"X-Algolia-Application-Id": config.app_id, "X-Algolia-API-Key": config.api_key},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AlgoliaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
