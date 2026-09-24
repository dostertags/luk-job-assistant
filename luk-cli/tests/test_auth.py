"""auth.py — login_state table, login/refresh with a FAKE browser driver, paste-cookie (spec §4.2–§4.4).

Playwright is never imported here: the browser is a scripted fake whose `screens[i]` are the open
page URLs until the i-th `wait()` (each wait advances the fake clock by 1 s).
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import warnings
from datetime import datetime, timezone

import httpx
import pytest
from filelock import FileLock
from pydantic import SecretStr

from luk_cli import auth
from luk_cli.errors import AuthRequired, Blocked, InvalidArgument, NetworkError, RateLimited, SessionBusy
from luk_cli.models import SessionMeta
from luk_cli.session import SessionStore

LUK = "https://www.takealuk.com"
SIGN_IN = LUK + "/users/sign_in"
HOME = LUK + "/"
JOBS = LUK + "/job_offers?job_positions=analista"
COMPANIES = LUK + "/companies"
ONBOARDING = LUK + "/onboarding"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth?client_id=luk&state=OAUTHSTATE"
GOOGLE_PASSWORD = "https://accounts.google.com/v3/signin/challenge/pwd?TL=OAUTHSTATE"
GOOGLE_REJECTED = "https://accounts.google.com/v3/signin/rejected?continue=OAUTHSTATE"
CALLBACK = LUK + "/users/auth/google_oauth2/callback?code=OAUTHCODE&state=OAUTHSTATE#frag"
VALUE = "Zm9vYmFyYmF6cXV4" * 3 + "%3D%3D--" + "c2lnbmF0dXJl" * 2  # shape of a Rails cookie-store value


def cookie(name: str, domain: str, value: str = "v" * 20, **kw: object) -> dict:
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1,
            "httpOnly": False, "secure": True, "sameSite": "Lax", **kw}


def browser_state(value: str = VALUE) -> dict:
    """What `context.storage_state()` returns after a Google login: mostly not Luk's to keep."""
    return {
        "cookies": [
            cookie("_portal_de_empleos_session", "www.takealuk.com", value, httpOnly=True),
            cookie("_ga", ".takealuk.com"),
            cookie("SID", ".google.com"),
            cookie("li_at", ".www.linkedin.com"),
            cookie("stolen", "eviltakealuk.com"),
        ],
        "origins": [
            {"origin": "https://www.takealuk.com", "localStorage": []},
            {"origin": "https://accounts.google.com", "localStorage": [{"name": "k", "value": "v" * 12}]},
        ],
    }


class FakeBrowser:
    """Scripted auth.BrowserSession."""

    def __init__(self, clock, screens, probes=(), *, identity=(None, None), marker=False, state=None):
        self.clock = clock
        self.screens = [list(s) for s in screens]
        self.probes = list(probes)
        self._identity = identity
        self.marker = marker
        self.state = state if state is not None else browser_state()
        self.step = 0
        self.events: list[tuple] = []
        self.closed = False

    def goto(self, path):
        self.events.append(("goto", path))

    def page_urls(self):
        return list(self.screens[min(self.step, len(self.screens) - 1)])

    def wait(self, ms):
        self.events.append(("wait", ms))
        self.clock.sleep(ms / 1000)
        self.step += 1

    def probe(self, path):
        self.events.append(("probe", path, tuple(self.page_urls()), self.clock.time()))
        return self.probes.pop(0)

    def has_login_marker(self):
        return self.marker

    def identity(self):
        return self._identity

    def storage_state(self):
        self.events.append(("storage_state",))
        return self.state

    def close(self):
        self.closed = True
        self.events.append(("close",))


class InterruptedBrowser(FakeBrowser):
    def wait(self, ms):
        raise KeyboardInterrupt


class FakeDriver:
    def __init__(self, browser, used="chromium"):
        self.browser, self.used, self.launches = browser, used, []

    def launch(self, **kwargs):
        self.launches.append(kwargs)
        return self.browser, self.used


class RecordingLimiter:
    def __init__(self, events):
        self.events = events

    def acquire(self):
        self.events.append(("acquire",))


class Harness:
    def __init__(self, settings, store, clock):
        self.settings, self.store, self.clock = settings, store, clock
        self.notes: list[str] = []
        self.driver: FakeDriver | None = None

    def run(self, browser, fn=auth.login, **kw):
        self.driver = FakeDriver(browser)
        return fn(self.settings, self.store, RecordingLimiter(browser.events), notify=self.notes.append,
                  driver=self.driver, clock=self.clock.time, **kw)

    def browser(self, screens, probes=(), **kw):
        return FakeBrowser(self.clock, screens, probes, **kw)


@pytest.fixture
def h(settings, store, fake_clock):
    return Harness(settings, store, fake_clock)


def probes_of(browser):
    return [e for e in browser.events if e[0] == "probe"]


def saved_json(settings):
    return json.loads(settings.session_path.read_text("utf-8"))


# -- login_state: the pure §4.2 step-5 decision --------------------------------------------------

@pytest.mark.parametrize(
    ("pages", "status", "location", "expected"),
    [
        ([], None, None, "cancelled"),
        ([], 200, None, "cancelled"),
        ([SIGN_IN], None, None, "waiting"),
        ([GOOGLE_AUTH], None, None, "waiting"),
        ([HOME], 200, None, "success"),
        ([HOME, GOOGLE_AUTH], 200, None, "success"),
        ([HOME], 302, SIGN_IN, "waiting"),
        ([HOME], 302, "/users/sign_in", "waiting"),
        ([HOME], 303, SIGN_IN + "?redirect=1", "waiting"),
        ([HOME], 302, ONBOARDING, "success_incomplete"),
        ([HOME], 307, "/onboarding/step-2", "success_incomplete"),
        ([HOME], 302, None, "waiting"),
        ([HOME], 401, None, "waiting"),
        ([HOME], 500, None, "waiting"),
        ([HOME], None, None, "waiting"),
    ],
)
def test_login_state_table(pages, status, location, expected):
    assert auth.login_state(pages, status, location) == expected


# -- login with a fake browser ---------------------------------------------------------------------

def test_success_only_after_probe_200_through_google(h, settings):
    # The user first wanders to the home page (probe: not yet), then signs in with Google.
    browser = h.browser(
        [[SIGN_IN], [HOME], [SIGN_IN], [GOOGLE_AUTH], [GOOGLE_PASSWORD], [CALLBACK], [HOME]],
        probes=[(302, SIGN_IN), (200, None)],
        identity=("Paz Prueba", "paz@example.com"),
    )
    result = h.run(browser)

    assert result == auth.LoginResult("success", "Paz Prueba", "paz@example.com", "chromium", [])
    assert h.driver.launches == [
        {"browser": "auto", "base_url": settings.base_url, "user_agent": settings.user_agent, "storage_state": None}
    ]
    assert browser.events[0] == ("goto", "/users/sign_in")
    probes = probes_of(browser)
    assert [p[1] for p in probes] == ["/saved_jobs", "/saved_jobs"]
    assert all(p[2] == (HOME,) for p in probes)  # never on Google, /users/* or the OAuth callback
    # Back on the home page after a round trip: re-probed as soon as the 2 s floor allows.
    assert auth.PROBE_EVERY_S <= probes[1][3] - probes[0][3] < auth.PROBE_IDLE_S
    for i, event in enumerate(browser.events):
        if event[0] == "probe":
            assert browser.events[i - 1] == ("acquire",)  # every probe goes through the rate limiter
    assert browser.closed
    assert browser.events.index(("storage_state",)) < browser.events.index(("close",))

    saved = saved_json(settings)
    assert [c["name"] for c in saved["cookies"]] == ["_portal_de_empleos_session"]
    assert saved["cookies"][0]["value"] == VALUE
    assert [o["origin"] for o in saved["origins"]] == ["https://www.takealuk.com"]
    meta = h.store.load_meta()
    assert (meta.name, meta.email, meta.browser) == ("Paz Prueba", "paz@example.com", "chromium")
    assert meta.last_auth_ok_at is not None

    # auth prints every line itself (the CLI prints nothing of its own for login).
    assert h.notes == [auth.ANNOUNCE, auth.FALLBACKS, "Logged in as Paz Prueba"]
    assert h.notes[0].startswith("Opening a browser window. Log in to Luk with your own account")
    assert "--paste-cookie" in h.notes[1] and "--browser msedge" in h.notes[1]
    text = "\n".join(h.notes)
    for secret in ("OAUTHCODE", "OAUTHSTATE", "frag", VALUE):
        assert secret not in text


def test_profile_incomplete_saves_and_warns_with_the_path_only(h, settings):
    location = ONBOARDING + "?step=2&token=ONBOARDTOKEN#frag"
    browser = h.browser([[SIGN_IN], [ONBOARDING]], probes=[(302, location)], identity=("Paz", None))
    result = h.run(browser)

    assert result.outcome == "success_incomplete"
    assert result.warnings == [
        "profile incomplete — finish onboarding in the browser (Luk redirected to /onboarding)"
    ]
    assert h.notes[2:] == [result.warnings[0], "Logged in as Paz"]  # warning first, printed once
    assert "ONBOARDTOKEN" not in "\n".join(h.notes)
    assert settings.session_path.exists()
    assert h.store.load_meta().last_auth_ok_at is None
    assert browser.closed


def test_name_unavailable_still_saves(h, settings):
    browser = h.browser([[HOME]], probes=[(200, None)])
    result = h.run(browser)

    assert (result.outcome, result.name, result.email) == ("success", None, None)
    assert h.notes[-1] == "Logged in (name unavailable — run `luk debug capture /`)"
    assert settings.session_path.exists()


def test_an_idle_anonymous_window_is_probed_every_idle_interval(h, settings):
    """A window left on a public Luk page must not burn the hourly budget at one probe per 2 s."""
    browser = h.browser([[HOME]], probes=[(302, SIGN_IN)] * 10)
    start = h.clock.time()
    with pytest.raises(InvalidArgument, match="timed out"):
        h.run(browser, timeout_s=30)
    assert [p[3] - start for p in probes_of(browser)] == [0, 10, 20, 30]
    assert auth.PROBE_IDLE_S == 10
    assert not settings.session_path.exists()


def test_page_changes_reprobe_after_the_floor_only(h):
    browser = h.browser([[HOME], [JOBS], [COMPANIES], [COMPANIES], [COMPANIES]], probes=[(302, SIGN_IN)] * 5)
    start = h.clock.time()
    with pytest.raises(InvalidArgument, match="timed out"):
        h.run(browser, timeout_s=4)
    assert [p[3] - start for p in probes_of(browser)] == [0, 2]  # JOBS at t=1 waits for the 2 s floor


def test_a_marker_appearing_on_an_unchanged_page_reprobes_early(h):
    class OneTap(FakeBrowser):
        """Google One Tap on the home page: the URL never changes, the header does."""

        def has_login_marker(self):
            return self.step >= 3

    browser = OneTap(h.clock, [[HOME]], probes=[(302, SIGN_IN), (200, None)])
    start = h.clock.time()
    assert h.run(browser).outcome == "success"
    assert [p[3] - start for p in probes_of(browser)] == [0, 3]


@pytest.mark.parametrize("marker", [True, False])
def test_header_marker_only_triggers_an_early_probe(h, settings, marker):
    browser = h.browser([[LUK + "/users/edit"]], probes=[(200, None)], marker=marker)
    if marker:
        assert h.run(browser, timeout_s=5).outcome == "success"
    else:
        with pytest.raises(InvalidArgument, match="timed out"):
            h.run(browser, timeout_s=5)
        assert probes_of(browser) == []
        assert not settings.session_path.exists()


def test_marker_never_gates_success(h, settings):
    browser = h.browser([[HOME]], probes=[(302, SIGN_IN), (302, SIGN_IN)], marker=True)
    with pytest.raises(InvalidArgument, match="timed out"):
        h.run(browser, timeout_s=int(auth.PROBE_IDLE_S) + 2)
    assert len(probes_of(browser)) == 2  # a marker that keeps showing does not re-probe every 2 s
    assert not settings.session_path.exists()


def test_cancel_when_the_window_closes(h, settings):
    browser = h.browser([[SIGN_IN], [GOOGLE_AUTH], []])
    with pytest.raises(InvalidArgument, match="Login cancelled"):
        h.run(browser)
    assert browser.closed
    assert probes_of(browser) == []
    assert not settings.session_path.exists()


def test_timeout_prints_fallbacks_and_closes(h, settings):
    browser = h.browser([[SIGN_IN]])
    start = h.clock.time()
    with pytest.raises(InvalidArgument, match="Login timed out after 5 s") as info:
        h.run(browser, timeout_s=5)
    assert "--paste-cookie" in info.value.hint and "--browser msedge" in info.value.hint
    assert h.clock.time() - start >= 5
    assert probes_of(browser) == []
    assert browser.closed
    assert not settings.session_path.exists()


def test_default_timeout_comes_from_settings(h, settings):
    browser = h.browser([[SIGN_IN]])
    start = h.clock.time()
    with pytest.raises(InvalidArgument, match=f"after {settings.login_timeout_s} s"):
        h.run(browser)
    assert h.clock.time() - start >= settings.login_timeout_s


def test_timeout_must_be_positive(h):
    browser = h.browser([[SIGN_IN]])
    with pytest.raises(InvalidArgument, match="--timeout"):
        h.run(browser, timeout_s=0)
    assert h.driver.launches == []


def test_google_refusal_prints_fallbacks_and_keeps_the_window(h):
    browser = h.browser([[SIGN_IN], [GOOGLE_AUTH], [GOOGLE_REJECTED], [GOOGLE_REJECTED], []])
    with pytest.raises(InvalidArgument, match="Login cancelled"):
        h.run(browser)
    refusals = [n for n in h.notes if "Google refused" in n]
    assert len(refusals) == 1
    assert "--browser msedge" in refusals[0] and "--paste-cookie" in refusals[0]
    assert "OAUTHSTATE" not in "\n".join(h.notes)
    assert [e for e in browser.events if e[0] == "wait"] == [("wait", auth.POLL_MS)] * 4  # kept polling


@pytest.mark.parametrize(("status", "error"), [(403, Blocked), (429, RateLimited)])
def test_blocked_probe_stops_at_once(h, settings, status, error):
    browser = h.browser([[HOME]], probes=[(status, None)])
    with pytest.raises(error):
        h.run(browser)
    assert len(probes_of(browser)) == 1
    assert browser.closed
    assert not settings.session_path.exists()


def test_ctrl_c_closes_the_browser_and_releases_the_lock(h, settings):
    browser = InterruptedBrowser(h.clock, [[SIGN_IN]])
    with pytest.raises(KeyboardInterrupt):
        h.run(browser)
    assert browser.closed
    assert not settings.session_path.exists()
    with SessionStore(settings).login_lock("still held"):
        pass


def test_login_lock_contention(h, settings):
    settings.login_lock_path.parent.mkdir(parents=True, exist_ok=True)
    browser = h.browser([[HOME]], probes=[(200, None)])
    with FileLock(str(settings.login_lock_path)), pytest.raises(SessionBusy, match="login already in progress"):
        h.run(browser)
    assert h.driver.launches == []
    assert h.notes == []


# -- already logged in, --force, refresh -----------------------------------------------------------

def luk_transport(status, calls, location=None):
    """takealuk.com mock: `status` for every request (a logged-in-looking page when 200)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status == 200:
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  text="<html lang='es-CL'><header id='main-header'></header></html>")
        return httpx.Response(status, headers={"location": location} if location else {})

    return httpx.MockTransport(handler)


def save_with_meta(store, make_state, name="Paz"):
    meta = SessionMeta(name=name, logged_in_at=datetime.now(timezone.utc), browser="chromium",
                       luk_cli_version="0.1.0")
    return store.save(make_state(), meta)


def test_already_logged_in_skips_the_browser(h, store, make_state, session_value):
    save_with_meta(store, make_state)
    calls: list[httpx.Request] = []
    browser = h.browser([[HOME]])
    result = h.run(browser, transport=luk_transport(200, calls))

    assert (result.outcome, result.name, result.browser) == ("already", "Paz", "chromium")
    assert h.driver.launches == []
    assert h.notes == ["Already logged in as Paz"]
    assert [c.url.path for c in calls] == ["/saved_jobs"]
    assert calls[0].headers["cookie"] == f"_portal_de_empleos_session={session_value}"


def test_force_skips_the_check_and_starts_fresh(h, store, make_state):
    save_with_meta(store, make_state)
    calls: list[httpx.Request] = []
    browser = h.browser([[HOME]], probes=[(200, None)], identity=("Otra Cuenta", None))
    result = h.run(browser, force=True, transport=luk_transport(200, calls))

    assert result.outcome == "success"
    assert calls == []
    assert h.driver.launches[0]["storage_state"] is None
    assert h.store.load_meta().name == "Otra Cuenta"


def test_expired_session_opens_the_browser_seeded_with_it(h, store, make_state):
    loaded = save_with_meta(store, make_state)
    calls: list[httpx.Request] = []
    browser = h.browser([[HOME]], probes=[(200, None)], identity=("Paz", None))
    result = h.run(browser, transport=luk_transport(302, calls, location=SIGN_IN))

    assert result.outcome == "success"
    assert len(calls) == 1
    assert h.driver.launches[0]["storage_state"] == loaded.state.to_playwright()


def test_the_already_logged_in_probe_never_follows_a_redirect(h, store, make_state):
    """Step 1's probe is the §4.2 probe (`max_redirects=0`): a 302 to onboarding means "not logged in" after
    one request; its Location is never requested with the session cookie."""
    save_with_meta(store, make_state)
    calls: list[httpx.Request] = []
    browser = h.browser([[HOME]], probes=[(200, None)], identity=("Paz", None))
    result = h.run(browser, transport=luk_transport(302, calls, location=ONBOARDING))

    assert result.outcome == "success" and len(h.driver.launches) == 1
    assert [c.url.path for c in calls] == ["/saved_jobs"]


def test_refresh_reuses_the_saved_state_and_resaves(h, store, make_state, settings):
    loaded = save_with_meta(store, make_state)
    browser = h.browser([[HOME]], probes=[(200, None)], identity=("Paz", "paz@example.com"),
                        state=browser_state("r" * 48))
    result = h.run(browser, fn=auth.refresh)

    assert result.outcome == "success"
    assert h.driver.launches[0]["storage_state"] == loaded.state.to_playwright()
    assert len(probes_of(browser)) == 1
    assert saved_json(settings)["cookies"][0]["value"] == "r" * 48
    assert h.store.load_meta().email == "paz@example.com"
    assert browser.closed


def test_refresh_without_a_session_is_a_plain_login(h):
    browser = h.browser([[SIGN_IN], [HOME]], probes=[(200, None)])
    assert h.run(browser, fn=auth.refresh).outcome == "success"
    assert h.driver.launches[0]["storage_state"] is None


# -- browser choice (real driver's launch order, with a fake BrowserType) ----------------------------

class LaunchFailed(Exception):
    pass


class FakeChromium:
    def __init__(self, executable_path, fail=()):
        self.executable_path = str(executable_path)
        self.fail = set(fail)
        self.calls: list[dict] = []

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["channel"] or "chromium"
        if name in self.fail:
            raise LaunchFailed(name)
        return f"browser:{name}"


@pytest.mark.parametrize(
    ("choice", "bundled", "fail", "expected", "tried"),
    [
        ("auto", True, (), "chromium", [None]),
        ("auto", False, (), "msedge", ["msedge"]),
        ("auto", False, ("msedge",), "chrome", ["msedge", "chrome"]),
        ("auto", True, ("chromium",), "msedge", [None, "msedge"]),
        ("chromium", False, (), "chromium", [None]),
        ("msedge", True, (), "msedge", ["msedge"]),
        ("chrome", True, (), "chrome", ["chrome"]),
    ],
)
def test_launch_order(tmp_path, choice, bundled, fail, expected, tried):
    exe = tmp_path / "chrome.exe"
    if bundled:
        exe.write_bytes(b"")
    chromium = FakeChromium(exe, fail)
    assert auth._launch(chromium, choice, LaunchFailed) == (f"browser:{expected}", expected)
    assert chromium.calls == [{"headless": False, "channel": c} for c in tried]  # no args, never headless


@pytest.mark.parametrize(("choice", "last"), [("auto", "chrome"), ("chromium", "chromium"), ("msedge", "msedge")])
def test_launch_failure_names_the_cause_and_the_install_command(tmp_path, choice, last):
    chromium = FakeChromium(tmp_path / "missing.exe", fail=("chromium", "msedge", "chrome"))
    with pytest.raises(InvalidArgument) as info:
        auth._launch(chromium, choice, LaunchFailed)
    assert info.value.message.endswith(f"for the login window ({last})")  # first line of the last error
    assert f'"{sys.executable}" -m playwright install chromium' in info.value.hint


# -- the real driver's session, over fake Playwright objects (never imported) ------------------------

class FakePage:
    def __init__(self, url, *, closed=False, crashed=False, texts=None):
        self.url, self.closed, self.crashed, self.texts = url, closed, crashed, texts or {}
        self.waits: list[float] = []

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        if self.crashed:
            raise LaunchFailed("Target crashed")
        self.waits.append(ms)

    def query_selector(self, selector):
        if self.crashed:
            raise LaunchFailed("Target crashed")
        text = self.texts.get(selector)
        return None if text is None else type("Node", (), {"text_content": lambda self: text})()


class FakeResponse:
    def __init__(self, status, headers):
        self.status, self.headers, self.disposed = status, headers, False

    def dispose(self):
        self.disposed = True


class FakeRequest:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeContext:
    def __init__(self, pages, answer=None):
        self.pages, self.request = pages, FakeRequest(answer)


class FakeClosable:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def close(self):
        self.calls += 1
        if self.fail:
            raise LaunchFailed("Browser has been closed")

    stop = close


def real_session(settings, pages, answer=None, *, browser=None, playwright=None):
    headers = {"User-Agent": settings.user_agent, "Accept": auth.HTML_ACCEPT, "Accept-Language": "es-CL"}
    return auth._PlaywrightSession(playwright or FakeClosable(), browser or FakeClosable(),
                                   FakeContext(pages, answer), settings.host, headers, LaunchFailed)


def test_real_probe_sends_the_honest_ua_and_never_follows_redirects(settings):
    response = FakeResponse(302, {"location": SIGN_IN})
    session = real_session(settings, [FakePage(HOME)], response)
    assert session.probe("/saved_jobs") == (302, SIGN_IN)
    (path, kwargs), = session._context.request.calls
    assert path == "/saved_jobs" and kwargs["max_redirects"] == 0
    assert kwargs["headers"]["User-Agent"] == settings.user_agent
    assert response.disposed


def test_real_probe_without_an_answer_is_waiting_and_a_challenge_is_blocked(settings):
    assert real_session(settings, [FakePage(HOME)], LaunchFailed("net::ERR")).probe("/saved_jobs") == (None, None)
    challenged = FakeResponse(403, {"cf-mitigated": "challenge"})
    with pytest.raises(Blocked):
        real_session(settings, [FakePage(HOME)], challenged).probe("/saved_jobs")
    assert challenged.disposed


def test_real_pages_skip_closed_ones_and_wait_never_spins(settings, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(auth.time, "sleep", slept.append)
    live, crashed = FakePage(HOME), FakePage(JOBS, crashed=True)
    assert real_session(settings, [FakePage(SIGN_IN, closed=True), live]).page_urls() == [HOME]
    real_session(settings, [live]).wait(1000)
    assert (live.waits, slept) == ([1000], [])
    real_session(settings, [crashed]).wait(1000)  # wait_for_timeout fails at once: sleep instead
    assert slept == [1.0]


def test_real_identity_and_marker_read_luk_pages_only(settings):
    google = FakePage(GOOGLE_AUTH, texts={auth.NAME_SELECTOR: "Not Luk", auth.LOGGED_IN_MARKERS: "x"})
    luk = FakePage(HOME, texts={auth.NAME_SELECTOR: "  Paz \n Prueba ", auth.LOGGED_IN_MARKERS: "x"})
    assert real_session(settings, [google, FakePage(JOBS, crashed=True), luk]).identity() == ("Paz Prueba", None)
    assert real_session(settings, [google]).has_login_marker() is False
    assert real_session(settings, [FakePage(JOBS, crashed=True), luk]).has_login_marker() is True


def test_real_close_always_stops_playwright(settings):
    browser, playwright = FakeClosable(fail=True), FakeClosable()
    real_session(settings, [], browser=browser, playwright=playwright).close()
    assert (browser.calls, playwright.calls) == (1, 1)


def test_importing_auth_does_not_import_playwright():
    code = "import sys, luk_cli.auth; sys.exit('playwright' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# -- --paste-cookie ---------------------------------------------------------------------------------

CURL_BASH = (
    "curl 'https://www.takealuk.com/saved_jobs' \\\n"
    "  -H 'accept: text/html' \\\n"
    f"  -b '_ga=GA1.1.123; _portal_de_empleos_session={VALUE}; AMP_abc=xyz' \\\n"
    "  -H 'user-agent: Mozilla/5.0'"
)
CURL_CMD = (
    'curl ^"https://www.takealuk.com/saved_jobs^" ^\n'
    '  -H ^"accept: text/html^" ^\n'
    '  -H ^"cookie: _ga=GA1.1.123; _portal_de_empleos_session='
    + VALUE.replace("%", "^%^") + '; g_state=^{^\\^"i_l^\\^":0^}^" ^\n'
    '  -H ^"user-agent: Mozilla/5.0^"'
)


@pytest.mark.parametrize(
    "raw",
    [
        VALUE,
        f"  {VALUE}\n",
        f"_portal_de_empleos_session={VALUE}",
        f"Cookie: _ga=GA1.1.123; _portal_de_empleos_session={VALUE}; AMP_abc=xyz",
        f"cookie:_portal_de_empleos_session={VALUE}",
        f"-H 'Cookie: _portal_de_empleos_session={VALUE}'",
        CURL_BASH,
        CURL_CMD,
    ],
)
def test_extract_session_cookie(raw):
    assert auth.extract_session_cookie(raw).get_secret_value() == VALUE


def test_extract_keeps_a_url_decoded_value():
    decoded = VALUE.replace("%3D", "=")  # DevTools "show URL-decoded": base64 padding before --
    assert auth.extract_session_cookie(decoded).get_secret_value() == decoded
    assert auth.extract_session_cookie(decoded + "==").get_secret_value() == decoded + "=="


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "short%3D--value",
        "x" * 4097,
        "bad value with spaces and more than thirty-two characters",
        "Cookie: _ga=GA1.1.123; other_session=" + VALUE,
        "x_portal_de_empleos_session=" + VALUE,
        "_portal_de_empleos_session=tooshort",
        "_portal_de_empleos_session=" + "ñ" * 40,
    ],
)
def test_extract_session_cookie_rejects_without_echo(raw):
    with pytest.raises(InvalidArgument) as info:
        auth.extract_session_cookie(raw)
    message = f"{info.value.message} {info.value.hint}"
    assert VALUE not in message
    if raw.strip():
        assert raw.strip() not in message


def test_paste_cookie_state_is_exactly_the_spec_shape(settings):
    state = auth.paste_cookie_state(SecretStr(VALUE), settings.host)
    assert state.to_playwright() == {
        "cookies": [{"name": "_portal_de_empleos_session", "value": VALUE, "domain": "www.takealuk.com",
                     "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
        "origins": [],
    }
    assert VALUE not in repr(state)


def paste(settings, store, limiter, notes, *, lines=(), isatty=lambda: True, transport=None):
    feed = iter(lines)
    prompts: list[str] = []

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return next(feed)

    result = auth.paste_cookie(settings, store, limiter, notify=notes.append, isatty=isatty,
                               read_secret=read_secret, transport=transport)
    return result, prompts, feed


def test_paste_cookie_refuses_without_a_tty(settings, store, limiter):
    calls: list[httpx.Request] = []
    notes: list[str] = []
    with pytest.raises(InvalidArgument, match="run this in your own terminal"):
        paste(settings, store, limiter, notes, lines=[VALUE], isatty=lambda: False,
              transport=luk_transport(200, calls))
    assert calls == []
    assert not settings.session_path.exists()


def test_paste_cookie_refuses_stdin_from_the_null_device(settings, store, limiter, monkeypatch):
    """On Windows NUL is a character device, so `isatty()` is True for `< NUL` (Claude Code's PowerShell
    tool, Task Scheduler) while getpass reads an invisible console and hangs holding login.lock: the
    default terminal check refuses it like a pipe, before the lock and the hidden prompt."""
    calls: list[httpx.Request] = []
    prompts: list[str] = []

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return VALUE

    with open(os.devnull) as null:
        monkeypatch.setattr(sys, "stdin", null)
        with pytest.raises(InvalidArgument, match="run this in your own terminal"):
            auth.paste_cookie(settings, store, limiter, notify=lambda _: None, read_secret=read_secret,
                              transport=luk_transport(200, calls))
    assert prompts == [] and calls == []
    assert not settings.session_path.exists()


def test_paste_cookie_treats_getpass_warning_as_an_error(settings, store, limiter):
    def echoing(prompt: str) -> str:
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        return VALUE

    calls: list[httpx.Request] = []
    with pytest.raises(InvalidArgument, match="own terminal"):
        auth.paste_cookie(settings, store, limiter, notify=lambda _: None, isatty=lambda: True,
                          read_secret=echoing, transport=luk_transport(200, calls))
    assert calls == []
    assert not settings.session_path.exists()


def test_paste_cookie_probes_then_saves_only_the_session_cookie(settings, store, limiter):
    calls: list[httpx.Request] = []
    notes: list[str] = []
    result, prompts, feed = paste(settings, store, limiter, notes, lines=CURL_BASH.split("\n"),
                                  transport=luk_transport(200, calls))

    assert result == auth.LoginResult("success", None, None, "paste-cookie", [])
    assert next(feed, None) is None  # every continuation line of the cURL was consumed
    assert len(prompts) == 4
    assert [c.url.path for c in calls] == ["/saved_jobs"]
    assert calls[0].headers["cookie"] == f"_portal_de_empleos_session={VALUE}"
    assert calls[0].headers["user-agent"] == settings.user_agent
    assert saved_json(settings) == auth.paste_cookie_state(SecretStr(VALUE), settings.host).to_playwright()
    meta = store.load_meta()
    assert meta.browser == "paste-cookie" and meta.last_auth_ok_at is not None
    assert notes == [auth.PASTE_CHECK, "Logged in with the pasted cookie (name unavailable — run `luk whoami`)"]
    assert VALUE not in "\n".join(notes + prompts)


@pytest.mark.parametrize(
    ("location", "hint"),
    [(SIGN_IN, "Copy it again"), (ONBOARDING, "Luk redirected to /onboarding — finish your profile")],
)
def test_paste_cookie_rejected_is_never_saved_or_printed(settings, store, limiter, location, hint):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/onboarding":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html lang='es-CL'></html>")
        return httpx.Response(302, headers={"location": location})

    notes: list[str] = []
    with pytest.raises(AuthRequired) as info:
        paste(settings, store, limiter, notes, lines=[VALUE], transport=httpx.MockTransport(handler))
    assert "nothing was saved" in info.value.message
    assert info.value.hint.startswith(hint)
    assert VALUE not in f"{info.value.message} {info.value.hint} {notes}"
    assert notes == [auth.PASTE_CHECK]
    assert not settings.session_path.exists()


def test_the_paste_cookie_probe_never_follows_a_redirect(settings, store, limiter):
    calls: list[httpx.Request] = []
    with pytest.raises(AuthRequired) as info:
        paste(settings, store, limiter, [], lines=[VALUE], transport=luk_transport(302, calls, location=ONBOARDING))
    assert [c.url.path for c in calls] == ["/saved_jobs"]
    assert info.value.hint.startswith("Luk redirected to /onboarding — finish your profile")
    assert not settings.session_path.exists()


@pytest.mark.parametrize("status", [404, 500])
def test_paste_cookie_probe_other_status_is_not_saved(settings, store, limiter, status):
    calls: list[httpx.Request] = []
    with pytest.raises(NetworkError, match=f"HTTP {status}.*nothing was saved"):
        paste(settings, store, limiter, [], lines=[VALUE], transport=luk_transport(status, calls))
    assert len(calls) == 1
    assert not settings.session_path.exists()


def test_paste_cookie_takes_the_login_lock(settings, store, limiter):
    settings.login_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.login_lock_path)), pytest.raises(SessionBusy, match="login already in progress"):
        paste(settings, store, limiter, [], lines=[VALUE])
