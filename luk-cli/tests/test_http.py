"""http.py — hosts/redirects (§4.7), retries/blocks/limiter/verbose (§4.9), auth classification (§4.5),
cookie scope (§1.4, §4.1) and the secrets sentinel (§4.6, §8.2)."""

import json
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from luk_cli import http
from luk_cli.config import AlgoliaConfig
from luk_cli.errors import (
    AlgoliaRejected, AuthRequired, Blocked, NetworkError, RateLimited, UnsafeRequest,
)
from luk_cli.http import AlgoliaClient, LukClient
from luk_cli.models import SessionMeta

BASE = "https://www.takealuk.com"
ES_PAGE = '<html lang="es-CL"><body><header id="main-header"><nav>Hola</nav></header><main>ok</main></body></html>'
ANON_PAGE = ('<html lang="es-CL"><body><header id="main-header"><a href="/users/sign_in">Ingresar</a>'
             '</header></body></html>')
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"


def html(body=ES_PAGE, status=200, headers=None):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **(headers or {})})


def redirect(location, status=302, headers=None):
    return httpx.Response(status, headers={"location": location, **(headers or {})})


class Recorder:
    """Transport handler replaying `responses` (the last one repeats) and recording requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx.Response(nxt.status_code, headers=nxt.headers, content=nxt.content)  # fresh per request


class SpyLimiter:
    def __init__(self):
        self.calls = 0

    def acquire(self):
        self.calls += 1


def client(settings, rec, *, session=None, store=None, sleeps=None, clock=lambda: 1_790_000_000.0, **kw):
    limiter = kw.pop("limiter", SpyLimiter())
    opts = dict(transport=httpx.MockTransport(rec), sleep=(sleeps.append if sleeps is not None else lambda s: None),
                uniform=lambda a, b: 1.0, clock=clock, **kw)
    if session is None:
        return LukClient.anonymous(settings, limiter, **opts)
    return LukClient.private(settings, limiter, session, store=store, **opts)


# -- anonymous requests ----------------------------------------------------------------------------

def test_anonymous_html_request_shape(settings):
    rec = Recorder(html())
    fetched = client(settings, rec).get_html("/job_offers", [("job_positions", "analista"), ("locations", 1021)])
    req = rec.requests[0]
    assert str(req.url) == f"{BASE}/job_offers?job_positions=analista&locations=1021"
    assert req.method == "GET"
    assert req.headers["user-agent"] == settings.user_agent
    assert req.headers["accept"] == "text/html,application/xhtml+xml"
    assert req.headers["accept-language"] == "es-CL"
    assert "cookie" not in req.headers and "turbo-frame" not in req.headers
    assert (fetched.status, fetched.path, fetched.redirected, fetched.warnings) == (200, "/job_offers", False, ())
    assert fetched.url == f"{BASE}/job_offers?job_positions=analista&locations=1021" and fetched.is_html
    assert "text=" not in repr(fetched) and "Hola" not in repr(fetched)


def test_json_request_accept_header(settings):
    rec = Recorder(httpx.Response(200, json={"areas": []}))
    fetched = client(settings, rec).get_json("/flexible_search/areas", [("q", "santiago")])
    assert rec.requests[0].headers["accept"] == "application/json"
    assert fetched.is_json and fetched.json() == {"areas": []} and fetched.warnings == ()


def test_anonymous_client_never_stores_or_sends_cookies(settings, saved_session):
    saved_session()  # a session.json exists; public commands must still be anonymous
    rec = Recorder(html(headers={"set-cookie": "_portal_de_empleos_session=FROMSERVER; path=/; secure"}))
    c = client(settings, rec)
    c.get_html("/")
    c.get_html("/job_offers")
    assert all("cookie" not in r.headers for r in rec.requests)
    assert not c.is_private and c.session is None


# -- request guard (§4.7) --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("method", "url", "headers"),
    [
        ("POST", f"{BASE}/job_offers/x/save_later", {}),
        ("GET", "http://www.takealuk.com/", {}),
        ("GET", "https://evil.com/", {}),
        ("GET", "https://api.takealuk.com/", {}),
        ("GET", "https://www.takealuk.com:8443/", {}),
        ("GET", f"{BASE}/job_offers?locale=en", {}),
        ("GET", f"{BASE}/job_offers?sort_by=date", {}),
        # Rails reads these as params[:sort_by] / params[:locale] too: normalise before comparing.
        ("GET", f"{BASE}/job_offers?sort_by[]=date", {}),
        ("GET", f"{BASE}/job_offers?sort_by%5B%5D=date", {}),
        ("GET", f"{BASE}/saved_jobs?locale[]=en", {}),
        ("GET", f"{BASE}/job_offers?page=2&LOCALE=en", {}),
        ("GET", f"{BASE}/job_offers?%20Sort_By=date", {}),
        ("GET", f"{BASE}/job_offers", {"Turbo-Frame": "job_offers_results"}),
        ("GET", f"{BASE}/", {"Cookie": "a=b"}),
    ],
)
def test_luk_guard_rejects(method, url, headers):
    guard = http.luk_request_guard("www.takealuk.com", cookies_allowed=False)
    with pytest.raises(UnsafeRequest):
        guard(httpx.Request(method, url, headers=headers))


def test_luk_guard_allows_cookies_only_for_private_clients():
    request = httpx.Request("GET", f"{BASE}/saved_jobs", headers={"Cookie": "a=b"})
    http.luk_request_guard("www.takealuk.com", cookies_allowed=True)(request)


@pytest.mark.parametrize("path", ["//evil.com/x", "job_offers", "https://evil.com/"])
def test_client_rejects_non_path_targets_with_zero_requests(settings, path):
    rec = Recorder(html())
    with pytest.raises(UnsafeRequest):
        client(settings, rec).get_html(path)
    assert rec.requests == []


UNSAFE_LUK_SENDS = [
    ("POST", f"{BASE}/job_offers/x/save_later", {}),
    ("GET", "http://www.takealuk.com/", {}),
    ("GET", "https://evil.com/", {}),
    ("GET", "https://www.takealuk.com:8443/", {}),
    ("GET", f"{BASE}/job_offers?locale=en", {}),
    ("GET", f"{BASE}/job_offers", {"Turbo-Frame": "job_offers_results"}),
]


@pytest.mark.parametrize("private", [False, True], ids=["anonymous", "private"])
@pytest.mark.parametrize(("method", "url", "headers"), UNSAFE_LUK_SENDS)
def test_the_hook_is_installed_on_every_luk_client(settings, saved_session, private, method, url, headers):
    """§1.3/§4.7 "enforced by a hook": the guard is wired into each client's own httpx.Client, so even a
    request that bypasses `get_html` (and its pre-check) is refused before the transport sees it."""
    rec = Recorder(html())
    c = client(settings, rec, session=saved_session() if private else None)
    with pytest.raises(UnsafeRequest):
        c._client.send(c._client.build_request(method, url, headers=headers))
    assert rec.requests == []


def test_the_anonymous_hook_refuses_a_cookie_header(settings):
    rec = Recorder(html())
    c = client(settings, rec)
    with pytest.raises(UnsafeRequest):
        c._client.send(c._client.build_request("GET", f"{BASE}/", headers={"Cookie": "a=b"}))
    assert rec.requests == []


def test_forbidden_params_rejected_before_sending(settings):
    rec = Recorder(html())
    with pytest.raises(UnsafeRequest):
        client(settings, rec).get_html("/job_offers", [("locale", "en")])
    assert rec.requests == []


# -- redirects (§4.7) -------------------------------------------------------------------------------

def test_same_host_redirect_is_followed(settings):
    rec = Recorder(redirect("/job_offers/new-slug", 301), html())
    fetched = client(settings, rec).get_html("/job_offers/old-slug")
    assert [r.url.path for r in rec.requests] == ["/job_offers/old-slug", "/job_offers/new-slug"]
    assert fetched.redirected and fetched.path == "/job_offers/new-slug"


@pytest.mark.parametrize(
    "location",
    ["https://evil.com/job_offers/x", "http://www.takealuk.com/job_offers/x", "https://api.takealuk.com/x",
     "//evil.com/x", "https://www.takealuk.com:8443/x"],
)
def test_unsafe_redirects_are_never_followed(settings, location):
    rec = Recorder(redirect(location), html())
    with pytest.raises(UnsafeRequest):
        client(settings, rec).get_html("/job_offers/x")
    assert len(rec.requests) == 1


def test_redirect_limit(settings):
    rec = Recorder(redirect("/loop"))
    with pytest.raises(NetworkError, match="redirected more than 5"):
        client(settings, rec).get_html("/loop")
    assert len(rec.requests) == 6


def test_redirect_to_sign_in_is_auth_required_even_anonymous(settings):
    rec = Recorder(redirect(f"{BASE}/users/sign_in"))
    with pytest.raises(AuthRequired):
        client(settings, rec).get_html("/saved_jobs")
    assert len(rec.requests) == 1


# -- private requests and auth classification (§4.5) ------------------------------------------------

@pytest.fixture
def private_env(settings, store, session_value, make_state):
    meta = SessionMeta(logged_in_at=datetime(2026, 9, 23, tzinfo=timezone.utc), browser="chromium",
                       luk_cli_version="0.1.0")
    loaded = store.save(make_state(), meta)
    return settings, store, loaded, session_value


def test_private_client_sends_the_stored_cookie(private_env):
    settings, store, loaded, value = private_env
    rec = Recorder(html())
    c = client(settings, rec, session=loaded, store=store)
    fetched = c.get_html("/saved_jobs")
    assert rec.requests[0].headers["cookie"] == f"_portal_de_empleos_session={value}"
    assert fetched.status == 200 and c.is_private
    assert store.load_meta().last_auth_ok_at is not None  # non-redirected private 200


@pytest.mark.parametrize(
    "response",
    [html(status=401), redirect(f"{BASE}/users/sign_in"), redirect("/users/sign_in?x=1"), html(ANON_PAGE)],
)
def test_private_auth_required(private_env, response):
    settings, store, loaded, _ = private_env
    rec = Recorder(response)
    with pytest.raises(AuthRequired, match="luk login"):
        client(settings, rec, session=loaded, store=store).get_html("/saved_jobs")


def test_onboarding_redirect_is_auth_required_naming_the_path(private_env):
    settings, store, loaded, _ = private_env
    rec = Recorder(redirect("/onboarding"), html())
    with pytest.raises(AuthRequired, match="Luk redirected to /onboarding — finish your profile"):
        client(settings, rec, session=loaded, store=store).get_html("/saved_jobs")


@pytest.mark.parametrize(("location", "message"), [
    ("/onboarding", "Luk redirected to /onboarding — finish your profile"),
    (f"{BASE}/users/sign_in", "luk login"),
    ("https://evil.com/x", "Luk redirected to /x — finish your profile"),
])
def test_a_probe_never_follows_a_redirect(private_env, location, message):
    """`follow_redirects=False` (the §4.3/§4.2 session probe): any 3xx is AuthRequired after that single
    request; the Location is never requested (so it never gets the session cookie) and nothing is written."""
    settings, store, loaded, _ = private_env
    rec = Recorder(redirect(location, headers={"set-cookie": "_portal_de_empleos_session=" + "R" * 60 + "; path=/"}),
                   html())
    with pytest.raises(AuthRequired, match=message):
        client(settings, rec, session=loaded, store=store).get_html("/saved_jobs", follow_redirects=False)
    assert [r.url.path for r in rec.requests] == ["/saved_jobs"]
    assert store.load() == loaded and store.load_meta().last_auth_ok_at is None


def test_a_probe_answered_200_is_not_redirected(private_env):
    settings, store, loaded, _ = private_env
    rec = Recorder(html())
    fetched = client(settings, rec, session=loaded, store=store).get_html("/saved_jobs", follow_redirects=False)
    assert (fetched.status, fetched.redirected, len(rec.requests)) == (200, False, 1)


def test_rotated_cookie_is_written_back(private_env):
    settings, store, loaded, _ = private_env
    rotated = "N" * 60
    rec = Recorder(html(headers={"set-cookie": f"_portal_de_empleos_session={rotated}; path=/; secure; HttpOnly"}),
                   html())
    c = client(settings, rec, session=loaded, store=store)
    c.get_html("/saved_jobs")
    assert store.load().state.cookies[0].value.get_secret_value() == rotated
    assert c.session.sha256 == store.load().sha256  # the client tracks its own write (CAS token)
    c.get_html("/profile/cvs")
    assert rec.requests[1].headers["cookie"] == f"_portal_de_empleos_session={rotated}"


def test_no_write_back_when_auth_required(private_env):
    settings, store, loaded, _ = private_env
    rec = Recorder(redirect("/users/sign_in", headers={"set-cookie": "_portal_de_empleos_session=ANON; path=/"}))
    with pytest.raises(AuthRequired):
        client(settings, rec, session=loaded, store=store).get_html("/saved_jobs")
    assert store.load().sha256 == loaded.sha256


def test_write_back_discarded_after_logout(private_env):
    settings, store, loaded, _ = private_env
    store.delete()  # logout happened while the client was alive
    rec = Recorder(html(headers={"set-cookie": "_portal_de_empleos_session=" + "Z" * 40 + "; path=/; secure"}), html())
    c = client(settings, rec, session=loaded, store=store)
    c.get_html("/saved_jobs")
    assert not store.exists()
    c.get_html("/saved_jobs")
    assert "cookie" not in rec.requests[1].headers  # in-memory cookies discarded


def test_probe_client_without_store_never_writes(settings, make_state, store):
    from luk_cli.session import LoadedSession
    from luk_cli.models import StorageState

    loaded = LoadedSession.from_state(StorageState.model_validate(make_state()))
    rec = Recorder(html(headers={"set-cookie": "_portal_de_empleos_session=" + "Q" * 40 + "; path=/; secure"}))
    assert client(settings, rec, session=loaded, store=None).get_html("/saved_jobs").status == 200
    assert not store.exists()


# -- blocks, retries, limiter (§4.9) ----------------------------------------------------------------

@pytest.mark.parametrize(
    "response",
    [html(status=403), html(headers={"cf-mitigated": "challenge"}), html(CHALLENGE, status=503),
     html(CHALLENGE, status=429), html("<title>Captcha</title>", status=403)],
)
def test_blocks_stop_after_exactly_one_request(settings, response):
    rec = Recorder(response)
    with pytest.raises(Blocked, match="not retrying"):
        client(settings, rec).get_html("/job_offers")
    assert len(rec.requests) == 1


def test_retry_with_backoff_then_success(settings):
    sleeps = []
    rec = Recorder(html(status=503), html(status=502), html())
    limiter = SpyLimiter()
    fetched = client(settings, rec, sleeps=sleeps, limiter=limiter).get_html("/job_offers")
    assert fetched.status == 200 and len(rec.requests) == 3
    assert sleeps == [1.5, 3.0] and limiter.calls == 3  # every attempt passes the limiter


def test_backoff_formula():
    assert [http.backoff_delay(n, lambda a, b: 1.0) for n in range(6)] == [1.5, 3.0, 6.0, 12.0, 24.0, 30.0]
    assert http.backoff_delay(0, lambda a, b: b) == pytest.approx(1.875)
    assert http.backoff_delay(0, lambda a, b: a) == pytest.approx(1.125)


@pytest.mark.parametrize(("status", "error"), [(502, NetworkError), (504, NetworkError), (429, RateLimited)])
def test_retries_exhausted_after_four_attempts(settings, status, error):
    sleeps = []
    rec = Recorder(html(status=status))
    with pytest.raises(error):
        client(settings, rec, sleeps=sleeps).get_html("/job_offers")
    assert len(rec.requests) == 4 and sleeps == [1.5, 3.0, 6.0]


def test_retry_after_seconds_is_honoured(settings):
    sleeps = []
    rec = Recorder(html(status=429, headers={"retry-after": "7"}), html())
    client(settings, rec, sleeps=sleeps).get_html("/job_offers")
    assert sleeps == [7.0]


def test_retry_after_http_date_is_honoured(settings):
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    later = format_datetime(now + timedelta(seconds=20), usegmt=True)
    sleeps = []
    rec = Recorder(html(status=503, headers={"retry-after": later}), html())
    client(settings, rec, sleeps=sleeps, clock=now.timestamp).get_html("/job_offers")
    assert sleeps == [pytest.approx(20.0)]


def test_retry_after_above_sixty_seconds_is_rate_limited_at_once(settings):
    rec = Recorder(html(status=429, headers={"retry-after": "120"}))
    with pytest.raises(RateLimited):
        client(settings, rec).get_html("/job_offers")
    assert len(rec.requests) == 1


def test_parse_retry_after():
    assert http.parse_retry_after(None, 0) is None
    assert http.parse_retry_after("garbage", 0) is None
    assert http.parse_retry_after("-5", 0) == 0.0
    noon = datetime(2026, 9, 23, 12, tzinfo=timezone.utc).timestamp()
    assert http.parse_retry_after("Wed, 23 Sep 2026 12:00:30 GMT", noon) == 30.0


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.ReadError("reset"),
    # the peer closed the connection before or during the response (a stale keep-alive socket, a cut body)
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
], ids=["connect", "read-timeout", "read-reset", "disconnected", "truncated-body"])
def test_connect_and_read_errors_are_retried(settings, exc):
    sleeps = []
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) < 3:
            raise exc
        return html()

    assert client(settings, flaky, sleeps=sleeps).get_html("/").status == 200
    assert sleeps == [1.5, 3.0]


def test_connect_errors_exhausted_is_network(settings):
    def down(request):
        raise httpx.ConnectError("down")

    with pytest.raises(NetworkError):
        client(settings, down).get_html("/")


def test_real_limiter_spaces_attempts(settings, limiter, fake_clock):
    rec = Recorder(html(status=503), html())
    c = LukClient.anonymous(settings, limiter, transport=httpx.MockTransport(rec), sleep=fake_clock.sleep,
                            uniform=lambda a, b: 1.0)
    c.get_html("/job_offers")
    assert fake_clock.sleeps == [1.5]  # the backoff already covered the 1.5 s spacing
    assert len(json.loads(settings.ratelimit_path.read_text("utf-8"))["recent"]) == 2


def test_non_retryable_statuses_are_returned(settings):
    rec = Recorder(html(status=404))
    assert client(settings, rec).get_html("/job_offers/missing").status == 404
    assert len(rec.requests) == 1


# -- locale guard and verbose hook ------------------------------------------------------------------

def test_locale_guard_warns_but_does_not_fail(settings):
    rec = Recorder(html('<html lang="en"><body>hi</body></html>'))
    fetched = client(settings, rec).get_html("/")
    assert fetched.status == 200 and len(fetched.warnings) == 1 and "lang" in fetched.warnings[0]


def test_verbose_prints_one_line_per_hop(settings, capsys):
    rec = Recorder(redirect("/job_offers/b", 301), html(status=503), html())
    client(settings, rec, verbose=True).get_html("/job_offers/a", [("x", "1")])
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith(f"GET {BASE}/job_offers/a?x=1 -> 301 (") and lines[0].endswith(" ms) attempt=1")
    assert lines[1].startswith(f"GET {BASE}/job_offers/b -> 503 (") and lines[1].endswith("attempt=1")
    assert lines[2].startswith(f"GET {BASE}/job_offers/b -> 200 (") and lines[2].endswith("attempt=2")


def test_not_verbose_prints_nothing(settings, capsys):
    client(settings, Recorder(html())).get_html("/")
    assert capsys.readouterr() == ("", "")


def test_markers():
    assert http.looks_like_challenge(CHALLENGE) and not http.looks_like_challenge(ES_PAGE)
    assert http.has_anonymous_marker(ANON_PAGE) and not http.has_anonymous_marker(ES_PAGE)


# -- Algolia (§4.7, §4.9) -------------------------------------------------------------------------

CFG = AlgoliaConfig("TESTAPPID0", "fake-search-key", "JobOffer_query_suggestions")
HITS = {"results": [{"hits": [{"query": "analista financiero", "popularity": 12}]}]}


def algolia(settings, rec, resolve=lambda refresh: CFG, **kw):
    return AlgoliaClient(settings, resolve, transport=httpx.MockTransport(rec), limiter=SpyLimiter(),
                         sleep=lambda s: None, uniform=lambda a, b: 1.0, **kw)


def test_algolia_request_shape_and_no_cookies_origin_referer(settings):
    rec = Recorder(httpx.Response(200, json=HITS, headers={"set-cookie": "tracker=1; path=/"}))
    c = algolia(settings, rec)
    fetched = c.query_suggestions("analista fin", 5)
    c.query_suggestions("analista fin", 5)
    req = rec.requests[0]
    assert (req.method, str(req.url)) == ("POST", "https://testappid0-dsn.algolia.net/1/indexes/*/queries")
    assert req.headers["x-algolia-application-id"] == "TESTAPPID0"
    assert req.headers["x-algolia-api-key"] == CFG.api_key
    assert req.headers["user-agent"] == settings.user_agent
    assert httpx.Response(200, content=req.content).json() == {"requests": [{
        "indexName": "JobOffer_query_suggestions", "query": "analista fin", "hitsPerPage": 5,
        "attributesToRetrieve": ["query", "popularity"]}]}
    for r in rec.requests:
        assert not {"cookie", "origin", "referer"} & set(r.headers.keys())
    assert fetched.json() == HITS


@pytest.mark.parametrize(
    ("method", "url", "headers"),
    [
        ("GET", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {}),
        ("POST", "http://testappid0-dsn.algolia.net/1/indexes/*/queries", {}),
        ("POST", "https://evil.com/1/indexes/*/queries", {}),
        ("POST", "https://testappid0-dsn.algolia.net.evil.com/1/indexes/*/queries", {}),
        ("POST", "https://testappid0-dsn.algolia.net/1/indexes/JobOffer/query", {}),
        ("POST", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {"Origin": "https://www.takealuk.com"}),
        ("POST", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {"Referer": "https://www.takealuk.com/"}),
        ("POST", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {"Cookie": "a=b"}),
    ],
)
def test_algolia_guard_rejects(method, url, headers):
    with pytest.raises(UnsafeRequest):
        http.algolia_request_guard(httpx.Request(method, url, headers=headers))


@pytest.mark.parametrize(
    ("method", "url", "headers"),
    [
        ("GET", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {}),
        ("POST", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {"Origin": "https://www.takealuk.com"}),
        ("POST", "https://testappid0-dsn.algolia.net/1/indexes/*/queries", {"Referer": "https://www.takealuk.com/"}),
        ("POST", "https://evil.com/1/indexes/*/queries", {}),
        ("POST", "https://www.takealuk.com/1/indexes/*/queries", {}),
    ],
)
def test_the_hook_is_installed_on_the_algolia_client(settings, method, url, headers):
    rec = Recorder(httpx.Response(200, json=HITS))
    c = algolia(settings, rec)
    with pytest.raises(UnsafeRequest):
        c._client.send(c._client.build_request(method, url, headers=headers, json={}))
    assert rec.requests == []


def test_bad_app_id_is_rejected_before_any_request():
    with pytest.raises(Exception, match="Algolia application id"):
        AlgoliaConfig("evil.com#", "k", "i")


def test_algolia_401_triggers_exactly_one_rediscovery(settings):
    refreshed = []

    def resolve(refresh):
        refreshed.append(refresh)
        return CFG

    rec = Recorder(httpx.Response(401, json={"message": "Invalid API key"}), httpx.Response(200, json=HITS))
    assert algolia(settings, rec, resolve).query_suggestions("x", 1).json() == HITS
    assert refreshed == [False, True] and len(rec.requests) == 2


def test_algolia_refused_after_rediscovery_is_blocked(settings):
    refreshed = []

    def resolve(refresh):
        refreshed.append(refresh)
        return CFG

    rec = Recorder(httpx.Response(403, json={}))
    with pytest.raises(AlgoliaRejected) as info:
        algolia(settings, rec, resolve).query_suggestions("x", 1)
    assert info.value.exit_code == 4 and refreshed == [False, True] and len(rec.requests) == 2


def test_algolia_uses_its_interval_limiter(settings):
    spy = SpyLimiter()
    c = AlgoliaClient(settings, lambda r: CFG, transport=httpx.MockTransport(Recorder(httpx.Response(200, json=HITS))),
                      limiter=spy)
    c.query_suggestions("x", 1)
    assert spy.calls == 1


# -- secrets sentinel (§4.6, §8.2) ------------------------------------------------------------------

def test_secrets_sentinel_never_reaches_output(settings, store, make_state, capsys, caplog):
    sentinel_a = "SENTINEL_A_" + "x" * 30  # sent by the server in Set-Cookie
    sentinel_b = "SENTINEL_B_" + "y" * 30  # held in the jar from session.json
    loaded = store.save(make_state(sentinel_b))
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    root.addHandler(handler)
    caplog.set_level(logging.DEBUG)
    try:
        rec = Recorder(
            html(headers={"set-cookie": f"_portal_de_empleos_session={sentinel_a}; path=/; secure; HttpOnly"}),
        )
        c = client(settings, rec, session=loaded, store=store, verbose=True)
        c.get_html("/saved_jobs")
        header = f"Cookie: x; _portal_de_empleos_session={sentinel_b}"
        logging.getLogger("luk_cli.test").debug("jar=%s header=%s", sentinel_a, header)
        logging.getLogger("httpcore.http11").debug("raw Set-Cookie %s", sentinel_a)

        def boom(request):
            raise httpx.ConnectError("connection refused")

        failing = client(settings, boom, session=store.load(), store=store, verbose=True)
        with pytest.raises(NetworkError) as info:
            failing.get_html("/saved_jobs")
        print(str(info.value), repr(info.value), file=sys.stderr)
        print("".join(traceback.format_exception(info.type, info.value, info.tb)), file=sys.stderr)
        print(repr(c), repr(failing), repr(c.session), repr(store), repr(c.session.state), file=sys.stderr)
        logging.getLogger("luk_cli").exception("wrapped", exc_info=info.value)
    finally:
        root.removeHandler(handler)
    out, err = capsys.readouterr()
    for text in (out, err, caplog.text):
        assert sentinel_a not in text and sentinel_b not in text
    assert "GET https://www.takealuk.com/saved_jobs -> 200" in err  # verbose ran
