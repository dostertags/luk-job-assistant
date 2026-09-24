"""The crawl: robots.txt check, flat pagination of the listing, dedupe, optional enrichment.

Luk's listing is a flat, paginated list (``/job_offers?countries[]=Chile&worldwide=1&page=1..N``;
verified live 2026-09-23/24), so there is no keyword enumeration: pages are walked until one of

- a page with no offer cards (``stop_reason="empty"``),
- the last page announced by the pagination nav (``"last_page"``),
- the page cap, ``--max-pages`` or the hard cap ``MAX_PAGES`` (``"max_pages"``),
- ``MAX_PAGES_WITHOUT_NEW`` pages in a row that bring no new slug (``"no_new"``; a safety net
  in case the site starts repeating itself),
- an error: ``"blocked"`` (403/429 after backoff: STOP), ``"error"``, ``"robots"``,
  ``"interrupted"`` (Ctrl+C; what was collected is kept).

The site repeats cards (within a page and across pages), so offers are deduped by slug,
first seen wins. Enrichment then fetches each offer's detail page, one request per offer at
the same polite pace; a failed detail never aborts the run, but a block does stop it.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, Optional
from urllib.parse import urlencode

from . import config, robots
from .fetcher import Blocked, Fetcher, build_list_params
from .models import Offer
from .parser import extract_meta, parse_detail, parse_listing

log = logging.getLogger(__name__)


def list_url(page: int = 1, countries: Optional[Iterable[str]] = None,
             date_filter: Optional[str] = None) -> str:
    """The full listing URL for a page (what robots.txt is checked against)."""
    query = urlencode(build_list_params(page, countries, date_filter))
    return f"{config.LIST_URL}?{query}" if query else config.LIST_URL


def _new_stats() -> dict:
    return {"pages": 0, "offers": 0, "duplicates": 0, "enriched": 0, "errors": 0,
            "status": "OK", "stop_reason": None, "blocked": False, "total_reported": None,
            "last_page": None, "requests": 0, "duration_s": 0.0}


def crawl(*, fetcher=None, countries: Optional[Iterable[str]] = None,
          date_filter: Optional[str] = None, max_pages: Optional[int] = None,
          enrich: bool = True, enrich_limit: Optional[int] = None,
          clock: Callable[[], float] = time.monotonic) -> tuple[list[Offer], dict]:
    """Crawl the public listing (Chile by default) and return ``(offers, stats)``.

    ``countries`` are Luk's country names (:data:`luk_scraper.config.COUNTRIES`), case and
    accents ignored (``"peru"`` -> ``"Perú"``); an unknown one raises ``ValueError`` before any
    request. Every listing URL also carries ``worldwide=1`` so the scope never depends on the
    operator's IP geolocation.

    ``stats``: ``pages``, ``offers``, ``duplicates``, ``enriched``, ``errors``, ``status``
    (``OK`` | ``PARTIAL`` | ``ERROR``), ``stop_reason``, ``blocked``, ``total_reported``,
    ``last_page``, ``requests`` and ``duration_s``.

    robots.txt is always read from the site first; there is no parameter to skip it or to
    supply another policy. The policy is also installed on the fetcher
    (:meth:`~luk_scraper.fetcher.Fetcher.set_robots`) so every redirect hop is checked too.
    Raises :class:`~luk_scraper.robots.RobotsDisallowed` before any listing request when
    robots.txt disallows the listing for our User-Agent (or cannot be read).
    """
    countries = config.normalize_countries(countries)   # ValueError before any request
    fetcher = fetcher or Fetcher()
    cap = config.MAX_PAGES if not max_pages or max_pages < 1 else min(int(max_pages),
                                                                       config.MAX_PAGES)
    ua = getattr(fetcher, "user_agent", None) or config.user_agent()
    stats = _new_stats()
    t0 = clock()
    offers: dict[str, Offer] = {}
    problems = False

    try:
        policy = robots.load(fetcher)
    except Blocked as e:
        log.error("Luk refused robots.txt (%s): stopping. Do not retry or work around it.", e)
        stats.update(blocked=True, errors=1, stop_reason="blocked")
        return [], _finish(stats, [], True, fetcher, clock, t0)
    fetcher.set_robots(policy)        # every later URL, redirect hops included, is checked
    robots.ensure_allowed(policy, list_url(1, countries, date_filter), ua)
    delay = policy.crawl_delay(ua)
    if delay:
        fetcher.set_min_delay(delay)
        log.info("robots.txt Crawl-delay: waiting at least %.1fs between requests", delay)

    try:
        problems = _walk_listing(fetcher, policy, ua, countries, date_filter, cap, offers, stats)
        # After a block or a robots stop we make no further request at all. After a plain
        # listing error (e.g. a 500 that outlived the retries) the offers already collected
        # are still enriched.
        if enrich and offers and stats["stop_reason"] not in ("blocked", "robots"):
            problems = _enrich(fetcher, policy, ua, list(offers.values()), enrich_limit,
                               stats) or problems
    except KeyboardInterrupt:
        log.warning("interrupted: keeping the %d offers collected so far", len(offers))
        stats["stop_reason"] = "interrupted"
        problems = True

    result = list(offers.values())
    return result, _finish(stats, result, problems, fetcher, clock, t0)


def _finish(stats: dict, offers: list[Offer], problems: bool, fetcher, clock, t0) -> dict:
    stats["offers"] = len(offers)
    problems = problems or stats["errors"] > 0 or stats["blocked"]
    stats["status"] = "OK" if not problems else ("PARTIAL" if offers else "ERROR")
    stats["requests"] = getattr(fetcher, "requests_made", 0)
    stats["duration_s"] = round(clock() - t0, 1)
    return stats


def _walk_listing(fetcher, policy, ua, countries, date_filter, cap, offers, stats) -> bool:
    """Paginate into ``offers`` (slug -> Offer). Returns True if it stopped on a problem."""
    page = 1
    without_new = 0
    while True:
        url = list_url(page, countries, date_filter)
        if not policy.allowed(url, ua):
            log.error("robots.txt disallows %s: stopping", url)
            stats["stop_reason"] = "robots"
            return True
        try:
            html = fetcher.list_page(page=page, countries=countries, date_filter=date_filter)
        except Blocked as e:
            log.error("Luk is refusing our requests (%s): stopping. Do not retry or work "
                      "around it.", e)
            stats.update(blocked=True, stop_reason="blocked", errors=stats["errors"] + 1)
            return True
        except Exception as e:  # noqa: BLE001 - one listing failure ends the walk cleanly
            log.error("listing page %d failed: %s", page, e)
            stats.update(stop_reason="error", errors=stats["errors"] + 1)
            return True

        cards = parse_listing(html)
        if not cards:
            stats["stop_reason"] = "empty"
            return False
        stats["pages"] += 1
        new = 0
        for offer in cards:
            if offer.slug in offers:
                stats["duplicates"] += 1
            else:
                offers[offer.slug] = offer
                new += 1
        if page == 1:
            stats["total_reported"], stats["last_page"] = extract_meta(html)
            log.info("Luk reports %s results over %s pages", stats["total_reported"] or "?",
                     stats["last_page"] or "?")
        log.info("page %d: %d cards, %d new (%d offers so far)", page, len(cards), new,
                 len(offers))

        without_new = 0 if new else without_new + 1
        if stats["last_page"] and page >= stats["last_page"]:
            stats["stop_reason"] = "last_page"
            return False
        if page >= cap:
            stats["stop_reason"] = "max_pages"
            return False
        if without_new >= config.MAX_PAGES_WITHOUT_NEW:
            log.warning("%d pages in a row without new offers: stopping", without_new)
            stats["stop_reason"] = "no_new"
            return False
        page += 1


def _enrich(fetcher, policy, ua, offers: list[Offer], limit: Optional[int], stats) -> bool:
    """Fill each offer from its detail page. Returns True if it had to stop early."""
    targets = offers[:limit] if limit and limit > 0 else offers
    log.info("enriching %d offers from their detail pages", len(targets))
    for i, offer in enumerate(targets, 1):
        if not policy.allowed(offer.url, ua):
            log.error("robots.txt disallows %s: stopping enrichment", offer.url)
            stats["stop_reason"] = "robots"
            return True
        try:
            html = fetcher.get_html(offer.url)
        except Blocked as e:
            log.error("Luk is refusing our requests (%s): stopping. Do not retry or work "
                      "around it.", e)
            stats.update(blocked=True, stop_reason="blocked", errors=stats["errors"] + 1)
            return True
        except Exception as e:  # noqa: BLE001 - one broken detail never aborts the run
            stats["errors"] += 1
            log.warning("detail failed for %s: %s", offer.slug, e)
            continue
        try:
            fields = parse_detail(html)
        except Exception as e:  # noqa: BLE001
            fields = {}
            log.warning("detail of %s could not be parsed: %s", offer.slug, e)
        if not fields:
            stats["errors"] += 1
            log.warning("no JobPosting data in the detail of %s", offer.slug)
            continue
        for name, value in fields.items():
            setattr(offer, name, value)
        stats["enriched"] += 1
        if i % 25 == 0:
            log.info("enriched %d/%d", i, len(targets))
    return False
