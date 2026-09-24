"""Fetcher: throttle, backoff then stop, guard rails, honest identity (fake session, no sleep)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from conftest import (BASE, LUK_LIKE_ROBOTS, TEST_UA, FakeClock, FakeSession, make_response,
                      real_session)
from luk_scraper import config
from luk_scraper.fetcher import (Blocked, FetchError, Fetcher, build_list_params, check_url)
from luk_scraper.robots import RobotsPolicy

LIST = BASE + "/job_offers"
DETAIL = BASE + "/job_offers/cargo-demo-1"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
CREDENTIAL_HEADERS = ("Cookie", "Authorization", "Proxy-Authorization")


def _fetcher(responses, **kw):
    clock = FakeClock()
    session = FakeSession(responses)
    f = Fetcher(session=session, sleep=clock.sleep, clock=clock,
                user_agent="luk-scraper/0.0.0 (+https://example.com/contact)", **kw)
    return f, session, clock


def _real_fetcher(responses, session_setup=None):
    """A Fetcher on a real requests.Session (fake transport): exercises requests' own merging of
    session headers, cookie jar, auth and netrc."""
    session, adapter = real_session(responses)
    if session_setup:
        session_setup(session)
    clock = FakeClock()
    f = Fetcher(session=session, sleep=clock.sleep, clock=clock, user_agent=TEST_UA)
    return f, session, adapter


# --- backoff then stop --------------------------------------------------------------------

def test_429_then_200_succeeds_honouring_retry_after():
    f, session, clock = _fetcher([make_response(429, headers={"Retry-After": "7"}),
                                  make_response(200, "<html>ok</html>")])
    assert f.get_html(LIST) == "<html>ok</html>"
    assert len(session.calls) == 2
    assert 7 in clock.sleeps                      # waited what Luk asked for
    assert f.requests_made == 2


def test_503_then_200_uses_exponential_backoff():
    f, session, clock = _fetcher([make_response(503), make_response(503), make_response(200, "x")])
    assert f.get_html(LIST) == "x"
    backoffs = [s for s in clock.sleeps if s >= config.BACKOFF_BASE_S]
    assert backoffs[:2] == [config.BACKOFF_BASE_S, config.BACKOFF_BASE_S * 2]


def test_persistent_403_raises_blocked_after_retries():
    responses = [make_response(403) for _ in range(config.BACKOFF_MAX_RETRIES + 1)]
    f, session, clock = _fetcher(responses)
    with pytest.raises(Blocked) as exc:
        f.get_html(LIST)
    assert exc.value.status == 403
    assert len(session.calls) == config.BACKOFF_MAX_RETRIES + 1   # tried, then stopped
    # no pointless sleep after the final attempt
    assert len([s for s in clock.sleeps if s >= config.BACKOFF_BASE_S]) == config.BACKOFF_MAX_RETRIES


def test_persistent_429_is_blocked_and_persistent_500_is_fetch_error():
    f, _, _ = _fetcher([make_response(429)] * 5)
    with pytest.raises(Blocked):
        f.get_html(LIST)
    f, _, _ = _fetcher([make_response(500)] * 5)
    with pytest.raises(FetchError) as exc:
        f.get_html(LIST)
    assert not isinstance(exc.value, Blocked) and exc.value.status == 500


def test_huge_retry_after_stops_instead_of_waiting():
    f, session, clock = _fetcher([make_response(429, headers={"Retry-After": "86400"})])
    with pytest.raises(Blocked):
        f.get_html(LIST)
    assert len(session.calls) == 1
    assert all(s < 86400 for s in clock.sleeps)


def test_retry_after_as_http_date():
    now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    when = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    f, _, clock = _fetcher([make_response(503, headers={"Retry-After": when}),
                            make_response(200, "ok")], now=lambda: now)
    assert f.get_html(LIST) == "ok"
    assert 30 in clock.sleeps


def test_network_errors_are_retried_then_raised():
    f, session, _ = _fetcher([requests.ConnectionError("boom"), make_response(200, "ok")])
    assert f.get_html(LIST) == "ok"
    f, session, _ = _fetcher([requests.Timeout("slow")] * 5)
    with pytest.raises(FetchError):
        f.get_html(LIST)
    assert len(session.calls) == 5


def test_404_is_not_retried():
    f, session, _ = _fetcher([make_response(404)])
    with pytest.raises(FetchError) as exc:
        f.get_html(DETAIL)
    assert exc.value.status == 404 and len(session.calls) == 1


# --- throttle -----------------------------------------------------------------------------

def test_throttle_waits_between_requests():
    f, _, clock = _fetcher([make_response(200, "a"), make_response(200, "b")], delay_s=2.0)
    f.get_html(LIST)
    f.get_html(DETAIL)
    assert clock.sleeps == [2.0]


@pytest.mark.parametrize("asked,used", [(0.0, 1.0), (0.2, 1.0), (1.0, 1.0), (3.5, 3.5),
                                        (None, 2.0), (float("nan"), 2.0), (-5, 1.0)])
def test_delay_has_a_hard_floor(asked, used):
    f, _, _ = _fetcher([], delay_s=asked)
    assert f.delay_s == used


def test_set_min_delay_only_raises():
    f, _, _ = _fetcher([], delay_s=2.0)
    f.set_min_delay(5)
    assert f.delay_s == 5
    f.set_min_delay(0.1)
    assert f.delay_s == 5


# --- guard rails --------------------------------------------------------------------------

def test_list_params_repeat_countries_and_skip_page_one():
    assert build_list_params() == [("countries[]", "Chile"), ("worldwide", "1")]
    assert build_list_params(3, ["Chile", "Perú"], "last_3_days") == [
        ("countries[]", "Chile"), ("countries[]", "Perú"), ("worldwide", "1"),
        ("date", "last_3_days"), ("page", "3")]


@pytest.mark.parametrize("countries", [None, ["Chile"], ["Colombia"], ["Brasil", "México"]])
def test_list_params_always_send_worldwide_with_countries(countries):
    # Without worldwide=1 Luk ANDs countries[] with the visitor's geo-IP country: from a Chilean
    # IP, countries[]=Colombia returned 0 offers, and 1430 with worldwide=1 (live, 2026-09-24).
    params = build_list_params(1, countries)
    assert ("worldwide", "1") in params
    assert [v for k, v in params if k == "countries[]"] == (countries or ["Chile"])


def test_list_params_normalize_country_names():
    assert build_list_params(1, ["peru", " MEXICO ", "brasil"])[:3] == [
        ("countries[]", "Perú"), ("countries[]", "México"), ("countries[]", "Brasil")]


@pytest.mark.parametrize("countries", [["Narnia"], ["Chile", "Argentina"], [], [" "]])
def test_list_params_refuse_unknown_or_no_country(countries):
    with pytest.raises(ValueError):
        build_list_params(1, countries)


def test_list_page_sends_expected_query():
    f, session, _ = _fetcher([make_response(200, "x")])
    f.list_page(page=2, countries=["Chile", "Perú"])
    call = session.calls[0]
    assert call["url"] == LIST
    assert call["params"] == [("countries[]", "Chile"), ("countries[]", "Perú"),
                              ("worldwide", "1"), ("page", "2")]
    assert call["allow_redirects"] is False and call["timeout"] == config.TIMEOUT_S


@pytest.mark.parametrize("url,params", [
    (LIST + "?sort_by=newest", None),
    (LIST + "?locale=en", None),
    (LIST, [("sort_by", "newest")]),
    (LIST, [("LOCALE", "en")]),
])
def test_forbidden_params_are_refused(url, params):
    with pytest.raises(ValueError, match="robots"):
        check_url(url, params)
    f, session, _ = _fetcher([])
    with pytest.raises(ValueError):
        f.get_html(url, params)
    assert session.calls == []


@pytest.mark.parametrize("url", ["http://www.takealuk.com/job_offers",
                                 "https://example.com/job_offers",
                                 "https://www.takealuk.com.example.com/job_offers"])
def test_only_luk_over_https(url):
    f, session, _ = _fetcher([])
    with pytest.raises(ValueError):
        f.get_html(url)
    assert session.calls == []


def test_redirect_within_job_offers_is_followed():
    f, session, _ = _fetcher([make_response(301, headers={"Location": "/job_offers/cargo-demo-2"}),
                              make_response(200, "moved")])
    assert f.get_html(DETAIL) == "moved"
    assert session.calls[1]["url"] == BASE + "/job_offers/cargo-demo-2"


@pytest.mark.parametrize("location", ["/users/sign_in", "https://example.com/job_offers/x",
                                      "/profile"])
def test_redirect_elsewhere_is_an_error_not_a_request(location):
    f, session, _ = _fetcher([make_response(302, headers={"Location": location})])
    with pytest.raises(FetchError, match="redirect"):
        f.get_html(DETAIL)
    assert len(session.calls) == 1


@pytest.mark.parametrize("location", [
    "/job_offersX",                                   # merely shares the prefix
    "/job_offers_admin",
    "/job_offers/cargo-demo-1/recommendations",       # robots-disallowed Turbo fragment
    "/job_offers/cargo-demo-2/apply",
])
def test_redirect_must_land_on_the_listing_or_an_offer_page(location):
    f, session, _ = _fetcher([make_response(302, headers={"Location": location})])
    with pytest.raises(FetchError, match="redirect"):
        f.get_html(DETAIL)
    assert len(session.calls) == 1


def test_redirect_to_the_listing_is_followed():
    f, session, _ = _fetcher([make_response(301, headers={"Location": "/job_offers?page=2"}),
                              make_response(200, "listing")])
    assert f.get_html(DETAIL) == "listing"
    assert session.calls[1]["url"] == LIST + "?page=2"


def test_every_redirect_hop_is_checked_against_robots():
    f, session, _ = _fetcher([make_response(301, headers={"Location": "/job_offers/cargo-demo-2"})])
    f.set_robots(RobotsPolicy.from_text(
        "User-agent: *\nAllow: /\nDisallow: /job_offers/cargo-demo-2\n"))
    with pytest.raises(FetchError, match="robots"):
        f.get_html(DETAIL)
    assert len(session.calls) == 1                    # the disallowed hop is never requested


def test_second_redirect_hop_is_checked_against_robots_too():
    f, session, _ = _fetcher([
        make_response(301, headers={"Location": "/job_offers/cargo-demo-2"}),
        make_response(301, headers={"Location": "/job_offers/cargo-demo-3"},
                      url=BASE + "/job_offers/cargo-demo-2")])
    f.set_robots(RobotsPolicy.from_text(
        "User-agent: *\nAllow: /\nDisallow: /job_offers/cargo-demo-3\n"))
    with pytest.raises(FetchError, match="robots"):
        f.get_html(DETAIL)
    assert [c["url"] for c in session.calls] == [DETAIL, BASE + "/job_offers/cargo-demo-2"]


def test_redirect_allowed_by_robots_is_followed():
    f, session, _ = _fetcher([make_response(301, headers={"Location": "/job_offers/cargo-demo-2"}),
                              make_response(200, "moved")])
    f.set_robots(RobotsPolicy.from_text(LUK_LIKE_ROBOTS))
    assert f.get_html(DETAIL) == "moved"
    assert len(session.calls) == 2


def test_robots_policy_also_gates_the_first_request():
    f, session, _ = _fetcher([])
    f.set_robots(RobotsPolicy.from_text("User-agent: *\nDisallow: /job_offers/\n"))
    with pytest.raises(FetchError, match="robots"):
        f.get_html(DETAIL)
    assert session.calls == []


def test_robots_is_checked_on_the_full_url_with_its_query():
    # The same URL the crawl checks (path + query), not just the path.
    f, session, _ = _fetcher([])
    f.set_robots(RobotsPolicy.from_text(
        "User-agent: *\nAllow: /\nDisallow: /*?countries[]=Chile\n"))
    with pytest.raises(FetchError, match="robots"):
        f.list_page(page=1)
    assert session.calls == []


@pytest.mark.parametrize("policy", [None, "User-agent: *\nAllow: /\n", True])
def test_set_robots_only_takes_a_parsed_policy(policy):
    f, _, _ = _fetcher([])
    with pytest.raises(TypeError):
        f.set_robots(policy)


def test_missing_charset_decodes_as_utf8():
    r = make_response(200, "Región Metropolitana", headers={"Content-Type": "text/html"})
    f, _, _ = _fetcher([r])
    assert f.get_html(LIST) == "Región Metropolitana"


def test_get_text_status_does_not_raise_on_404():
    f, _, _ = _fetcher([make_response(404, "nope")])
    assert f.get_text_status(BASE + "/robots.txt") == (404, "")


# --- identity -----------------------------------------------------------------------------

def test_session_headers_are_honest_and_cookies_refused():
    f, session, _ = _fetcher([])
    assert session.headers["User-Agent"].startswith("luk-scraper/")
    assert "Mozilla" not in session.headers["User-Agent"]
    policy = session.cookies._policy                          # no cookie is stored or sent
    assert policy.is_not_allowed("www.takealuk.com") and policy.is_not_allowed(".takealuk.com")


def test_default_user_agent(monkeypatch):
    monkeypatch.delenv(config.CONTACT_ENV, raising=False)
    f = Fetcher(session=FakeSession([]))
    assert f.user_agent == config.user_agent()
    assert f.session.headers["User-Agent"] == config.user_agent()


@pytest.mark.parametrize("ua", [
    BROWSER_UA,
    "Mozilla/5.0 luk-scraper/0.1.0 (+https://example.com/contact)",
    "luk-scraper/0.1.0 (+https://example.com/contact) Chrome/126.0",
    "luk-scraper/0.1.0 (+mailto:paz.prueba@example.com)",
    "luk-scraper/0.1.0 (+https://paz.prueba@example.com/)",
    "luk-scraper/0.1.0 (paz.prueba@example.com)",
    "luk-scraper/0.1.0 (+https://example.com/?who=paz.prueba@example.com)",
    "luk-scraper/0.1.0 (+ftp://example.com/)",
    "luk-scraper/0.1.0 (+https://example.com/\r\nCookie: x=1)",
    "luk-scraper/0.1.0 (+https://example.com/\x00)",
    "luk-scraper/0.1.0 (+https://example.com/ contact)",
    "luk-scraper/0.1.0",
    "luk-scraper (+https://example.com/contact)",
    "LUK-SCRAPER/0.1.0 (+https://example.com/contact)",
    "other-bot/0.1.0 (+https://example.com/contact)",
    "python-requests/2.32.3",
    "",
])
def test_user_agent_must_have_the_honest_format(ua):
    session = FakeSession([])
    with pytest.raises(ValueError, match="User-Agent"):
        Fetcher(session=session, user_agent=ua)
    assert "User-Agent" not in session.headers and session.calls == []


@pytest.mark.parametrize("ua", ["luk-scraper/0.0.0 (+https://example.com/contact)",
                                "luk-scraper/0.1.0.dev1 (+http://example.com)"])
def test_honest_user_agents_are_accepted(ua):
    assert Fetcher(session=FakeSession([]), user_agent=ua).user_agent == ua


def test_user_agent_cannot_be_swapped_after_construction():
    f, session, _ = _fetcher([make_response(200, "ok")])
    with pytest.raises(AttributeError):
        f.user_agent = BROWSER_UA
    session.headers["User-Agent"] = BROWSER_UA            # tampering with the session directly
    f.get_html(LIST)
    assert session.calls[0]["headers"]["User-Agent"] == TEST_UA


def _load_credentials(session):
    session.cookies.set("_portal_de_empleos_session", "FAKE-SESSION-VALUE",
                        domain="www.takealuk.com", path="/")
    session.cookies.set("remember_user_token", "FAKE-REMEMBER-VALUE", domain=".takealuk.com",
                        path="/")
    session.headers.update({"Cookie": "_portal_de_empleos_session=FAKE-HEADER-VALUE",
                            "Authorization": "Bearer FAKE-TOKEN",
                            "proxy-authorization": "Basic FAKE-PROXY"})
    session.auth = ("paz.prueba@example.com", "FAKE-SECRET")


def test_injected_session_cookies_and_credentials_never_reach_a_request():
    f, session, adapter = _real_fetcher([make_response(200, "ok")], _load_credentials)
    assert f.get_html(LIST) == "ok"
    sent = adapter.requests[0].headers
    assert sent["User-Agent"] == TEST_UA
    for name in CREDENTIAL_HEADERS:
        assert name not in sent, name
    assert "FAKE" not in repr(dict(sent))
    assert len(session.cookies) == 0


def test_cookies_and_credentials_added_later_are_not_sent_either():
    f, session, adapter = _real_fetcher([make_response(200, "a"), make_response(200, "b")])
    f.get_html(LIST)
    _load_credentials(session)                       # after construction, before the next GET
    f.get_html(DETAIL)
    sent = adapter.requests[1].headers
    for name in CREDENTIAL_HEADERS:
        assert name not in sent, name
    assert "FAKE" not in repr(dict(sent))


def test_netrc_credentials_are_never_sent(monkeypatch):
    # requests adds ~/.netrc credentials for the host unless the session has its own auth.
    monkeypatch.setattr(requests.sessions, "get_netrc_auth",
                        lambda url, raise_errors=False: ("paz.prueba@example.com", "FAKE"))
    f, _, adapter = _real_fetcher([make_response(200, "ok")])
    f.get_html(LIST)
    assert "Authorization" not in adapter.requests[0].headers


def test_real_session_sends_only_the_honest_headers():
    f, _, adapter = _real_fetcher([make_response(200, "ok")])
    f.get_html(LIST)
    sent = adapter.requests[0]
    assert sent.method == "GET"
    assert sent.headers["User-Agent"] == TEST_UA
    assert not {h.lower() for h in sent.headers} & {h.lower() for h in CREDENTIAL_HEADERS}
