"""Login, refresh and paste-cookie (spec §1.1, §1.2, §4.2–§4.4). Playwright is imported lazily.

Mechanism (site verified live 2026-09-23, anonymous; the Playwright 1.44.0 sync API used below was
checked by introspection of the installed package 2026-09-24):
- `/users/sign_in` offers email+password (`form#new_user`, no remember-me), "Continuar con Google"
  (POST `/users/auth/google_oauth2`) and LinkedIn (POST `/users/auth/linkedin`). The only credential
  is `_portal_de_empleos_session` (secure, httponly, lax, no Expires); expiry is server-side.
- `/saved_jobs` answers an anonymous request with `302 Location: …/users/sign_in`, so it is the
  authoritative probe: 200 = logged in; a 3xx elsewhere (e.g. `/onboarding`) = logged in with an
  incomplete profile. `APIRequestContext.get(path, max_redirects=0)` returns the 3xx unfollowed and
  shares the window's cookies.
- Logged-in header markers (`[aria-label^="Avatar de"]`, `.header-dropdown-name`,
  `.header-dropdown-email`) are unverified (seen during design, never captured): they only trigger an early probe
  and supply a best-effort name/email, never success.
- Playwright ≥1.44 keeps the browser alive after its last window closes, so "cancelled" is read from
  an empty `context.pages`, never from a `disconnected` event.

Rules: the user types their own credentials in a headed window; this module never reads or fills
the login form and never prints a URL (at most a redirect's path: OAuth `code`/`state` live in
queries). No HAR/trace/video, no request/response/console listeners, no extra launch args, no
persistent context on the user's profile. Every probe takes a rate-limiter slot first and sends the
honest luk-cli UA. Every user-facing line, outcome included, goes through `notify` (stderr; the CLI
prints nothing of its own for login/refresh); `LoginResult.warnings` repeats the warnings for
callers and must not be printed again.
"""

from __future__ import annotations

import getpass
import re
import sys
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from luk_cli import __version__
from luk_cli.config import SESSION_COOKIE, SIGN_IN_PATH, Settings
from luk_cli.errors import AuthRequired, Blocked, InvalidArgument, NetworkError, RateLimited
from luk_cli.http import ACCEPT_LANGUAGE, HTML_ACCEPT, Limiter, LukClient
from luk_cli.inputs import bounded_int
from luk_cli.models import SessionMeta, StorageState, StoredCookie
from luk_cli.session import LoadedSession, SessionStore

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, BrowserType, Page, Playwright
    from playwright.sync_api import StorageState as PlaywrightState

BrowserChoice = Literal["auto", "chromium", "chrome", "msedge"]
LoginState = Literal["waiting", "success", "success_incomplete", "cancelled"]
Outcome = Literal["already", "success", "success_incomplete"]
SESSION_VALUE_RE = re.compile(r"^[A-Za-z0-9%+/=._-]{32,4096}$")
PROBE_PATH = "/saved_jobs"
POLL_MS = 1000
PROBE_EVERY_S = 2.0  # floor between two probes (§4.2 step 5)
PROBE_IDLE_S = 10.0  # after a "not yet", re-probe an unchanged window only this often (hourly budget)
GOOGLE_HOST = "accounts.google.com"
GOOGLE_REJECTED_MARKER = "/signin/rejected"  # on accounts.google.com → print the fallbacks at once
LOGGED_IN_MARKERS = '[aria-label^="Avatar de"], #header-user-menu .header-dropdown-name, .header-dropdown-email'
NAME_SELECTOR = ".header-dropdown-name"
EMAIL_SELECTOR = ".header-dropdown-email"
PASTE_BROWSER = "paste-cookie"
MAX_PASTE_LINES = 200
_CONTINUATION = ("\\", "^")  # bash / cmd line continuations of a "Copy as cURL" paste
_PASTED_PAIR_RE = re.compile(rf"(?<![A-Za-z0-9_]){SESSION_COOKIE}=([^;\s'\"]+)")
# Another cookie's name=value: in a raw session value, base64 padding is only followed by =, -- or the end.
_OTHER_PAIR_RE = re.compile(r"=[^=-]")

ANNOUNCE = (
    "Opening a browser window. Log in to Luk with your own account (email, Google or LinkedIn). "
    "The window closes automatically once you're in."
)
_FALLBACK_LIST = (
    "retry with `luk login --browser msedge` or `--browser chrome`; use LinkedIn or email + password "
    '("¿Olvidaste tu contraseña?" sets a password on a Google-only account); or run '
    "`luk login --paste-cookie` in your own terminal."
)
FALLBACKS = "If Google refuses the window: " + _FALLBACK_LIST
GOOGLE_REFUSED = "Google refused to sign in from this window (it stays open): " + _FALLBACK_LIST
INCOMPLETE = "profile incomplete — finish onboarding in the browser"
NAME_UNAVAILABLE = "Logged in (name unavailable — run `luk debug capture /`)"
PASTE_PROMPT = (
    f"Paste {SESSION_COOKIE} (its value, a Cookie header or a \"Copy as cURL\" string; input is hidden): "
)
PASTE_CHECK = f"Checking the pasted cookie with one request to {PROBE_PATH}…"
PASTE_DONE = "Logged in with the pasted cookie (name unavailable — run `luk whoami`)"
PASTE_AGAIN = "Copy it again from a Luk tab where you are logged in, or run `luk login`."


class BrowserSession(Protocol):
    """One headed browser context (the only thing the login loop touches)."""

    def goto(self, path: str) -> None:
        """Navigate the first page to `path` on base_url (only `/users/sign_in`)."""

    def page_urls(self) -> list[str]:
        """URLs of the open pages; [] once every page is closed. Swallows playwright Error."""

    def wait(self, ms: int) -> None:
        """`page.wait_for_timeout(ms)`; on playwright Error (navigating/crashed page) it sleeps instead."""

    def probe(self, path: str) -> tuple[int | None, str | None]:
        """`context.request.get(path, max_redirects=0)` with the luk-cli UA → (status, Location).
        (None, None) when no answer arrived; a `cf-mitigated` answer raises Blocked."""

    def has_login_marker(self) -> bool:
        """A logged-in header marker on an open Luk page: a local DOM query, asked once per poll. It
        only ever triggers an early probe; swallows playwright Error."""

    def identity(self) -> tuple[str | None, str | None]:
        """Best-effort (name, email) from `.header-dropdown-name` / `.header-dropdown-email`."""

    def storage_state(self) -> dict[str, Any]:
        """`context.storage_state()` as a dict — never with `path=` (it is filtered before writing)."""

    def close(self) -> None:
        """`browser.close()`: also deletes the temp profile holding Google/LinkedIn cookies."""


class BrowserDriver(Protocol):
    def launch(
        self, *, browser: BrowserChoice, base_url: str, user_agent: str, storage_state: dict[str, Any] | None
    ) -> tuple[BrowserSession, str]:
        """Headed launch (§4.2 step 4): `auto` = bundled Chromium if its executable exists, else
        channel msedge, else chrome. locale es-CL, 1280×800, no extra args. `user_agent` is sent on
        probes only (the window keeps its real UA). Returns the session and the browser used; no
        usable browser → InvalidArgument naming `"<sys.executable>" -m playwright install chromium`."""


@dataclass(frozen=True)
class LoginResult:
    outcome: Outcome
    name: str | None
    email: str | None
    browser: str  # "chromium" | "chrome" | "msedge" | "paste-cookie" (meta.json's value for "already")
    warnings: list[str] = field(default_factory=list)


def login_state(pages: Sequence[str], probe_status: int | None, probe_location: str | None) -> LoginState:
    """Pure §4.2 step-5 decision. `pages` = open page URLs ([] after a close event → "cancelled");
    no probe yet → "waiting"; 200 → "success"; 3xx to /users/sign_in → "waiting"; any other 3xx →
    "success_incomplete" (profile incomplete — finish onboarding in the browser)."""
    if not pages:
        return "cancelled"
    if probe_status == 200:
        return "success"
    if probe_status is not None and 300 <= probe_status < 400 and probe_location:
        return "waiting" if urlsplit(probe_location).path.startswith(SIGN_IN_PATH) else "success_incomplete"
    return "waiting"


def extract_session_cookie(raw: str) -> SecretStr:
    """--paste-cookie normalisation (§4.4): accepts the raw value, `name=value`, a `Cookie:` header or a
    "Copy as cURL" string (bash or cmd); keeps only `_portal_de_empleos_session` (everything else
    discarded in memory) and requires SESSION_VALUE_RE. InvalidArgument otherwise — the message
    never echoes input."""
    match = _PASTED_PAIR_RE.search(raw)
    value = match.group(1).replace("^", "") if match else raw.strip()  # cmd cURL escapes with ^
    if not SESSION_VALUE_RE.fullmatch(value) or (match is None and _OTHER_PAIR_RE.search(value)):
        raise InvalidArgument(
            f"no usable {SESSION_COOKIE} value in what was pasted (nothing was saved)",
            hint="Paste its value (32–4096 characters of A-Z a-z 0-9 % + / = . _ -), the Cookie header "
            'or DevTools "Copy as cURL" of a www.takealuk.com request.',
        )
    return SecretStr(value)


def paste_cookie_state(value: SecretStr, host: str) -> StorageState:
    """The storage state stored after a passing probe: one host-only, secure, httpOnly, Lax session
    cookie (`expires: -1`, path `/`) for `host`, and no origins."""
    cookie = StoredCookie(
        name=SESSION_COOKIE, value=value, domain=host, path="/", expires=-1, httpOnly=True, secure=True, sameSite="Lax"
    )
    return StorageState(cookies=[cookie], origins=[])


def playwright_driver() -> BrowserDriver:
    """The real driver; the only place `playwright.sync_api` is imported."""
    return _PlaywrightDriver()


def login(
    settings: Settings,
    store: SessionStore,
    limiter: Limiter,
    *,
    browser: BrowserChoice = "auto",
    timeout_s: int | None = None,
    force: bool = False,
    notify: Callable[[str], None],
    driver: BrowserDriver | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LoginResult:
    """`luk login` (§4.2): already-logged-in probe (unless `force`) → "Already logged in as <name from
    meta.json>"; else `store.login_lock("login already in progress")`, announce + fallbacks, launch,
    poll with login_state, save filtered state + meta under session.lock, close the browser in
    `finally`. Timeout (default settings.login_timeout_s) or cancel → InvalidArgument (timeout:
    fallbacks as hint); Ctrl-C propagates (exit 130). `notify` must write to stderr; `driver` defaults
    to playwright_driver(); `transport` feeds the httpx already-logged-in probe. `force` also starts
    from an empty browser (e.g. to switch accounts); otherwise the window starts from the saved state,
    so an onboarding-stuck session can be finished."""
    timeout = bounded_int("--timeout", settings.login_timeout_s if timeout_s is None else timeout_s, 1)
    saved = None if force else _load_saved(store)
    if saved is not None and _session_valid(settings, limiter, saved, store, transport):
        meta = store.load_meta()
        name, email = (meta.name, meta.email) if meta else (None, None)
        who = name or email
        notify(f"Already logged in as {who}" if who else "Already logged in (name unavailable — run `luk whoami`)")
        return LoginResult("already", name, email, meta.browser if meta else "unknown")
    return _browser_login(
        settings, store, limiter, driver=driver, browser=browser, timeout_s=timeout,
        seed=saved.state.to_playwright() if saved else None, notify=notify, clock=clock,
    )


def refresh(
    settings: Settings,
    store: SessionStore,
    limiter: Limiter,
    *,
    browser: BrowserChoice = "auto",
    notify: Callable[[str], None],
    driver: BrowserDriver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LoginResult:
    """`luk session refresh` (§4.3): the login flow seeded with the saved state (if any); a still-valid
    session succeeds at the first probe, is re-saved and the window closes. Every request it makes is
    a browser probe, so unlike `login` it takes no httpx transport."""
    saved = _load_saved(store)
    return _browser_login(
        settings, store, limiter, driver=driver, browser=browser, timeout_s=settings.login_timeout_s,
        seed=saved.state.to_playwright() if saved else None, notify=notify, clock=clock,
    )


def paste_cookie(
    settings: Settings,
    store: SessionStore,
    limiter: Limiter,
    *,
    notify: Callable[[str], None],
    isatty: Callable[[], bool] | None = None,
    read_secret: Callable[[str], str] = getpass.getpass,
    transport: httpx.BaseTransport | None = None,
) -> LoginResult:
    """`luk login --paste-cookie` (§4.4): refuse unless `isatty()` (default `_stdin_isatty`: a terminal,
    and on Windows a real console, not NUL) with InvalidArgument "run this in your own terminal", before
    taking login.lock; under login.lock, read with getpass
    under `warnings.simplefilter("error", getpass.GetPassWarning)` (cURL continuation lines included);
    extract; probe `/saved_jobs` (no redirect followed) with an in-memory `LoadedSession.from_state` private client
    (store=None); on 200 save under session.lock, else drop the value (never printed) and raise.
    No already-logged-in check: pasting is an explicit request to replace the session."""
    if not (isatty or _stdin_isatty)():
        raise InvalidArgument("--paste-cookie reads hidden input from a terminal: run this in your own terminal")
    with store.login_lock("login already in progress"):
        state = paste_cookie_state(extract_session_cookie(_read_pasted(read_secret)), settings.host)
        notify(PASTE_CHECK)
        _probe_pasted(settings, limiter, state, transport)
        now = datetime.now(timezone.utc)
        store.save(
            state,
            SessionMeta(logged_in_at=now, last_auth_ok_at=now, browser=PASTE_BROWSER, luk_cli_version=__version__),
        )
    notify(PASTE_DONE)
    return LoginResult("success", None, None, PASTE_BROWSER)


# -- the browser flow ------------------------------------------------------------------------------

def _load_saved(store: SessionStore) -> LoadedSession | None:
    try:
        return store.load()
    except AuthRequired:  # unreadable session.json: log in again from scratch
        return None


def _session_valid(
    settings: Settings, limiter: Limiter, saved: LoadedSession, store: SessionStore,
    transport: httpx.BaseTransport | None,
) -> bool:
    try:
        with LukClient.private(settings, limiter, saved, store=store, transport=transport) as client:
            return client.get_html(PROBE_PATH, follow_redirects=False).status == 200
    except AuthRequired:
        return False


def _browser_login(
    settings: Settings,
    store: SessionStore,
    limiter: Limiter,
    *,
    driver: BrowserDriver | None,
    browser: BrowserChoice,
    timeout_s: int,
    seed: dict[str, Any] | None,
    notify: Callable[[str], None],
    clock: Callable[[], float],
) -> LoginResult:
    with store.login_lock("login already in progress"):
        notify(ANNOUNCE)
        notify(FALLBACKS)
        session, used = (driver or playwright_driver()).launch(
            browser=browser, base_url=settings.base_url, user_agent=settings.user_agent, storage_state=seed
        )
        try:
            session.goto(SIGN_IN_PATH)
            outcome, location = _poll(session, settings.host, limiter, timeout_s, notify, clock)
            state = session.storage_state()
            name, email = session.identity()
        finally:
            session.close()
        now = datetime.now(timezone.utc)
        store.save(
            state,
            SessionMeta(
                name=name, email=email, logged_in_at=now, last_auth_ok_at=now if outcome == "success" else None,
                browser=used, luk_cli_version=__version__,
            ),
        )
    # Only the redirect's path is kept: its query may carry tokens.
    result_warnings = (
        [f"{INCOMPLETE} (Luk redirected to {urlsplit(location or '').path})"]
        if outcome == "success_incomplete" else []
    )
    for warning in result_warnings:
        notify(warning)
    who = name or email
    notify(f"Logged in as {who}" if who else NAME_UNAVAILABLE)
    return LoginResult(outcome, name, email, used, result_warnings)


def _poll(
    session: BrowserSession,
    host: str,
    limiter: Limiter,
    timeout_s: int,
    notify: Callable[[str], None],
    clock: Callable[[], float],
) -> tuple[Literal["success", "success_incomplete"], str | None]:
    """Poll once per POLL_MS until login_state decides; cancel or timeout → InvalidArgument.

    A probe is warranted while a Luk page outside `/users/*` is open or a logged-in header marker
    shows. It goes out at most every PROBE_EVERY_S; after a "not yet" answer, the next one waits
    until the view (open pages + marker) changes or PROBE_IDLE_S passes, so a window left open on a
    public page cannot spend the shared hourly budget at one probe per 2 s.
    """
    deadline = clock() + timeout_s
    next_probe_at = clock()
    idle_until = next_probe_at
    answered: tuple[tuple[str, ...], bool] | None = None  # the view the last probe answered
    google_refused = False
    while True:
        pages = session.page_urls()
        if not google_refused and any(_is_google_refusal(url) for url in pages):
            google_refused = True
            notify(GOOGLE_REFUSED)
        marker = session.has_login_marker()
        view = (tuple(pages), marker)
        if view != answered:
            answered = None  # the view moved since the last answer (it may come back): re-probe early
        status = location = None
        now = clock()
        if (pages and (marker or _on_luk_app_page(pages, host)) and now >= next_probe_at
                and (answered is None or now >= idle_until)):
            limiter.acquire()
            status, location = session.probe(PROBE_PATH)
            if status == 403:
                raise Blocked.for_status(status)
            if status == 429:
                raise RateLimited("Luk answered HTTP 429 to the login probe; stopping")
            answered = view
            next_probe_at, idle_until = clock() + PROBE_EVERY_S, clock() + PROBE_IDLE_S
        state = login_state(pages, status, location)
        if state == "cancelled":
            raise InvalidArgument("Login cancelled")
        if state != "waiting":
            return state, location
        if clock() >= deadline:
            raise InvalidArgument(f"Login timed out after {timeout_s} s", hint=FALLBACKS)
        session.wait(POLL_MS)


def _is_google_refusal(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == GOOGLE_HOST and GOOGLE_REJECTED_MARKER in parts.path


def _on_luk_app_page(pages: Sequence[str], host: str) -> bool:
    """A Luk page outside `/users/*` (sign-in, OAuth callbacks): only then is a probe worth sending."""
    for url in pages:
        parts = urlsplit(url)
        if parts.hostname == host and not (parts.path == "/users" or parts.path.startswith("/users/")):
            return True
    return False


# -- paste-cookie ------------------------------------------------------------------------------------

def _stdin_isatty() -> bool:
    """stdin is an interactive terminal. On Windows `isatty()` is also True for NUL (a character
    device: `< NUL`, Claude Code's PowerShell tool, Task Scheduler), where getpass would read an
    invisible console and hang holding login.lock, so there the handle must also be a console."""
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        return False
    return sys.platform != "win32" or _is_windows_console(stdin)


def _is_windows_console(stream: Any) -> bool:
    """GetConsoleMode succeeds only on a console handle (it fails for NUL, pipes and files)."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
    except (OSError, ValueError, AttributeError):
        return False
    mode = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    return bool(kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)))


def _read_pasted(read_secret: Callable[[str], str]) -> str:
    """One hidden line, plus the continuation lines of a multi-line cURL paste (else they would reach
    the shell after exit)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            lines = [read_secret(PASTE_PROMPT)]
            while lines[-1].rstrip().endswith(_CONTINUATION) and len(lines) < MAX_PASTE_LINES:
                lines.append(read_secret(""))
        except getpass.GetPassWarning:
            raise InvalidArgument("cannot hide the input here: run this in your own terminal") from None
        except EOFError:
            raise InvalidArgument("no cookie was pasted") from None
    return "\n".join(lines)


def _probe_pasted(
    settings: Settings, limiter: Limiter, state: StorageState, transport: httpx.BaseTransport | None
) -> None:
    probe = LoadedSession.from_state(state)  # registers the value for log redaction
    try:
        with LukClient.private(settings, limiter, probe, store=None, transport=transport) as client:
            status = client.get_html(PROBE_PATH, follow_redirects=False).status
    except AuthRequired as err:
        # The client's specific reason (the onboarding redirect) beats the generic "expired" one.
        hint = PASTE_AGAIN if err.message == AuthRequired.default_message else err.message
        raise AuthRequired("Luk did not accept the pasted cookie; nothing was saved.", hint=hint) from None
    if status != 200:
        raise NetworkError(f"Luk answered HTTP {status} to the {PROBE_PATH} probe; nothing was saved")


# -- the real driver (Playwright sync API, imported lazily) ------------------------------------------

def _install_hint() -> str:
    return f'Run "{sys.executable}" -m playwright install chromium, or try --browser msedge / --browser chrome.'


def _launch(chromium: BrowserType, choice: BrowserChoice, error: type[Exception]) -> tuple[Browser, str]:
    """§4.2 step 4: headed, no extra args; `auto` = bundled Chromium if its executable exists, else
    channel msedge, else chrome. A browser that fails to start makes `auto` try the next one; the
    error names the first line of the last failure (e.g. "Executable doesn't exist at …")."""
    if choice == "auto":
        order = (["chromium"] if Path(chromium.executable_path).exists() else []) + ["msedge", "chrome"]
    else:
        order = [choice]
    failure: Exception | None = None
    for name in order:
        try:
            return chromium.launch(headless=False, channel=None if name == "chromium" else name), name
        except error as err:
            failure = err
    lines = str(failure).strip().splitlines()
    raise InvalidArgument(
        f"could not start {'a browser' if choice == 'auto' else choice} for the login window "
        f"({lines[0] if lines else type(failure).__name__})",
        hint=_install_hint(),
    )


class _PlaywrightDriver:
    def launch(
        self, *, browser: BrowserChoice, base_url: str, user_agent: str, storage_state: dict[str, Any] | None
    ) -> tuple[BrowserSession, str]:
        from playwright.sync_api import Error, sync_playwright

        with ExitStack() as cleanup:
            playwright = sync_playwright().start()
            cleanup.callback(playwright.stop)
            instance, used = _launch(playwright.chromium, browser, Error)
            cleanup.callback(instance.close)
            context = instance.new_context(
                base_url=base_url, locale="es-CL", viewport={"width": 1280, "height": 800},
                storage_state=cast("PlaywrightState | None", storage_state),
            )
            context.new_page()
            cleanup.pop_all()
        headers = {"User-Agent": user_agent, "Accept": HTML_ACCEPT, "Accept-Language": ACCEPT_LANGUAGE}
        host = urlsplit(base_url).hostname or ""
        return _PlaywrightSession(playwright, instance, context, host, headers, Error), used


class _PlaywrightSession:
    """BrowserSession over one context; Playwright errors from navigating/closed pages are swallowed."""

    def __init__(
        self, playwright: Playwright, browser: Browser, context: BrowserContext, host: str,
        probe_headers: Mapping[str, str], error: type[Exception],
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._host = host
        self._probe_headers = dict(probe_headers)
        self._error = error

    def _pages(self) -> list[Page]:
        return [page for page in self._context.pages if not page.is_closed()]

    def _luk_pages(self) -> list[Page]:
        return [page for page in self._pages() if urlsplit(page.url).hostname == self._host]

    def goto(self, path: str) -> None:
        try:
            self._pages()[0].goto(path, wait_until="commit")
        except self._error:
            raise NetworkError("the login window could not open Luk's sign-in page") from None

    def page_urls(self) -> list[str]:
        return [page.url for page in self._pages()]

    def wait(self, ms: int) -> None:
        pages = self._pages()
        if not pages:
            return  # the next poll sees the window gone ("cancelled")
        try:
            pages[0].wait_for_timeout(ms)
        except self._error:  # a navigating/crashed page fails at once: still wait, never spin
            time.sleep(ms / 1000)

    def probe(self, path: str) -> tuple[int | None, str | None]:
        try:
            response = self._context.request.get(path, headers=self._probe_headers, max_redirects=0)
        except self._error:
            return None, None
        try:
            if "cf-mitigated" in response.headers:
                raise Blocked.for_status(response.status)
            return response.status, response.headers.get("location")
        finally:
            with suppress(self._error):
                response.dispose()

    def has_login_marker(self) -> bool:
        for page in self._luk_pages():
            with suppress(self._error):
                if page.query_selector(LOGGED_IN_MARKERS) is not None:
                    return True
        return False

    def identity(self) -> tuple[str | None, str | None]:
        return self._text(NAME_SELECTOR), self._text(EMAIL_SELECTOR)

    def _text(self, selector: str) -> str | None:
        for page in self._luk_pages():
            try:
                node = page.query_selector(selector)
                text = node.text_content() if node is not None else None
            except self._error:
                continue
            if text and text.strip():
                return " ".join(text.split())
        return None

    def storage_state(self) -> dict[str, Any]:
        return dict(self._context.storage_state())

    def close(self) -> None:
        with suppress(self._error):
            self._browser.close()
        with suppress(self._error):
            self._playwright.stop()
