"""Crawler: stop conditions, dedupe, enrichment and its error tolerance (fake fetcher)."""
from __future__ import annotations

import inspect

import pytest

from conftest import (BASE, EMPTY_PAGE, TEST_UA, FakeClock, FakeFetcher, FakeSession, detail_page,
                      listing_page, make_response, read_fixture)
from luk_scraper import config, crawler
from luk_scraper.fetcher import Blocked, FetchError, Fetcher
from luk_scraper.robots import RobotsPolicy


def _url(slug):
    return f"{BASE}/job_offers/{slug}"


def test_stops_at_empty_page():
    ff = FakeFetcher({1: listing_page(["a-1", "a-2"]), 2: listing_page(["a-3"])})
    offers, stats = crawler.crawl(fetcher=ff, enrich=False)
    assert [o.slug for o in offers] == ["a-1", "a-2", "a-3"]
    assert stats["pages"] == 2 and stats["stop_reason"] == "empty"
    assert stats["status"] == "OK" and stats["errors"] == 0
    assert [c[1] for c in ff.calls if c[0] == "list"] == [1, 2, 3]


def test_stops_at_last_page_announced_by_the_nav():
    ff = FakeFetcher({1: listing_page(["a-1"], total=3, last_page=2),
                      2: listing_page(["a-2"]), 3: listing_page(["a-3"])})
    offers, stats = crawler.crawl(fetcher=ff, enrich=False)
    assert [o.slug for o in offers] == ["a-1", "a-2"]
    assert stats["stop_reason"] == "last_page"
    assert (stats["total_reported"], stats["last_page"]) == (3, 2)


def test_max_pages_cap():
    ff = FakeFetcher({p: listing_page([f"a-{p}"]) for p in range(1, 10)})
    offers, stats = crawler.crawl(fetcher=ff, enrich=False, max_pages=3)
    assert len(offers) == 3 and stats["pages"] == 3 and stats["stop_reason"] == "max_pages"


def test_hard_cap_cannot_be_exceeded(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES", 4)
    ff = FakeFetcher({p: listing_page([f"a-{p}"]) for p in range(1, 10)})
    _, stats = crawler.crawl(fetcher=ff, enrich=False, max_pages=50)
    assert stats["pages"] == 4 and stats["stop_reason"] == "max_pages"


def test_dedupes_by_slug_within_and_across_pages():
    ff = FakeFetcher({1: listing_page(["a-1", "a-1", "a-2"]),
                      2: listing_page(["a-2", "a-3"])})
    offers, stats = crawler.crawl(fetcher=ff, enrich=False)
    assert [o.slug for o in offers] == ["a-1", "a-2", "a-3"]
    assert stats["duplicates"] == 2 and stats["offers"] == 3


def test_stops_when_pages_bring_nothing_new():
    ff = FakeFetcher({p: listing_page(["a-1", "a-2"]) for p in range(1, 20)})
    offers, stats = crawler.crawl(fetcher=ff, enrich=False)
    assert len(offers) == 2
    assert stats["stop_reason"] == "no_new"
    assert stats["pages"] == 1 + config.MAX_PAGES_WITHOUT_NEW


def test_countries_and_date_filter_are_passed_through():
    ff = FakeFetcher({1: listing_page(["a-1"])})
    crawler.crawl(fetcher=ff, enrich=False, countries=["Chile", "Perú"],
                  date_filter="last_3_days")
    assert ("list", 1, ("Chile", "Perú"), "last_3_days") in ff.calls


def test_country_names_are_normalized_before_crawling():
    ff = FakeFetcher({1: listing_page(["a-1"])})
    crawler.crawl(fetcher=ff, enrich=False, countries=["peru", "chile"])
    assert ("list", 1, ("Perú", "Chile"), None) in ff.calls


def test_unknown_country_is_refused_before_any_request():
    ff = FakeFetcher({1: listing_page(["a-1"])})
    with pytest.raises(ValueError, match="Narnia"):
        crawler.crawl(fetcher=ff, countries=["Narnia"])
    assert ff.calls == []


def test_list_url_sends_worldwide_with_the_countries():
    url = crawler.list_url(1, ["Colombia"])
    assert url == config.LIST_URL + "?countries%5B%5D=Colombia&worldwide=1"


def test_default_country_is_chile():
    ff = FakeFetcher({1: listing_page(["a-1"])})
    crawler.crawl(fetcher=ff, enrich=False)
    assert ff.calls[1] == ("list", 1, ("Chile",), None)


def test_robots_is_checked_first():
    ff = FakeFetcher({1: listing_page(["a-1"])})
    crawler.crawl(fetcher=ff, enrich=False)
    assert ff.calls[0] == ("robots", config.ROBOTS_URL)


# --- robots.txt cannot be skipped or overridden ---------------------------------------------

@pytest.mark.parametrize("bypass", [
    {"check_robots": False},
    {"robots_policy": RobotsPolicy.from_text("")},                       # allows everything
    {"robots_policy": RobotsPolicy.from_text("User-agent: *\nAllow: /\n")},
])
def test_robots_cannot_be_switched_off_or_replaced(bypass):
    ff = FakeFetcher({1: listing_page(["a-1"])}, robots_txt="User-agent: *\nDisallow: /\n")
    with pytest.raises(TypeError):
        crawler.crawl(fetcher=ff, **bypass)
    assert "list" not in ff.kinds() and "detail" not in ff.kinds()


def test_crawl_signature_has_no_robots_switch():
    params = set(inspect.signature(crawler.crawl).parameters)
    assert not params & {"check_robots", "robots_policy", "robots", "policy"}


def test_crawl_installs_the_site_robots_policy_on_the_fetcher():
    ff = FakeFetcher({1: listing_page(["a-1"])})
    crawler.crawl(fetcher=ff, enrich=False)
    assert ff.robots_policy is not None
    assert ff.robots_policy.allowed(BASE + "/job_offers/a-1", TEST_UA)
    assert not ff.robots_policy.allowed(BASE + "/job_offers/a-1/recommendations", TEST_UA)


def test_crawl_checks_robots_on_redirect_hops_with_a_real_fetcher():
    """End to end with the real Fetcher: a detail page redirecting to a robots-disallowed offer
    is an error for that offer, and the disallowed URL is never requested."""
    robots_txt = "User-agent: *\nAllow: /\nDisallow: /job_offers/cargo-demo-9\n"
    session = FakeSession([
        make_response(200, robots_txt, headers={"Content-Type": "text/plain; charset=utf-8"},
                      url=BASE + "/robots.txt"),
        make_response(200, listing_page(["cargo-demo-1"])),
        make_response(200, EMPTY_PAGE),
        make_response(301, headers={"Location": "/job_offers/cargo-demo-9"},
                      url=_url("cargo-demo-1")),
    ])
    clock = FakeClock()
    f = Fetcher(session=session, sleep=clock.sleep, clock=clock, user_agent=TEST_UA)
    offers, stats = crawler.crawl(fetcher=f, clock=clock)
    assert [c["url"] for c in session.calls] == [config.ROBOTS_URL, config.LIST_URL,
                                                 config.LIST_URL, _url("cargo-demo-1")]
    assert len(offers) == 1 and stats["enriched"] == 0 and stats["errors"] == 1
    assert stats["status"] == "PARTIAL"


# --- enrichment ---------------------------------------------------------------------------

def test_enrichment_fills_detail_fields_with_the_fixture():
    slug = "analista-de-datos-empresa-demo-01-spa-chile"
    ff = FakeFetcher({1: read_fixture("listing.html")},
                     {_url(slug): read_fixture("detail.html")})
    offers, stats = crawler.crawl(fetcher=ff, max_pages=1)
    o = {x.slug: x for x in offers}[slug]
    assert o.description.startswith("Cargo: Analista de Datos Demo")
    assert (o.published_at, o.valid_through, o.external_id) == ("2026-09-23", "2026-12-22", "9001")
    # listing fields are not overwritten by the detail
    assert (o.company, o.country, o.workday) == ("Empresa Demo 01 SpA", "Chile", "Jornada Completa")
    assert stats["enriched"] == 3 and stats["status"] == "OK"


def test_one_failed_detail_never_aborts_the_run():
    ff = FakeFetcher({1: listing_page(["a-1", "a-2", "a-3"])},
                     {_url("a-1"): FetchError("HTTP 404", status=404),
                      _url("a-2"): "<html>no json-ld</html>",
                      _url("a-3"): detail_page(value="3")})
    offers, stats = crawler.crawl(fetcher=ff)
    assert len(offers) == 3
    assert [o.external_id for o in offers] == [None, None, "3"]
    assert stats["enriched"] == 1 and stats["errors"] == 2
    assert stats["status"] == "PARTIAL"


def test_unexpected_exception_in_a_detail_is_tolerated():
    ff = FakeFetcher({1: listing_page(["a-1", "a-2"])}, {_url("a-1"): RuntimeError("boom")})
    offers, stats = crawler.crawl(fetcher=ff)
    assert stats["enriched"] == 1 and stats["errors"] == 1 and len(offers) == 2


def test_blocked_during_enrichment_stops_all_requests():
    ff = FakeFetcher({1: listing_page(["a-1", "a-2", "a-3"])},
                     {_url("a-2"): Blocked("HTTP 429", status=429)})
    offers, stats = crawler.crawl(fetcher=ff)
    details = [c[1] for c in ff.calls if c[0] == "detail"]
    assert details == [_url("a-1"), _url("a-2")]          # a-3 never requested
    assert stats["blocked"] and stats["stop_reason"] == "blocked"
    assert stats["status"] == "PARTIAL" and len(offers) == 3


def test_enrich_limit_and_no_enrich():
    ff = FakeFetcher({1: listing_page(["a-1", "a-2", "a-3"])})
    _, stats = crawler.crawl(fetcher=ff, enrich_limit=2)
    assert stats["enriched"] == 2 and ff.kinds().count("detail") == 2
    ff = FakeFetcher({1: listing_page(["a-1"])})
    _, stats = crawler.crawl(fetcher=ff, enrich=False)
    assert "detail" not in ff.kinds() and stats["enriched"] == 0


# --- listing failures ---------------------------------------------------------------------

def test_blocked_on_first_page_is_error_and_stops():
    ff = FakeFetcher({1: Blocked("HTTP 403", status=403)})
    offers, stats = crawler.crawl(fetcher=ff)
    assert offers == [] and stats["status"] == "ERROR" and stats["blocked"]
    assert ff.kinds() == ["robots", "list"]


def test_blocked_mid_listing_keeps_what_it_has_and_does_not_enrich():
    ff = FakeFetcher({1: listing_page(["a-1"]), 2: Blocked("HTTP 429", status=429),
                      3: listing_page(["a-3"])})
    offers, stats = crawler.crawl(fetcher=ff)
    assert [o.slug for o in offers] == ["a-1"]
    assert stats["status"] == "PARTIAL" and stats["blocked"]
    assert "detail" not in ff.kinds()
    assert [c[1] for c in ff.calls if c[0] == "list"] == [1, 2]


def test_listing_error_mid_crawl_is_partial_but_still_enriches():
    ff = FakeFetcher({1: listing_page(["a-1"]), 2: FetchError("HTTP 500", status=500)})
    offers, stats = crawler.crawl(fetcher=ff)
    assert stats["status"] == "PARTIAL" and stats["stop_reason"] == "error"
    assert not stats["blocked"] and stats["enriched"] == 1


def test_blocked_on_robots_is_error():
    ff = FakeFetcher(robots_error=Blocked("HTTP 403", status=403))
    offers, stats = crawler.crawl(fetcher=ff)
    assert offers == [] and stats["blocked"] and stats["status"] == "ERROR"
    assert ff.kinds() == ["robots"]


def test_interrupt_keeps_collected_offers():
    ff = FakeFetcher({1: listing_page(["a-1", "a-2"])}, {_url("a-2"): KeyboardInterrupt()})
    offers, stats = crawler.crawl(fetcher=ff)
    assert len(offers) == 2 and stats["stop_reason"] == "interrupted"
    assert stats["status"] == "PARTIAL"


def test_empty_result_is_ok():
    offers, stats = crawler.crawl(fetcher=FakeFetcher({1: EMPTY_PAGE}))
    assert offers == [] and stats["status"] == "OK" and stats["pages"] == 0


@pytest.mark.parametrize("key", ["pages", "offers", "enriched", "errors", "status"])
def test_stats_have_the_documented_keys(key):
    _, stats = crawler.crawl(fetcher=FakeFetcher({1: listing_page(["a-1"])}), enrich=False)
    assert key in stats


def test_list_url_never_contains_forbidden_params():
    url = crawler.list_url(3, ["Chile"], "last_week")
    assert "sort_by" not in url and "locale" not in url
    assert url.startswith(config.LIST_URL + "?countries%5B%5D=Chile")
