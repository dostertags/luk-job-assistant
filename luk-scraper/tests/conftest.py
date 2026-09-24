"""Shared offline test helpers: fixtures, a fake fetcher, a fake HTTP session, a fake clock.

Nothing here touches the network. All content is fake (Empresa Demo NN SpA, example.com).
"""
from __future__ import annotations

import pathlib
from typing import Optional, Union

import pytest
import requests

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BASE = "https://www.takealuk.com"
TEST_UA = "luk-scraper/0.0.0 (+https://example.com/contact)"

# The `User-agent: *` group of Luk's robots.txt, verbatim as fetched live on 2026-09-24
# (Allow: / first, then Disallows, some with wildcards).
LUK_LIKE_ROBOTS = """\
User-agent: *
Allow: /
Disallow: /profile
Disallow: /saved_jobs
Disallow: /companies/profile
Disallow: /companies/profile/edit
Disallow: /companies/job_offers/
Disallow: /admin/
Disallow: /flipper
Disallow: /users/sign_in
Disallow: /users/sign_up
Disallow: /users/password
Disallow: /users/confirmation
Disallow: /users/unlock
Disallow: /companies/sign_in
Disallow: /companies/registration
Disallow: /onboarding
Disallow: /job_offers/*/recommendations
Disallow: /wp-*
Disallow: /wordpress*
Disallow: /backup*
Disallow: /blog
Disallow: /*?sort_by=
Disallow: /*&sort_by=
Disallow: /*?locale=
Disallow: /*&locale=

Sitemap: https://www.takealuk.com/sitemap.xml
"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Offline guarantee: a test that reaches requests' real transport fails at once (an
    AssertionError, not a network error, so the Fetcher does not retry or back off)."""
    def refuse(self, request, *args, **kwargs):
        raise AssertionError(f"test tried to reach the network: {request.method} {request.url}")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def listing_html() -> str:
    return read_fixture("listing.html")


@pytest.fixture
def detail_html() -> str:
    return read_fixture("detail.html")


# --- synthetic listing pages --------------------------------------------------------------

def card(slug: str, title: str = "Cargo Demo", company: str = "Empresa Demo 01 SpA",
         location: str = "Providencia, Santiago, Región Metropolitana, Chile",
         tags: tuple[str, ...] = ("Jornada Completa", "Presencial"),
         posted: Optional[str] = "Hace 1 día") -> str:
    tag_html = "".join(f'<span class="tag-navy">{t}</span>' for t in tags)
    posted_html = f"<span>{posted}</span>" if posted else ""
    return (
        f'<div class="card job-offer-card" data-scroll-restore-slug="{slug}">'
        f'<a href="/job_offers/{slug}">'
        f'<h2 class="card-title job-offer-card__title">{title}</h2>'
        f'<p class="item-title">{company}</p>'
        f'<span class="break-words min-w-0">{location}</span>'
        f"{tag_html}{posted_html}</a></div>"
    )


def listing_page(slugs, *, total: Optional[int] = None, last_page: Optional[int] = None) -> str:
    cards = "".join(card(s) for s in slugs)
    count = (f'<div class="job-offers-results-count"><b>{total}</b> resultados</div>'
             if total is not None else "")
    nav = (f'<nav class="pagination"><a href="?page=2">2</a>'
           f'<a href="?page={last_page}">{last_page}</a></nav>' if last_page else "")
    return f"<html><body>{count}<div class=\"job-offers-list\">{cards}</div>{nav}</body></html>"


EMPTY_PAGE = "<html><body><div class='job-offers-list'></div></body></html>"


def detail_page(title: str = "Cargo Demo", value: str = "1") -> str:
    return ('<html><head><script type="application/ld+json">'
            '{"@type":"JobPosting","title":"' + title + '","description":"<p>Texto de '
            'ejemplo.</p>","datePosted":"2026-09-20","validThrough":"2026-10-20",'
            '"identifier":{"@type":"PropertyValue","value":"' + value + '"}}'
            "</script></head><body></body></html>")


# --- fakes --------------------------------------------------------------------------------

Result = Union[str, BaseException]


class FakeFetcher:
    """Stands in for luk_scraper.fetcher.Fetcher at the crawler/CLI level."""

    def __init__(self, pages: Optional[dict[int, Result]] = None,
                 details: Optional[dict[str, Result]] = None, *,
                 robots_txt: str = LUK_LIKE_ROBOTS, robots_status: int = 200,
                 robots_error: Optional[Exception] = None, default_detail: Result = ""):
        self.pages = pages or {}
        self.details = details or {}
        self.default_detail = default_detail or detail_page()
        self.robots_txt = robots_txt
        self.robots_status = robots_status
        self.robots_error = robots_error
        self.user_agent = TEST_UA
        self.delay_s = 2.0
        self.robots_policy = None
        self.calls: list[tuple] = []
        self.requests_made = 0

    def _answer(self, value: Result) -> str:
        self.requests_made += 1
        if isinstance(value, BaseException):
            raise value
        return value

    def get_text_status(self, url: str) -> tuple[int, str]:
        self.calls.append(("robots", url))
        if self.robots_error is not None:
            self.requests_made += 1
            raise self.robots_error
        self.requests_made += 1
        return self.robots_status, self.robots_txt

    def list_page(self, page: int = 1, countries=None, date_filter=None) -> str:
        self.calls.append(("list", page, tuple(countries or ()), date_filter))
        return self._answer(self.pages.get(page, EMPTY_PAGE))

    def get_html(self, url: str, params=None) -> str:
        self.calls.append(("detail", url))
        return self._answer(self.details.get(url, self.default_detail))

    def set_min_delay(self, delay_s: float) -> None:
        self.delay_s = max(self.delay_s, delay_s)

    def set_robots(self, policy) -> None:
        self.robots_policy = policy

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def make_response(status: int = 200, text: str = "", headers: Optional[dict] = None,
                  url: str = BASE + "/job_offers") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = text.encode("utf-8")
    r.headers.update(headers or {"Content-Type": "text/html; charset=utf-8"})
    r.url = url
    return r


class FakeSession:
    """Minimal requests.Session stand-in: returns queued responses, records every GET."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "params": params, "timeout": timeout,
                           "allow_redirects": allow_redirects, "headers": dict(self.headers)})
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    # Anything that would write to Luk must not exist on the fake either.
    def post(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("luk-scraper must never POST")


class RecordingAdapter(requests.adapters.BaseAdapter):
    """Transport for a *real* ``requests.Session``: records every PreparedRequest exactly as
    requests built it (session headers, cookie jar, auth and netrc already merged) and answers
    with queued responses. No network."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {request.url}")
        response = self.responses.pop(0)
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def real_session(responses) -> tuple[requests.Session, RecordingAdapter]:
    """A real requests.Session whose https/http traffic goes to a RecordingAdapter."""
    session = requests.Session()
    adapter = RecordingAdapter(responses)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter


class FakeClock:
    """A monotonic clock that only moves when someone sleeps."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
