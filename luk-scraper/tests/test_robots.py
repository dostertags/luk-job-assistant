"""robots.txt: parsing, RFC 9309 matching on top of urllib.robotparser, and crawl abort."""
from __future__ import annotations

import pytest

from conftest import BASE, LUK_LIKE_ROBOTS, FakeFetcher, listing_page
from luk_scraper import crawler, robots
from luk_scraper.fetcher import Blocked, FetchError
from luk_scraper.robots import RobotsDisallowed, RobotsPolicy

UA = "luk-scraper/0.1.0 (+https://example.com/contact)"
LIST = crawler.list_url(1, ["Chile"], None)


def test_luk_like_robots_allows_listing_and_detail():
    p = RobotsPolicy.from_text(LUK_LIKE_ROBOTS)
    assert p.allowed(LIST, UA)
    assert p.allowed(crawler.list_url(5, ["Chile", "Perú"], "last_3_days"), UA)
    assert p.allowed(BASE + "/job_offers/cargo-demo-1", UA)


@pytest.mark.parametrize("path", ["/companies", "/companies?q=banco", "/companies/empresa-demo-01"])
def test_luk_like_robots_allows_public_company_pages(path):
    # Only specific /companies/ sub-paths are disallowed, not /companies/* as a whole.
    assert RobotsPolicy.from_text(LUK_LIKE_ROBOTS).allowed(BASE + path, UA)


@pytest.mark.parametrize("path", ["/users/sign_in", "/profile", "/saved_jobs",
                                  "/companies/profile", "/companies/job_offers/123",
                                  "/companies/sign_in", "/admin/users", "/onboarding",
                                  "/job_offers/cargo-demo-1/recommendations",
                                  "/job_offers?sort_by=newest", "/job_offers?page=2&locale=en"])
def test_luk_like_robots_disallowed_paths(path):
    # urllib.robotparser alone says "allowed" for all of these (first match: "Allow: /").
    assert not RobotsPolicy.from_text(LUK_LIKE_ROBOTS).allowed(BASE + path, UA)


def test_longest_match_beats_a_leading_allow_all():
    text = "User-agent: *\nAllow: /\nDisallow: /job_offers\n"
    assert not RobotsPolicy.from_text(text).allowed(LIST, UA)


def test_allow_wins_ties_and_more_specific_allow_wins():
    text = "User-agent: *\nDisallow: /job_offers\nAllow: /job_offers?countries\n"
    # robotparser (first match) disallows; we take the conservative AND of both answers.
    assert not RobotsPolicy.from_text(text).allowed(LIST, UA)
    text = "User-agent: *\nAllow: /job_offers\nDisallow: /job_offers/*/recommendations\n"
    assert RobotsPolicy.from_text(text).allowed(LIST, UA)


def test_group_for_our_product_token_wins_over_star():
    text = ("User-agent: *\nAllow: /\n\n"
            "User-agent: luk-scraper\nDisallow: /\n")
    assert not RobotsPolicy.from_text(text).allowed(LIST, UA)
    text = ("User-agent: *\nDisallow: /\n\n"
            "User-agent: luk-scraper\nAllow: /job_offers\n")
    assert RobotsPolicy.from_text(text).allowed(LIST, UA)


def test_dollar_anchor_and_empty_disallow():
    p = RobotsPolicy.from_text("User-agent: *\nDisallow: /*.pdf$\nDisallow:\n")
    assert not p.allowed(BASE + "/files/x.pdf", UA)
    assert p.allowed(BASE + "/files/x.pdf?download=1", UA)
    assert p.allowed(LIST, UA)


def test_empty_robots_allows_everything():
    assert RobotsPolicy.from_text("").allowed(LIST, UA)


def test_policy_has_no_allow_all_override():
    # "No rules" is only ever the result of reading the site's robots.txt (e.g. a 404), never a
    # switch a caller can flip.
    with pytest.raises(TypeError):
        RobotsPolicy(allow_all=True)


def test_crawl_delay():
    p = RobotsPolicy.from_text("User-agent: *\nCrawl-delay: 5\nAllow: /\n")
    assert p.crawl_delay(UA) == 5.0
    assert RobotsPolicy.from_text(LUK_LIKE_ROBOTS).crawl_delay(UA) is None


# --- loading ------------------------------------------------------------------------------

def test_load_404_means_no_rules():
    assert robots.load(FakeFetcher(robots_status=404, robots_txt="")).allowed(LIST, UA)


def test_load_401_403_means_disallowed():
    assert not robots.load(FakeFetcher(robots_status=401, robots_txt="")).allowed(LIST, UA)


def test_load_unreachable_means_do_not_crawl():
    with pytest.raises(RobotsDisallowed, match="unreachable"):
        robots.load(FakeFetcher(robots_error=FetchError("HTTP 503")))
    with pytest.raises(RobotsDisallowed):
        robots.load(FakeFetcher(robots_status=501, robots_txt=""))


def test_load_blocked_propagates():
    with pytest.raises(Blocked):
        robots.load(FakeFetcher(robots_error=Blocked("HTTP 403", status=403)))


# --- the crawl ----------------------------------------------------------------------------

def test_disallowed_listing_aborts_before_any_listing_request():
    ff = FakeFetcher({1: listing_page(["cargo-demo-1"])},
                     robots_txt="User-agent: *\nDisallow: /job_offers\n")
    with pytest.raises(RobotsDisallowed):
        crawler.crawl(fetcher=ff)
    assert ff.kinds() == ["robots"]


def test_disallow_all_for_our_agent_aborts():
    ff = FakeFetcher({1: listing_page(["cargo-demo-1"])},
                     robots_txt="User-agent: luk-scraper\nDisallow: /\n")
    with pytest.raises(RobotsDisallowed):
        crawler.crawl(fetcher=ff)
    assert "list" not in ff.kinds()


def test_crawl_delay_raises_the_fetcher_pause():
    ff = FakeFetcher({1: listing_page(["cargo-demo-1"])},
                     robots_txt="User-agent: *\nCrawl-delay: 7\nAllow: /\n")
    crawler.crawl(fetcher=ff, enrich=False)
    assert ff.delay_s == 7


def test_detail_disallowed_stops_enrichment():
    ff = FakeFetcher({1: listing_page(["cargo-demo-1", "cargo-demo-2"])},
                     robots_txt="User-agent: *\nAllow: /job_offers$\nAllow: /job_offers?\n"
                                "Disallow: /job_offers/\n")
    offers, stats = crawler.crawl(fetcher=ff)
    assert len(offers) == 2
    assert "detail" not in ff.kinds()
    assert stats["stop_reason"] == "robots" and stats["status"] == "PARTIAL"
