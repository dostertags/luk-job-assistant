"""api.py — the only layer the CLI and MCP call (spec §5, §7.2).

Location modes and pagination (§5.1), show/company semantics (§5.2), Algolia discovery and its 24 h
cache (§3), private reload (§4.5), capture (§8.1), doctor and schema export. Parsers are faked here
(they are tested on the public fixtures in their own module); the transport is httpx.MockTransport,
so every test is offline and asserts on the exact requests that were (or were not) sent.
"""

import hashlib
import json
import sys
import time
from datetime import date, datetime, timezone

import httpx
import pytest

from luk_cli import api, auth, config, parsers, scrub
from luk_cli.api import ApiContext
from luk_cli.config import CACHE_TTL_S, AlgoliaConfig, load_settings
from luk_cli.errors import (
    AlgoliaRejected, AuthRequired, Blocked, BudgetExceeded, InvalidArgument, NetworkError, NotFound,
    RateLimited, ScrubFailed, SiteChanged,
)
from luk_cli.models import (
    Application, Area, AreaRef, Company, CompanyCard, Cv, Envelope, ErrorEnvelope, JobCard, JobPosting,
    SearchFilters, SearchQuery, SessionMeta, SimilarRoles, Suggestion, WhoAmI,
)
from luk_cli.parsers import CompaniesPage, Pagination, Parsed, PrivateList, SearchPage
from luk_cli.ratelimit import ALGOLIA_LIMITER

BASE = "https://www.takealuk.com"
TODAY = date(2026, 9, 23)
PAGE = '<html lang="es-CL"><body><main>ok</main></body></html>'
QUERIES = "/1/indexes/*/queries"
ALGOLIA = AlgoliaConfig("ABCDE12345", "public-search-key", "JobOffer_query_suggestions")


def html(body=PAGE, status=200):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def as_json(data, status=200):
    return httpx.Response(status, json=data)


def redirect(location):
    return httpx.Response(302, headers={"location": location})


class Site:
    """Transport handler: path → queued responses (the last one repeats); records every request."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def add(self, path, *responses):
        self.routes[path] = list(responses)

    def paths(self):
        return [r.url.path for r in self.requests]

    def __call__(self, request):
        self.requests.append(request)
        queue = self.routes.get(request.url.path)
        if not queue:
            return html("<html lang='es-CL'>missing</html>", status=404)
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(nxt.status_code, headers=nxt.headers, content=nxt.content)


def card(i, **kw):
    return JobCard(slug=f"job-{i}", url=f"{BASE}/job_offers/job-{i}", title=f"Job {i}", **kw)


def posting(slug, **kw):
    return JobPosting(slug=slug, url=f"{BASE}/job_offers/{slug}", title=slug.title(), status="open",
                      description_text=kw.pop("description_text", "desc"), **kw)


def company_card(i):
    return CompanyCard(slug=f"co-{i}", url=f"{BASE}/companies/co-{i}", name=f"Co {i}")


def search_page(cards, *, total=None, last_page=None, has_next=False, effective=None, related=()):
    return SearchPage(total=len(cards) if total is None else total, cards=cards,
                      pagination=Pagination(last_page, has_next), effective_location=effective,
                      related_roles=list(related))


def area(id_, name, *, path=None, label=None, match=True):
    return Area(id=id_, name=name, display_path=path or f"{name}, Chile", area_type_label=label,
                visitor_country_match=match)


def params(request):
    return request.url.params.multi_items()


@pytest.fixture(autouse=True)
def _no_algolia_wait(monkeypatch):
    monkeypatch.setattr(ALGOLIA_LIMITER, "min_interval_s", 0.0)


@pytest.fixture
def site():
    return Site()


@pytest.fixture
def algolia():
    return Site()


@pytest.fixture
def make_ctx(store, limiter, site, algolia):
    made = []

    def make(settings):
        ctx = ApiContext(settings, store, limiter, luk_transport=httpx.MockTransport(site),
                         algolia_transport=httpx.MockTransport(algolia), today=lambda: TODAY)
        made.append(ctx)
        return ctx

    yield make
    for ctx in made:
        ctx.close()


@pytest.fixture
def ctx(make_ctx, settings):
    return make_ctx(settings)


@pytest.fixture
def search(monkeypatch):
    """page number → SearchPage served by the fake parse_search; `.calls` records its kwargs."""

    class Pages(dict):
        calls = []

    pages = Pages()

    def parse(html_text, *, page, today, base_url, capture_path):
        pages.calls.append({"page": page, "today": today, "base_url": base_url, "capture_path": capture_path})
        return pages[page]

    monkeypatch.setattr(parsers, "parse_search", parse)
    return pages


@pytest.fixture
def areas(monkeypatch):
    """The list the fake parse_areas returns; `.calls` records (body, content_type)."""

    class Areas(list):
        calls = []

    result = Areas()

    def parse(body, content_type):
        result.calls.append((body, content_type))
        return list(result)

    monkeypatch.setattr(parsers, "parse_areas", parse)
    return result


# -- search: location modes (§5.1) -----------------------------------------------------------------

def test_search_default_sends_explicit_chile_and_one_pill_per_role(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([card(1), card(2)], related=["Contador"], effective=AreaRef(id=1021, display_path="Chile"))

    env = api.search_jobs(ctx, roles=["analista financiero", " Contador "])

    (req,) = site.requests
    assert req.url.path == "/job_offers"
    assert params(req) == [("job_positions", "analista financiero,Contador"), ("locations", "1021")]
    assert "cookie" not in req.headers
    assert search.calls == [{"page": 1, "today": TODAY, "base_url": BASE,
                             "capture_path": "/job_offers?job_positions=analista+financiero%2CContador&locations=1021"}]
    data = env.data
    assert env.kind == "job_search" and env.warnings == []
    assert [c.slug for c in data.results] == ["job-1", "job-2"]
    assert (data.total, data.page, data.last_fetched_page, data.per_page, data.last_page) == (2, 1, 1, None, None)
    assert (data.has_more, data.next_page, data.details, data.not_found) == (False, None, None, [])
    assert data.query == SearchQuery(roles=["analista financiero", "Contador"], location_ids=[1021])
    assert data.effective_location == AreaRef(id=1021, display_path="Chile")
    assert data.related_roles == ["Contador"] and data.location_alternatives == []


def test_search_with_zero_roles_sends_no_pills(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([])
    env = api.search_jobs(ctx)
    assert params(site.requests[0]) == [("locations", "1021")]
    assert env.data.results == [] and env.data.total == 0


@pytest.mark.parametrize("kwargs", [
    {"roles": ["a,b"]},
    {"roles": ["x" * 51]},
    {"roles": ["   "]},
    {"locations": ["santiago"], "worldwide": True},
    {"countries": ["CO"], "location_ids": [1318]},
    {"page": 0},
    {"limit": 0},
    {"limit": 151},
    {"location_ids": [0]},
    {"min_salary": 0},
    {"max_salary": -5},
    {"currency": "COP"},
    {"min_salary": 5, "currency": "USD"},
    {"job_types": ["remote"]},
    {"posted_within": "2w"},
    {"countries": ["AR"]},
])
def test_search_rejects_bad_input_before_any_request(ctx, site, kwargs):
    with pytest.raises(InvalidArgument):
        api.search_jobs(ctx, **kwargs)
    assert site.requests == []


def test_search_location_text_prefers_exact_folded_name_in_the_visitor_country(ctx, site, search, areas):
    site.add("/flexible_search/areas", as_json({"areas": []}))
    site.add("/job_offers", html())
    areas.extend([
        area(9579, "Santiago de Cuba", path="Santiago de Cuba, Cuba", match=False),
        area(1, "Ñuñoa", path="Ñuñoa, Perú", label="Distrito", match=False),
        area(1349, "Ñuñoa", path="Ñuñoa, Santiago, Región Metropolitana, Chile", label="Comuna"),
        area(2, "Ñuñoa Norte"),
        area(3, "Villa Ñuñoa"),
    ])
    search[1] = search_page([card(1)])

    env = api.search_jobs(ctx, roles=["analista"], locations=["nunoa"], location_ids=[1318])

    areas_req, search_req = site.requests
    assert areas_req.url.path == "/flexible_search/areas" and params(areas_req) == [("q", "nunoa")]
    assert areas_req.headers["accept"] == "application/json"
    assert params(search_req) == [("job_positions", "analista"), ("locations", "1349,1318")]
    assert env.data.query.location_ids == [1349, 1318]
    assert [a.id for a in env.data.location_alternatives] == [9579, 1, 2]
    assert env.warnings[0] == "Ubicación: Ñuñoa, Santiago, Región Metropolitana, Chile [Comuna] (1349)"
    assert env.warnings[1].startswith("Alternativas: Santiago de Cuba, Cuba (9579); Ñuñoa, Perú [Distrito] (1);")


def test_search_location_without_exact_match_takes_the_first_result(ctx, site, search, areas):
    site.add("/flexible_search/areas", as_json({"areas": []}))
    site.add("/job_offers", html())
    areas.extend([area(1318, "Santiago", label="Provincia"), area(1348, "Santiago", label="Comuna")])
    search[1] = search_page([])
    env = api.search_jobs(ctx, locations=["stgo centro"])
    assert params(site.requests[1]) == [("locations", "1318")]
    assert [a.id for a in env.data.location_alternatives] == [1348]


def test_unknown_location_is_not_found_and_no_search_is_sent(ctx, site, areas):
    site.add("/flexible_search/areas", as_json({"areas": []}))
    with pytest.raises(NotFound, match="No Luk area matches 'atlantida'"):
        api.search_jobs(ctx, locations=["atlantida"])
    assert site.paths() == ["/flexible_search/areas"]


def test_area_lookups_are_cached_for_24_hours(ctx, make_ctx, settings, site, search, areas):
    site.add("/flexible_search/areas", as_json({"areas": []}))
    site.add("/job_offers", html())
    areas.append(area(1318, "Santiago"))
    search[1] = search_page([])

    api.search_jobs(ctx, locations=["santiago"])
    api.search_jobs(make_ctx(settings), locations=["santiago"])  # a new process reads the cache file too
    assert site.paths() == ["/flexible_search/areas", "/job_offers", "/job_offers"]

    for cached in settings.areas_cache_dir.glob("*.json"):  # age it past the TTL
        raw = json.loads(cached.read_text("utf-8"))
        raw["fetched_at"] = time.time() - CACHE_TTL_S - 1
        cached.write_text(json.dumps(raw), "utf-8")
    api.search_jobs(ctx, locations=["santiago"])
    assert site.paths()[-2:] == ["/flexible_search/areas", "/job_offers"]


def test_countries_are_sent_with_worldwide_and_never_with_locations(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([])
    env = api.search_jobs(ctx, roles=["analista"], countries=["MX", "PE", "MX"])
    assert params(site.requests[0]) == [
        ("job_positions", "analista"), ("worldwide", "1"), ("countries[]", "México"), ("countries[]", "Perú"),
    ]
    assert env.data.query == SearchQuery(roles=["analista"], countries=["MX", "PE"], worldwide=True)


def test_worldwide_sends_only_worldwide(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([])
    env = api.search_jobs(ctx, roles=["analista"], worldwide=True)
    assert params(site.requests[0]) == [("job_positions", "analista"), ("worldwide", "1")]
    assert env.data.query.worldwide is True and env.data.query.location_ids == []


def test_verified_filters_map_to_the_site_params(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([])
    env = api.search_jobs(ctx, roles=["analista"], job_types=["part_time", "intern", "part_time"],
                          posted_within="1w", min_salary=1_000_000, max_salary=2_000_000)
    assert params(site.requests[0]) == [
        ("job_positions", "analista"), ("locations", "1021"), ("job_types[]", "part_time"), ("job_types[]", "intern"),
        ("date", "last_week"), ("min_salary", "1000000"), ("max_salary", "2000000"), ("salary_currency", "CLP"),
    ]
    assert env.data.query.filters == SearchFilters(job_types=["part_time", "intern"], posted_within="1w",
                                                   min_salary=1_000_000, max_salary=2_000_000, currency="CLP")


def test_explicit_currency_is_sent_with_its_bound(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([])
    api.search_jobs(ctx, max_salary=5, currency="COP")
    assert params(site.requests[0])[-2:] == [("max_salary", "5"), ("salary_currency", "COP")]


@pytest.mark.parametrize(("kwargs", "param"), [
    ({"job_types": ["intern"]}, "job_types"),
    ({"posted_within": "24h"}, "posted_within"),
    ({"min_salary": 1}, "salary"),
    ({"countries": ["CO"]}, "countries"),
    ({"worldwide": True}, "worldwide"),
])
def test_unverified_params_are_refused_with_zero_requests(ctx, site, monkeypatch, kwargs, param):
    monkeypatch.setattr(config, "VERIFIED_PARAMS", config.VERIFIED_PARAMS - {param})
    with pytest.raises(InvalidArgument, match="unverified"):
        api.search_jobs(ctx, **kwargs)
    assert site.requests == []


# -- search: pagination (§5.1) ----------------------------------------------------------------------

def test_limit_fetches_following_pages_dedupes_and_trims(ctx, site, search):
    site.add("/job_offers", html())
    search[1] = search_page([card(i) for i in range(15)], total=40, last_page=3, has_next=True)
    search[2] = search_page([card(14), *(card(i) for i in range(15, 29))], total=40, last_page=3, has_next=True)

    env = api.search_jobs(ctx, roles=["analista"], limit=20)

    assert [params(r)[-1] for r in site.requests] == [("locations", "1021"), ("page", "2")]
    assert search.calls[1]["capture_path"].endswith("&page=2")
    data = env.data
    assert [c.slug for c in data.results] == [f"job-{i}" for i in range(20)]
    assert (data.total, data.page, data.offset, data.last_fetched_page, data.per_page) == (40, 1, 0, 2, 15)
    # page 2 = [job-14 (a repeat), job-15 … job-28]: its first 6 were consumed, job-20 comes next
    assert (data.last_page, data.has_more, data.next_page, data.next_offset) == (3, True, 2, 6)
    assert env.warnings == []


SITE_PAGES = {1: [card(i) for i in range(15)], 2: [card(14), *(card(i) for i in range(15, 29))],
              3: [card(i) for i in range(29, 33)]}


def serve_site_pages(site, search):
    site.add("/job_offers", html())
    for number, cards in SITE_PAGES.items():
        search[number] = search_page(cards, total=33, last_page=3, has_next=number < 3)


@pytest.mark.parametrize("limit", [1, 3, 14])
def test_a_cut_page_continues_right_after_its_last_result(ctx, site, search, limit):
    serve_site_pages(site, search)
    env = api.search_jobs(ctx, limit=limit)
    assert [c.slug for c in env.data.results] == [f"job-{i}" for i in range(limit)]
    assert (env.data.has_more, env.data.next_page, env.data.next_offset) == (True, 1, limit)
    assert env.warnings == []


def test_a_consumed_page_continues_at_the_top_of_the_next(ctx, site, search):
    serve_site_pages(site, search)
    data = api.search_jobs(ctx, limit=15).data
    assert (len(site.requests), data.has_more, data.next_page, data.next_offset) == (1, True, 2, 0)


def test_offset_skips_the_top_of_the_start_page_and_is_never_sent(ctx, site, search):
    serve_site_pages(site, search)
    env = api.search_jobs(ctx, page=2, offset=3, limit=5)
    assert [params(r) for r in site.requests] == [[("locations", "1021"), ("page", "2")]]
    assert [c.slug for c in env.data.results] == [f"job-{i}" for i in range(17, 22)]
    assert (env.data.page, env.data.offset, env.data.next_page, env.data.next_offset) == (2, 3, 2, 8)


def test_the_cursor_steps_over_offers_luk_lists_again(ctx, site, search):
    """Luk may list an offer on two pages when its results shift. Within a call a repeat is dropped (first
    wins); the cursor lands on the next offer not yet returned, and a continuation never returns again an
    offer from the top of its start page, so neither repeat reaches the caller."""
    site.add("/job_offers", html())
    search[1] = search_page([card(i) for i in range(15)], total=40, last_page=2, has_next=True)
    search[2] = search_page([card(15), card(3), card(4), *(card(i) for i in range(16, 20)), card(15)],
                            total=40, last_page=2)

    first = api.search_jobs(ctx, limit=16).data
    assert [c.slug for c in first.results] == [f"job-{i}" for i in range(16)]
    assert (first.next_page, first.next_offset) == (2, 3)  # job-3 and job-4 were stepped over

    rest = api.search_jobs(ctx, page=2, offset=3, limit=15).data
    assert [c.slug for c in rest.results] == [f"job-{i}" for i in range(16, 20)]  # the trailing job-15 too
    assert (rest.has_more, rest.next_page, rest.next_offset) == (False, None, None)


@pytest.mark.parametrize("limit", range(1, 35))
def test_walking_the_cursor_over_luk_repeats_never_skips(ctx, site, search, limit):
    """SITE_PAGES lists job-14 on pages 1 and 2. Nothing is ever skipped; job-14 comes twice only when a call
    ends exactly at the end of page 1 (the continuation starts on page 2 and cannot know page 1)."""
    serve_site_pages(site, search)
    got: list[str] = []
    page, offset = 1, 0
    for _ in range(40):
        data = api.search_jobs(ctx, page=page, offset=offset, limit=limit).data
        got += [c.slug for c in data.results]
        if not data.has_more:
            break
        page, offset = data.next_page, data.next_offset
    assert list(dict.fromkeys(got)) == [f"job-{i}" for i in range(33)]
    assert sorted(slug for slug in set(got) if got.count(slug) > 1) == (["job-14"] if 15 % limit == 0 else [])


def test_pagination_stops_when_there_is_no_next_page(ctx, site, search):
    site.add("/job_offers", html())
    search[2] = search_page([card(i) for i in range(15)], total=19, last_page=2, has_next=True)
    search[3] = search_page([card(i) for i in range(15, 19)], total=19, last_page=3)
    env = api.search_jobs(ctx, page=2, limit=150)
    assert len(site.requests) == 2 and len(env.data.results) == 19
    assert (env.data.page, env.data.last_fetched_page, env.data.has_more, env.data.next_page) == (2, 3, False, None)
    assert env.data.next_offset is None


def test_pagination_stops_after_ten_pages(ctx, site, search):
    site.add("/job_offers", html())
    for n in range(1, 12):
        search[n] = search_page([card(f"{n}-{i}") for i in range(5)], total=500, last_page=100, has_next=True)
    env = api.search_jobs(ctx, limit=150)
    assert len(site.requests) == api.MAX_PAGES_PER_CALL
    assert len(env.data.results) == 50
    assert (env.data.last_fetched_page, env.data.has_more, env.data.next_page, env.data.next_offset) == (10, True, 11, 0)


def test_page_past_the_last_is_empty_with_a_note(ctx, site, search):
    site.add("/job_offers", html())
    search[20] = search_page([], total=274, last_page=19)
    env = api.search_jobs(ctx, roles=["analista"], page=20)
    assert env.data.results == [] and env.data.total == 274 and env.data.has_more is False
    assert (env.data.next_page, env.data.next_offset) == (None, None)
    assert env.warnings == ["page 20 is past the last page (19)"]


def test_offset_past_the_end_of_the_start_page_is_noted(ctx, site, search):
    """A cursor never points past its page, but the last page can shrink between two calls: nothing is left
    there, so the call continues with the next page if any, with a note."""
    serve_site_pages(site, search)
    last = api.search_jobs(ctx, page=3, offset=4)
    assert (last.data.results, last.data.has_more, last.data.next_page) == ([], False, None)
    assert last.warnings == ["page 3 has only 4 results; offset 4 skipped them all"]

    full = api.search_jobs(ctx, page=1, offset=40, limit=2)
    assert [c.slug for c in full.data.results] == ["job-15", "job-16"]  # job-14 was on page 1 already
    assert (full.data.next_page, full.data.next_offset) == (2, 3)
    assert full.warnings == ["page 1 has only 15 results; offset 40 skipped them all"]


@pytest.mark.parametrize("offset", [-1, api.MAX_OFFSET + 1, 10**18])
@pytest.mark.parametrize("call", [api.search_jobs, api.search_companies, api.saved_jobs, api.applications])
def test_an_out_of_range_offset_is_refused_with_zero_requests(ctx, site, saved_session, call, offset):
    """The offset counts within one page (pages hold ≤24 results), not across the whole list."""
    saved_session()
    with pytest.raises(InvalidArgument, match=f"offset must be between 0 and {api.MAX_OFFSET}"):
        call(ctx, offset=offset)
    assert site.requests == []


def test_page_warnings_are_copied_once(ctx, site, search):
    site.add("/job_offers", html('<html lang="en"><body>x</body></html>'))
    search[1] = search_page([card(1)], has_next=True)
    search[2] = search_page([card(2)])
    env = api.search_jobs(ctx, limit=5)
    assert len(env.warnings) == 1 and "lang='en'" in env.warnings[0]


# -- the continuation cursor, every list and every limit (§5.1, §5.4) -------------------------------------

class Listing:
    """A Luk list with no repeated key, served by `?page=N` (none = 1). Each body names its page, so the
    fake parser serves that page's items; `.items` is the whole list in order."""

    def __init__(self, sizes, make):
        self.pages, first = {}, 0
        for number, size in enumerate(sizes, 1):
            self.pages[number] = [make(i) for i in range(first, first + size)]
            first += size
        self.items = [item for items in self.pages.values() for item in items]

    def __call__(self, request):
        return html(f'<html lang="es-CL"><body><main>page {request.url.params.get("page", "1")}</main></body></html>')

    def page(self, text):
        number = int(text.split("page ")[1].split("<")[0])
        return self.pages.get(number, []), Pagination(len(self.pages), number < len(self.pages))


def application(i):
    return Application(title=f"Analista {i // 2}", status_text="Vista")  # rows 2k and 2k+1 are equal: never deduped


LISTINGS = {
    # kind: (page sizes, item factory, (parser to fake, its result), api call)
    "search": ((15, 15, 4), card, ("parse_search", lambda items, pagination, total: search_page(
        items, total=total, last_page=pagination.last_page, has_next=pagination.has_next)),
        lambda c, **kw: api.search_jobs(c, roles=["analista"], **kw)),
    "companies": ((24, 24, 5), company_card, ("parse_companies", lambda items, pagination, total: CompaniesPage(
        total, items, pagination)), api.search_companies),
    "saved": ((4,) * 13, card, ("parse_saved_jobs", lambda items, pagination, total: PrivateList(
        items, pagination, verified=True)), api.saved_jobs),  # 13 pages: a limit >40 meets the 10-page stop
    "applications": ((10, 10, 10, 3), application, ("parse_applications", lambda items, pagination, total: PrivateList(
        items, pagination, verified=True)), api.applications),
}


@pytest.mark.parametrize("kind", LISTINGS)
def test_following_the_cursor_returns_every_result_once_for_every_limit(
    kind, settings, store, no_limit, saved_session, monkeypatch,
):
    """The §5.1 property: from (page 1, offset 0), following (next_page, next_offset) with the same limit
    returns the whole list in order — nothing skipped, nothing repeated — for every limit 1-150; and
    has_more ⇔ next_page ⇔ next_offset, with next_offset always inside next_page."""
    sizes, make, (parser, result), call = LISTINGS[kind]
    listing = Listing(sizes, make)
    monkeypatch.setattr(parsers, parser, lambda text, **kw: result(*listing.page(text), len(listing.items)))
    saved_session()
    ctx = ApiContext(settings, store, no_limit, luk_transport=httpx.MockTransport(listing), today=lambda: TODAY)
    try:
        for limit in range(1, api.MAX_LIMIT + 1):
            got: list = []
            page, offset = 1, 0
            for _ in range(len(listing.items) + 1):
                data = call(ctx, page=page, offset=offset, limit=limit).data
                assert (data.page, data.offset) == (page, offset)
                assert data.has_more is (data.next_page is not None) is (data.next_offset is not None), limit
                pages_read = data.last_fetched_page - page + 1
                assert len(data.results) == limit or not data.has_more or pages_read == api.MAX_PAGES_PER_CALL
                got += data.results
                if not data.has_more:
                    break
                assert 0 <= data.next_offset < len(listing.pages[data.next_page]), limit
                assert (data.next_page, data.next_offset) > (page, offset), limit
                page, offset = data.next_page, data.next_offset
            assert got == listing.items, f"{kind}, limit {limit}: the continuation skipped or repeated a result"
    finally:
        ctx.close()


def test_search_server_error_is_a_network_error(ctx, site, search):
    site.add("/job_offers", html(status=500))
    with pytest.raises(NetworkError, match="HTTP 500"):
        api.search_jobs(ctx)


# -- show / get_jobs (§5.2, §7.2) ------------------------------------------------------------------

@pytest.fixture
def jobs(monkeypatch):
    """slug → JobPosting served by the fake parse_job (warns 'dom only' for slugs starting 'dom')."""

    class Jobs(dict):
        calls = []

    served = Jobs()

    def parse(html_text, *, slug, base_url, today, capture_path):
        served.calls.append({"slug": slug, "capture_path": capture_path, "today": today})
        return Parsed(served[slug], ["dom only"] if slug.startswith("dom") else [])

    monkeypatch.setattr(parsers, "parse_job", parse)
    return served


@pytest.mark.parametrize("value", [
    "http://www.takealuk.com/job_offers/x", "https://evil.com/job_offers/x", "C:\\x.bat", "\\\\host\\share",
    "../saved_jobs", "x/save_later", "https://www.takealuk.com/companies/x",
])
def test_show_rejects_anything_but_a_luk_job_with_zero_requests(ctx, site, value):
    with pytest.raises(InvalidArgument):
        api.get_job(ctx, value)
    assert site.requests == []


def test_show_fetches_the_rebuilt_path_anonymously(ctx, site, jobs):
    site.add("/job_offers/analista-tributario", html())
    jobs["analista-tributario"] = posting("analista-tributario")
    env = api.get_job(ctx, "https://takealuk.com/job_offers/analista-tributario/?utm=x#top")
    (req,) = site.requests
    assert str(req.url) == f"{BASE}/job_offers/analista-tributario" and "cookie" not in req.headers
    assert env.kind == "job" and env.data.slug == "analista-tributario" and env.data.canonical_slug is None
    assert jobs.calls == [{"slug": "analista-tributario", "capture_path": "/job_offers/analista-tributario",
                           "today": TODAY}]


@pytest.mark.parametrize("status", [404, 410])
def test_show_gone_offer_is_not_found(ctx, site, status):
    site.add("/job_offers/zzzz-no-existe", html(status=status))
    with pytest.raises(NotFound, match="zzzz-no-existe"):
        api.get_job(ctx, "zzzz-no-existe")


def test_show_redirect_to_another_offer_sets_canonical_slug(ctx, site, jobs):
    site.add("/job_offers/old-slug", redirect("/job_offers/new-slug"))
    site.add("/job_offers/new-slug", html())
    jobs["old-slug"] = posting("old-slug")
    env = api.get_job(ctx, "old-slug")
    assert env.data.canonical_slug == "new-slug"


def test_show_redirect_to_the_same_offer_is_not_a_canonical_slug(ctx, site, jobs):
    site.add("/job_offers/same-slug", redirect("/job_offers/same-slug/"))
    site.add("/job_offers/same-slug/", html())
    jobs["same-slug"] = posting("same-slug")
    assert api.get_job(ctx, "same-slug").data.canonical_slug is None


@pytest.mark.parametrize("target", ["/job_offers", "/companies/home", "/"])
def test_show_redirect_elsewhere_is_not_found(ctx, site, jobs, target):
    site.add("/job_offers/closed-offer", redirect(target))
    site.add(target, html())
    with pytest.raises(NotFound, match="no longer available"):
        api.get_job(ctx, "closed-offer")
    assert jobs.calls == []


def test_show_merges_page_and_parser_warnings(ctx, site, jobs):
    site.add("/job_offers/dom-only", html('<html lang="en">x</html>'))
    jobs["dom-only"] = posting("dom-only")
    env = api.get_job(ctx, "dom-only")
    assert env.warnings[1:] == ["dom only"] and "lang='en'" in env.warnings[0]


def test_get_jobs_caps_text_collects_missing_and_dedupes(ctx, site, jobs):
    site.add("/job_offers/long", html())
    site.add("/job_offers/short", html())
    site.add("/job_offers/gone", html(status=410))
    jobs["long"] = posting("long", description_text="d" * 5000, requirements_text="r" * 4001)
    jobs["short"] = posting("short", requirements_text="r")

    env = api.get_jobs(ctx, ["long", f"{BASE}/job_offers/gone", "short", "long"])

    assert env.kind == "jobs" and site.paths() == ["/job_offers/long", "/job_offers/gone", "/job_offers/short"]
    long_, short = env.data.results
    assert len(long_.description_text) == api.TEXT_CAP and len(long_.requirements_text) == api.TEXT_CAP
    assert long_.text_truncated is True and short.text_truncated is False and short.requirements_text == "r"
    assert env.data.not_found == ["gone"]


def test_get_jobs_full_keeps_the_whole_text(ctx, site, jobs):
    site.add("/job_offers/long", html())
    jobs["long"] = posting("long", description_text="d" * 5000)
    env = api.get_jobs(ctx, ["long"], full=True)
    assert len(env.data.results[0].description_text) == 5000 and env.data.results[0].text_truncated is False


@pytest.mark.parametrize(("targets", "full"), [([], False), (["a"] * 11, False), (["a", "b", "c", "d"], True),
                                               (["ok", "bad slug"], False)])
def test_get_jobs_bounds_are_checked_before_any_request(ctx, site, targets, full):
    with pytest.raises(InvalidArgument):
        api.get_jobs(ctx, targets, full=full)
    assert site.requests == []


# -- Algolia suggestions (§2.1, §3, §4.7) ---------------------------------------------------------

@pytest.fixture
def suggestions(monkeypatch):
    served = [Suggestion(query=f"analista {i}", popularity=10 - i) for i in range(8)]
    monkeypatch.setattr(parsers, "parse_suggestions", lambda body, content_type: list(served))
    return served


def test_suggest_discovers_the_public_config_once_and_caches_it(ctx, make_ctx, settings, site, algolia,
                                                                suggestions, monkeypatch):
    site.add("/", html())
    algolia.add(QUERIES, as_json({"results": [{"hits": []}]}))
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: ALGOLIA)

    env = api.suggest(ctx, " analista fin ", limit=3)

    assert env.kind == "suggestion_list" and env.data.query == "analista fin"
    assert [s.query for s in env.data.results] == ["analista 0", "analista 1", "analista 2"]
    (req,) = algolia.requests
    assert req.method == "POST" and req.url.host == "abcde12345-dsn.algolia.net"
    assert req.headers["x-algolia-application-id"] == "ABCDE12345"
    assert json.loads(req.content)["requests"][0] == {
        "indexName": "JobOffer_query_suggestions", "query": "analista fin", "hitsPerPage": 3,
        "attributesToRetrieve": ["query", "popularity"],
    }
    assert "cookie" not in site.requests[0].headers
    cached = json.loads(settings.algolia_cache_path.read_text("utf-8"))
    assert cached["data"] == {"app_id": "ABCDE12345", "api_key": "public-search-key",
                              "index": "JobOffer_query_suggestions"}

    api.suggest(ctx, "contador")
    api.suggest(make_ctx(settings), "contador")
    assert site.paths() == ["/"] and len(algolia.requests) == 3


def test_stale_or_corrupt_algolia_cache_is_rediscovered(ctx, settings, site, algolia, suggestions, monkeypatch):
    site.add("/", html())
    algolia.add(QUERIES, as_json({"results": [{"hits": []}]}))
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: ALGOLIA)
    settings.cache_dir.mkdir(parents=True)
    settings.algolia_cache_path.write_text(json.dumps(
        {"fetched_at": time.time() - CACHE_TTL_S - 1, "data": {"app_id": "OLDAPP1234", "api_key": "k", "index": "i"}}
    ), "utf-8")
    api.suggest(ctx, "analista")
    settings.algolia_cache_path.write_text(json.dumps({"fetched_at": time.time(), "data": {"app_id": "evil.com#"}}))
    api.suggest(ctx, "analista")
    settings.algolia_cache_path.write_text("[1, 2")
    api.suggest(ctx, "analista")
    assert site.paths() == ["/", "/", "/"]
    assert {r.url.host for r in algolia.requests} == {"abcde12345-dsn.algolia.net"}


def test_algolia_401_drops_the_cache_and_rediscovers_once(ctx, settings, site, algolia, suggestions, monkeypatch):
    site.add("/", html())
    algolia.add(QUERIES, as_json({"message": "invalid key"}, status=401), as_json({"results": [{"hits": []}]}))
    fresh = AlgoliaConfig("NEWAPP9876", "new-key", "JobOffer_query_suggestions")
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: fresh)
    settings.cache_dir.mkdir(parents=True)
    settings.algolia_cache_path.write_text(json.dumps(
        {"fetched_at": time.time(),
         "data": {"app_id": "ABCDE12345", "api_key": "stale", "index": "JobOffer_query_suggestions"}}
    ), "utf-8")

    api.suggest(ctx, "analista")

    assert site.paths() == ["/"]  # the cached config was used first, then rediscovered once
    assert [r.headers["x-algolia-api-key"] for r in algolia.requests] == ["stale", "new-key"]
    assert algolia.requests[1].url.host == "newapp9876-dsn.algolia.net"
    assert json.loads(settings.algolia_cache_path.read_text("utf-8"))["data"]["app_id"] == "NEWAPP9876"


def test_algolia_env_override_skips_discovery(make_ctx, site, algolia, suggestions, monkeypatch):
    monkeypatch.setenv("LUK_ALGOLIA_APP_ID", "ENVAPP0001")
    monkeypatch.setenv("LUK_ALGOLIA_API_KEY", "env-key")
    monkeypatch.setenv("LUK_ALGOLIA_INDEX", "custom_index")
    algolia.add(QUERIES, as_json({"results": [{"hits": []}]}))
    api.suggest(make_ctx(load_settings()), "analista")
    assert site.requests == []
    assert algolia.requests[0].url.host == "envapp0001-dsn.algolia.net"
    assert json.loads(algolia.requests[0].content)["requests"][0]["indexName"] == "custom_index"


@pytest.mark.parametrize(("status", "error"), [(400, SiteChanged), (404, SiteChanged), (500, NetworkError)])
def test_algolia_error_status_is_classified(ctx, algolia, monkeypatch, status, error):
    """A 4xx other than 401/403 means the request Luk's page describes is no longer accepted (exit 5);
    a 5xx the client did not retry is a network error (exit 4)."""
    monkeypatch.setenv("LUK_ALGOLIA_APP_ID", "ENVAPP0001")
    monkeypatch.setenv("LUK_ALGOLIA_API_KEY", "env-key")
    monkeypatch.setenv("LUK_ALGOLIA_INDEX", "custom_index")
    ctx.settings = load_settings()
    algolia.add(QUERIES, as_json({"message": "bad"}, status=status))
    with pytest.raises(error, match=f"Algolia answered HTTP {status}"):
        api.suggest(ctx, "analista")


@pytest.mark.parametrize(("prefix", "limit"), [("", 5), ("  ", 5), ("a", 0), ("a", 21)])
def test_suggest_validates_before_any_request(ctx, site, algolia, prefix, limit):
    with pytest.raises(InvalidArgument):
        api.suggest(ctx, prefix, limit=limit)
    assert site.requests == [] and algolia.requests == []


# -- areas / similar / related roles ---------------------------------------------------------------

def test_find_areas_for_companies_sends_the_context_and_limits(ctx, site, areas):
    site.add("/flexible_search/areas", as_json({"areas": []}))
    areas.extend([area(i, f"A{i}") for i in range(12)])
    env = api.find_areas(ctx, "santiago", context="companies", limit=10)
    assert params(site.requests[0]) == [("q", "santiago"), ("context", "companies")]
    assert env.kind == "area_list" and env.data.query == "santiago" and env.data.context == "companies"
    assert len(env.data.results) == 10
    assert areas.calls[0][1].startswith("application/json")


def test_find_areas_refuses_unverified_companies_context(ctx, site, monkeypatch):
    monkeypatch.setattr(config, "VERIFIED_PARAMS", config.VERIFIED_PARAMS - {"areas_companies"})
    with pytest.raises(InvalidArgument):
        api.find_areas(ctx, "santiago", context="companies")
    with pytest.raises(InvalidArgument):
        api.find_areas(ctx, "santiago", context="planets")
    assert site.requests == []


def test_similar_roles_request_shape(ctx, site, monkeypatch):
    site.add("/job_titles/similar_roles", as_json({"items": []}))
    seen = {}

    def parse(body, content_type, *, role):
        seen["role"] = role
        return SimilarRoles(role=role, resolved_name="Analista Financiero", items=["Contador"], next_page=2)

    monkeypatch.setattr(parsers, "parse_similar_roles", parse)
    env = api.similar_roles(ctx, " analista financiero ", limit=5)
    assert params(site.requests[0]) == [("role_name", "analista financiero"), ("page", "1"), ("ring", "1"),
                                        ("limit", "5")]
    assert site.requests[0].headers["accept"] == "application/json"
    assert env.kind == "similar_roles" and env.data.resolved_name == "Analista Financiero"
    assert seen["role"] == "analista financiero"
    with pytest.raises(InvalidArgument):
        api.similar_roles(ctx, "x", limit=10)


def test_related_roles_combines_similar_and_five_suggestions(ctx, site, algolia, suggestions, monkeypatch):
    site.add("/job_titles/similar_roles", as_json({"items": []}))
    algolia.add(QUERIES, as_json({"results": [{"hits": []}]}))
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: ALGOLIA)
    site.add("/", html())
    monkeypatch.setattr(parsers, "parse_similar_roles", lambda body, ct, *, role: SimilarRoles(
        role=role, resolved_name="Analista", items=["Contador", "Auditor"]))
    env = api.related_roles(ctx, "analista")
    assert env.kind == "related_roles"
    assert env.data.similar == ["Contador", "Auditor"] and env.data.resolved_name == "Analista"
    assert len(env.data.suggestions) == 5 and json.loads(algolia.requests[0].content)["requests"][0]["hitsPerPage"] == 5
    assert env.warnings == []


def test_related_roles_survives_an_algolia_failure(ctx, site, algolia, monkeypatch):
    site.add("/job_titles/similar_roles", as_json({"items": []}))
    site.add("/", html())
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: ALGOLIA)
    algolia.add(QUERIES, as_json({"message": "no"}, status=403))
    monkeypatch.setattr(parsers, "parse_similar_roles", lambda body, ct, *, role: SimilarRoles(role=role, items=["X"]))
    env = api.related_roles(ctx, "analista")
    assert env.data.suggestions == [] and env.data.similar == ["X"]
    assert len(env.warnings) == 1 and "suggestions unavailable" in env.warnings[0]


# -- companies / company (§5, §5.2) ---------------------------------------------------------------

def test_companies_query_location_and_pagination(ctx, site, areas, monkeypatch):
    site.add("/flexible_search/areas", as_json({"areas": []}))
    site.add("/companies", html())
    areas.append(area(1318, "Santiago", label="Provincia"))
    pages = {1: CompaniesPage(50, [company_card(i) for i in range(24)], Pagination(3, True)),
             2: CompaniesPage(50, [company_card(i) for i in range(24, 48)], Pagination(3, True))}
    calls = []

    def parse(text, *, base_url, capture_path):
        calls.append(capture_path)
        return pages[len(calls)]

    monkeypatch.setattr(parsers, "parse_companies", parse)
    env = api.search_companies(ctx, query="banco", location="santiago", limit=30)

    areas_req, p1, p2 = site.requests
    assert params(areas_req) == [("q", "santiago"), ("context", "companies")]
    assert params(p1) == [("q", "banco"), ("locations", "1318")]
    assert params(p2) == [("q", "banco"), ("locations", "1318"), ("page", "2")]
    assert calls == ["/companies?q=banco&locations=1318", "/companies?q=banco&locations=1318&page=2"]
    data = env.data
    assert env.kind == "company_list" and len(data.results) == 30 and data.total == 50 and data.per_page == 24
    # page 2 was cut after 6 of its 24: the 7th comes next
    assert (data.has_more, data.next_page, data.next_offset, data.last_page) == (True, 2, 6, 3)
    assert env.warnings == ["Ubicación: Santiago, Chile [Provincia] (1318)"]


def test_companies_default_has_no_location_and_limit_bounds(ctx, site, monkeypatch):
    site.add("/companies", html())
    monkeypatch.setattr(parsers, "parse_companies", lambda text, *, base_url, capture_path: CompaniesPage(
        0, [], Pagination(None, False)))
    env = api.search_companies(ctx)
    assert params(site.requests[0]) == [] and env.data.results == [] and env.data.total == 0
    with pytest.raises(InvalidArgument):
        api.search_companies(ctx, limit=151)


def test_company_location_filter_is_refused_when_unverified(ctx, site, monkeypatch):
    monkeypatch.setattr(config, "VERIFIED_PARAMS", config.VERIFIED_PARAMS - {"companies_location"})
    with pytest.raises(InvalidArgument):
        api.search_companies(ctx, location="santiago")
    assert site.requests == []


def test_company_page_request_and_not_found(ctx, site, monkeypatch):
    site.add("/companies/empresa-demo-54", html())
    site.add("/companies/gone", html(status=404))
    seen = []

    def parse(text, *, slug, page, base_url, today, capture_path):
        seen.append((slug, page, capture_path))
        return Company(slug=slug, url=f"{BASE}/companies/{slug}", name="Empresa Demo 54 SpA", page=page, has_more=False)

    monkeypatch.setattr(parsers, "parse_company", parse)
    env = api.get_company(ctx, f"{BASE}/companies/empresa-demo-54", page=2)
    assert str(site.requests[0].url) == f"{BASE}/companies/empresa-demo-54?page=2"
    assert env.kind == "company" and seen == [("empresa-demo-54", 2, "/companies/empresa-demo-54?page=2")]
    with pytest.raises(NotFound):
        api.get_company(ctx, "gone")
    for bad in ("home", "pricing", "https://evil.com/companies/x"):
        with pytest.raises(InvalidArgument):
            api.get_company(ctx, bad)
    assert len(site.requests) == 2


@pytest.mark.parametrize(("location", "landed"), [("/companies/empresa-demo-54?page=2", 2), ("/companies/empresa-demo-54", 1)])
def test_company_page_past_the_last_is_labelled_with_the_page_luk_served(ctx, site, monkeypatch, location, landed):
    """Luk 302s ?page=99 to its last page (live 2026-09-24): the result carries the page it holds, never the
    requested number, plus the §5.1 note naming the last page."""
    site.add("/companies/empresa-demo-54", redirect(location), html())
    seen = []

    def parse(text, *, slug, page, base_url, today, capture_path):
        seen.append((page, capture_path))
        return Company(slug=slug, url=f"{BASE}/companies/{slug}", name="Empresa Demo 54 SpA", page=page, has_more=False)

    monkeypatch.setattr(parsers, "parse_company", parse)
    env = api.get_company(ctx, "empresa-demo-54", page=99)
    assert [str(r.url) for r in site.requests] == [f"{BASE}/companies/empresa-demo-54?page=99", f"{BASE}{location}"]
    assert env.data.page == landed and seen[0][0] == landed
    assert env.warnings == [f"page 99 is past the last page ({landed})"]


def test_company_redirected_off_a_company_page_is_not_found(ctx, site, monkeypatch):
    site.add("/companies/renamed", redirect("/companies/new-name"))
    site.add("/companies/closed", redirect("/companies"))
    site.add("/companies/new-name", html())
    site.add("/companies", html())
    monkeypatch.setattr(parsers, "parse_company", lambda text, *, slug, page, base_url, today, capture_path: Company(
        slug=slug, url=f"{BASE}/companies/{slug}", name="X", page=page, has_more=False))
    assert api.get_company(ctx, "renamed").data.slug == "new-name"
    with pytest.raises(NotFound):
        api.get_company(ctx, "closed")


# -- private commands (§4.5) -----------------------------------------------------------------------

def private_list(items, *, has_next=False, warnings=("parser unverified — run `luk debug capture /saved_jobs`",)):
    return PrivateList(items, Pagination(None, has_next), verified=False, warnings=list(warnings))


@pytest.mark.parametrize("call", [
    lambda c: api.saved_jobs(c), lambda c: api.applications(c), lambda c: api.cvs(c), lambda c: api.whoami(c),
    lambda c: api.session_status(c, check=True),
])
def test_private_commands_without_a_session_send_nothing(ctx, site, call):
    with pytest.raises(AuthRequired):
        call(ctx)
    assert site.requests == []


def test_saved_jobs_sends_the_session_and_pages(ctx, site, saved_session, session_value, monkeypatch):
    saved_session()
    site.add("/saved_jobs", html())
    pages = [private_list([card(1), card(2)], has_next=True), private_list([card(2), card(3)])]
    monkeypatch.setattr(parsers, "parse_saved_jobs", lambda text, *, today, base_url: pages.pop(0))

    env = api.saved_jobs(ctx, limit=10)

    assert [str(r.url) for r in site.requests] == [f"{BASE}/saved_jobs", f"{BASE}/saved_jobs?page=2"]
    assert all(session_value in r.headers["cookie"] for r in site.requests)
    assert env.kind == "job_list" and [c.slug for c in env.data.results] == ["job-1", "job-2", "job-3"]
    assert env.data.total is None and env.data.details is None
    assert env.warnings == ["parser unverified — run `luk debug capture /saved_jobs`"]


def test_saved_details_use_the_anonymous_client(ctx, site, saved_session, jobs, monkeypatch):
    saved_session()
    site.add("/saved_jobs", html())
    site.add("/job_offers/job-1", html())
    site.add("/job_offers/job-2", html(status=410))
    jobs["job-1"] = posting("job-1")
    cards = [card(1), JobCard(slug="Bad Slug!", url=f"{BASE}/x", title="?"), card(2), card(3)]
    monkeypatch.setattr(parsers, "parse_saved_jobs", lambda text, *, today, base_url: private_list(cards, warnings=()))

    env = api.saved_jobs(ctx, details=True, details_limit=3)

    assert site.paths() == ["/saved_jobs", "/job_offers/job-1", "/job_offers/job-2"]
    assert "cookie" in site.requests[0].headers
    assert all("cookie" not in r.headers for r in site.requests[1:])
    assert [p.slug for p in env.data.details] == ["job-1"] and env.data.not_found == ["Bad Slug!", "job-2"]
    assert len(env.data.results) == 4
    with pytest.raises(InvalidArgument):
        api.saved_jobs(ctx, details=True, details_limit=21)


def test_applications_keep_every_row(ctx, site, saved_session, monkeypatch):
    saved_session()
    site.add("/profile/application_histories", html())
    rows = [Application(title="A", status_text="En revisión"), Application(title="A", status_text="Descartado")]
    monkeypatch.setattr(parsers, "parse_applications", lambda text, *, base_url: private_list(rows, warnings=()))
    env = api.applications(ctx)
    assert env.kind == "application_list" and len(env.data.results) == 2
    assert site.paths() == ["/profile/application_histories"]


def test_private_client_follows_login_and_logout_between_calls(ctx, site, saved_session, store, monkeypatch):
    monkeypatch.setattr(parsers, "parse_cvs", lambda text: private_list([Cv(name="cv-1.pdf")], warnings=()))
    site.add("/profile/cvs", html())
    saved_session(value="A" * 40)
    assert api.cvs(ctx).data.results == [Cv(name="cv-1.pdf")]
    saved_session(value="B" * 48)  # a `luk login` from another process rewrote session.json
    api.cvs(ctx)
    assert "A" * 40 in site.requests[0].headers["cookie"] and "B" * 48 in site.requests[1].headers["cookie"]
    store.delete()  # `luk logout`
    with pytest.raises(AuthRequired):
        api.cvs(ctx)
    assert len(site.requests) == 2


def test_own_cookie_write_back_does_not_rebuild_the_client(ctx, site, saved_session, monkeypatch):
    monkeypatch.setattr(parsers, "parse_cvs", lambda text: private_list([], warnings=()))
    rotated = "r" * 50
    site.add("/profile/cvs", httpx.Response(200, text=PAGE, headers={
        "content-type": "text/html", "set-cookie": f"_portal_de_empleos_session={rotated}; path=/; secure; HttpOnly"}))
    saved_session()
    api.cvs(ctx)
    client = ctx.private()
    api.cvs(ctx)
    assert ctx.private() is client
    assert rotated in site.requests[1].headers["cookie"]


def test_whoami_updates_meta(ctx, site, store, make_state, monkeypatch):
    store.save(make_state(), SessionMeta(logged_in_at=datetime(2026, 9, 1, tzinfo=timezone.utc), browser="chromium",
                                         luk_cli_version="0.1.0"))
    site.add("/", html())
    monkeypatch.setattr(parsers, "parse_whoami",
                        lambda text: WhoAmI(logged_in=True, name="Ana Pérez", email="ana@example.com"))
    env = api.whoami(ctx)
    assert env.kind == "whoami" and env.data.name == "Ana Pérez"
    meta = store.load_meta()
    assert (meta.name, meta.email) == ("Ana Pérez", "ana@example.com") and meta.last_auth_ok_at is not None


# -- session / account status ----------------------------------------------------------------------

def test_session_status_is_local_and_never_shows_values(ctx, site, saved_session, session_value):
    assert api.session_status(ctx).data.exists is False
    saved_session()
    env = api.session_status(ctx)
    dumped = json.dumps(env.model_dump(mode="json"))
    assert session_value not in dumped and site.requests == []
    data = env.data
    assert data.exists is True and data.valid is None and data.expiry == "server-side, unknown"
    assert [(c.name, c.domain, c.expires) for c in data.cookies] == [
        ("_portal_de_empleos_session", "www.takealuk.com", "session")]
    assert data.age_seconds is not None and data.age_seconds >= 0


def test_session_status_check_probes_saved_jobs(ctx, site, store, make_state, monkeypatch):
    store.save(make_state(), SessionMeta(logged_in_at=datetime(2026, 9, 1, tzinfo=timezone.utc), browser="chromium",
                                         luk_cli_version="0.1.0"))
    site.add("/saved_jobs", html())
    env = api.session_status(ctx, check=True)
    assert site.paths() == ["/saved_jobs"] and env.data.valid is True
    assert env.data.last_validated_at is not None and env.data.meta.last_validated_at is not None
    site.add("/saved_jobs", redirect(f"{BASE}/users/sign_in"))
    with pytest.raises(AuthRequired):
        api.session_status(ctx, check=True)


@pytest.mark.parametrize("call", [lambda c: api.session_status(c, check=True), lambda c: api.account_status(c, check=True)])
def test_the_check_probe_never_follows_a_redirect(ctx, site, store, make_state, call):
    """§4.3: `--check` is GET /saved_jobs WITHOUT following redirects and §7.2 `check=True` is ONE probe: a
    3xx elsewhere (onboarding) is not valid, and its Location is never requested with the session cookie."""
    store.save(make_state(), SessionMeta(logged_in_at=datetime(2026, 9, 1, tzinfo=timezone.utc), browser="chromium",
                                         luk_cli_version="0.1.0"))
    site.add("/saved_jobs", redirect(f"{BASE}/onboarding"))
    site.add("/onboarding", html())
    try:
        valid = call(ctx).data.valid
    except AuthRequired as err:
        assert err.message == "Luk redirected to /onboarding — finish your profile in the browser, then retry"
        valid = False
    assert valid is False and site.paths() == ["/saved_jobs"]


def test_account_status_reports_and_hides_identity_when_private_is_off(ctx, make_ctx, site, store, make_state,
                                                                       monkeypatch):
    assert api.account_status(ctx).data.session_present is False
    assert api.account_status(ctx, check=True).data.valid is False and site.requests == []
    store.save(make_state(), SessionMeta(name="Ana", email="ana@example.com",
                                         logged_in_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                         browser="chromium", luk_cli_version="0.1.0"))
    site.add("/saved_jobs", redirect("/users/sign_in"))
    env = api.account_status(ctx, check=True)
    data = env.data
    assert env.kind == "account_status"
    assert (data.session_present, data.valid, data.name, data.private_tools_enabled) == (True, False, "Ana", True)
    assert "luk login" in data.login_instructions

    monkeypatch.setenv("LUK_MCP_PRIVATE", "0")
    hidden = api.account_status(make_ctx(load_settings())).data
    assert (hidden.name, hidden.email, hidden.private_tools_enabled) == (None, None, False)


# -- session commands delegate (§4.2–§4.4) ---------------------------------------------------------

def test_login_logout_and_refresh_delegate(ctx, saved_session, monkeypatch):
    calls = []
    result = auth.LoginResult(outcome="success", name="Ana", email=None, browser="msedge")

    def record(name):
        def fake(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return fake

    monkeypatch.setattr(auth, "login", record("login"))
    monkeypatch.setattr(auth, "paste_cookie", record("paste"))
    monkeypatch.setattr(auth, "refresh", record("refresh"))

    def notify(message):
        pass

    assert api.login(ctx, browser="msedge", timeout_s=30, force=True, notify=notify) is result
    api.login(ctx, paste_cookie=True, notify=notify)
    api.refresh_session(ctx, browser="chrome", notify=notify)
    (_, login_args, login_kw), (_, paste_args, paste_kw), (_, _, refresh_kw) = calls
    assert login_args == (ctx.settings, ctx.store, ctx.limiter)
    assert login_kw == {"browser": "msedge", "timeout_s": 30, "force": True, "notify": notify,
                        "transport": ctx.luk_transport}
    assert paste_kw == {"notify": notify, "transport": ctx.luk_transport}
    assert refresh_kw == {"browser": "chrome", "notify": notify}  # refresh sends no httpx request

    saved_session()
    assert api.logout(ctx) is True and api.logout(ctx) is False


# -- open / capture / doctor / schemas --------------------------------------------------------------

def test_open_passes_only_rebuilt_urls(ctx):
    opened = []
    urls = api.open_targets(ctx, ["analista-x", "https://luk.cl/job_offers/y?ref=1"], opener=opened.append)
    assert urls == opened == [f"{BASE}/job_offers/analista-x", f"{BASE}/job_offers/y"]
    assert api.open_targets(ctx, ["empresa-demo-54"], company=True, opener=opened.append) == [f"{BASE}/companies/empresa-demo-54"]
    for bad in (["C:\\x.bat"], ["ok", "javascript:alert(1)"], [], ["a"] * 6):
        with pytest.raises(InvalidArgument):
            api.open_targets(ctx, bad, opener=opened.append)
    assert len(opened) == 3


def test_open_reports_urls_no_browser_opened(ctx):
    """webbrowser.open returns False when no browser starts (no DISPLAY over SSH, a container, an empty
    registry): that is an error naming the URLs to open by hand, never a success."""
    results = {f"{BASE}/job_offers/a": True, f"{BASE}/job_offers/b": False, f"{BASE}/job_offers/c": False}
    tried = []

    def opener(url):
        tried.append(url)
        return results[url]

    with pytest.raises(InvalidArgument) as info:
        api.open_targets(ctx, ["a", "b", "c"], opener=opener)
    assert tried == list(results) and info.value.exit_code == 1
    assert info.value.message == (f"could not open a browser — open these URLs yourself: {BASE}/job_offers/b "
                                  f"{BASE}/job_offers/c")


@pytest.mark.parametrize("path", ["https://www.takealuk.com/", "/users/sign_out", "/job_offers/x/save_later",
                                  "/profile", "/job_offers/x/apply"])
def test_capture_refuses_paths_outside_the_allow_list(ctx, site, path):
    with pytest.raises(InvalidArgument):
        api.debug_capture(ctx, path)
    assert site.requests == []


def test_capture_private_page_scrubs_with_the_identity_and_writes_meta(ctx, site, store, make_state, settings,
                                                                      session_value, monkeypatch):
    store.save(make_state(), SessionMeta(name="Ana Pérez", logged_in_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                         browser="chromium", luk_cli_version="0.1.0"))
    site.add("/saved_jobs", html("<html lang='es-CL'>RAW Ana Pérez ana@example.com</html>"))
    monkeypatch.setattr(parsers, "parse_whoami", lambda text: WhoAmI(logged_in=True, name="Ana P.", email="ana@example.com"))
    seen = {}

    def fake_scrub(text, *, path, identity):
        seen.update(path=path, identity=identity)
        return "<html>SCRUBBED</html>"

    monkeypatch.setattr(scrub, "scrub", fake_scrub)

    written = api.debug_capture(ctx, "/saved_jobs?page=2")

    assert session_value in site.requests[0].headers["cookie"] and str(site.requests[0].url).endswith("?page=2")
    assert seen == {"path": "/saved_jobs", "identity": scrub.Identity(name="Ana Pérez", email="ana@example.com")}
    assert written.parent == settings.captures_dir and written.suffix == ".html"
    assert written.read_text("utf-8") == "<html>SCRUBBED</html>"
    meta = json.loads(written.with_name(written.name.replace(".html", ".meta.json")).read_text("utf-8"))
    assert meta["path"] == "/saved_jobs?page=2" and meta["final_url"] == f"{BASE}/saved_jobs?page=2"
    assert meta["status"] == 200 and meta["luk_cli_version"] == "0.1.0"
    assert meta["sha256"] == hashlib.sha256(b"<html>SCRUBBED</html>").hexdigest()
    assert datetime.fromisoformat(meta["captured_at"]).tzinfo is not None
    assert sorted(p.suffix for p in settings.captures_dir.iterdir()) == [".html", ".json"]


def test_capture_public_page_is_anonymous_with_no_identity(ctx, site, saved_session, tmp_path, monkeypatch):
    saved_session()
    site.add("/job_offers/analista-x", html())
    seen = {}

    def fake_scrub(text, *, path, identity):
        seen["identity"] = identity
        return "x"

    monkeypatch.setattr(scrub, "scrub", fake_scrub)
    written = api.debug_capture(ctx, "/job_offers/analista-x", out_dir=tmp_path / "out")
    assert "cookie" not in site.requests[0].headers
    assert seen["identity"] == scrub.Identity(name=None, email=None)
    assert written.parent == tmp_path / "out"


@pytest.mark.parametrize("out", [None, "out"])
def test_capture_fails_closed_and_writes_nothing(ctx, site, store, make_state, settings, tmp_path, out):
    """§8.1 fail closed: identity text the scrubber cannot remove (here an attribute name) → ScrubFailed,
    exit 1 naming the location, and no .html, .meta.json or temp file anywhere (the real scrubber)."""
    store.save(make_state(), SessionMeta(name="Paz Íñiguez Sentinela", logged_in_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                         browser="chromium", luk_cli_version="0.1.0"))
    site.add("/saved_jobs", html("<html lang='es-CL'><body><div data-sentinela-flag='1'>hola</div></body></html>"))
    out_dir = tmp_path / out if out else None
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    with pytest.raises(ScrubFailed) as info:
        api.debug_capture(ctx, "/saved_jobs", out_dir=out_dir)

    assert info.value.exit_code == 1 and "markup outside text and attribute values" in info.value.message
    assert len(site.requests) == 1
    written = {p for p in tmp_path.rglob("*") if p.is_file()} - before
    assert [p for p in written if p.name.endswith((".html", ".meta.json", ".tmp"))] == []
    assert not (out_dir or settings.captures_dir).exists()


def test_doctor_offline_reports_without_importing_playwright(ctx, saved_session, session_value):
    saved_session()
    before = "playwright" in sys.modules
    checks = api.doctor(ctx)
    assert ("playwright" in sys.modules) == before
    names = [c.name for c in checks]
    for expected in ("python", "luk-cli", "httpx", "playwright", "mcp extra", "luk on PATH", "chromium",
                     "msedge", "config dir", "cache dir", "session"):
        assert expected in names
    assert sys.executable in checks[names.index("python")].detail
    assert session_value not in json.dumps([c.detail for c in checks])


def test_doctor_live_runs_one_parse_per_public_page_and_reports_failures(ctx, site, monkeypatch):
    site.add("/", html())
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: ALGOLIA)

    def ok(kind, data):
        return lambda *a, **k: Envelope(kind=kind, data=data)

    monkeypatch.setattr(api, "search_jobs", ok("job_search", api.JobSearch(
        total=3, page=1, last_fetched_page=1, has_more=True, results=[card(1)], query=SearchQuery())))
    monkeypatch.setattr(api, "get_job", ok("job", posting("job-1")))
    monkeypatch.setattr(api, "_areas", lambda *a, **k: [area(1318, "Santiago")])
    monkeypatch.setattr(api, "similar_roles", ok("similar_roles", SimilarRoles(role="analista")))
    def moved(*args, **kwargs):
        raise SiteChanged("companies moved")

    monkeypatch.setattr(api, "search_companies", moved)
    monkeypatch.setattr(api, "suggest", ok("suggestion_list", api.SuggestionList(query="analista")))

    checks = {c.name: c for c in api.doctor(ctx, live=True)}

    assert checks["live: algolia discovery"].ok is True and "ABCDE12345" in checks["live: algolia discovery"].detail
    assert "public-search-key" not in checks["live: algolia discovery"].detail
    assert checks["live: search"].ok is True and checks["live: job detail"].ok is True
    assert checks["live: companies"].ok is False and checks["live: companies"].detail == "companies moved"
    assert checks["live: company"].ok is None  # skipped: no company slug to parse
    assert checks["live: suggest"].ok is True


@pytest.mark.parametrize("stop", [Blocked("Blocked by Luk (HTTP 403)"), RateLimited(), BudgetExceeded(),
                                  AlgoliaRejected("Algolia rejected the key (HTTP 403)")])
def test_doctor_live_stops_at_the_first_block_or_rate_limit(ctx, site, monkeypatch, stop):
    # ADR-0001: a 403/429/challenge means STOP — no further request, not even another check.
    site.add("/", html())
    monkeypatch.setattr(parsers, "parse_algolia_config", lambda text: ALGOLIA)

    def blocked(*args, **kwargs):
        raise stop

    def must_not_run(*args, **kwargs):
        raise AssertionError("a live check ran after Luk blocked or rate-limited us")

    monkeypatch.setattr(api, "search_jobs", blocked)
    for name in ("get_job", "_areas", "similar_roles", "search_companies", "get_company", "suggest"):
        monkeypatch.setattr(api, name, must_not_run)

    checks = {c.name: c for c in api.doctor(ctx, live=True)}

    assert checks["live: algolia discovery"].ok is True
    assert checks["live: search"].ok is False and checks["live: search"].detail == stop.message
    later = ["live: job detail", "live: areas", "live: similar roles", "live: companies", "live: company",
             "live: suggest"]
    for name in later:
        assert checks[name].ok is None and checks[name].detail.startswith("not run"), name


def test_mcp_config_uses_this_interpreter():
    assert api.mcp_config() == {"mcpServers": {"luk": {"type": "stdio", "command": sys.executable,
                                                       "args": ["-m", "luk_cli", "mcp"]}}}


def test_schemas_cover_every_kind_with_every_key_required():
    schemas = api.schemas()
    assert set(schemas) == {*api.KIND_MODELS, "error"}
    for kind, schema in schemas.items():
        assert set(schema["required"]) == {"schema_version", "kind", "data" if kind != "error" else "error"} | (
            {"warnings"} if kind != "error" else set())
    assert schemas["error"] == ErrorEnvelope.model_json_schema(mode="serialization")


def test_from_env_builds_a_context_from_settings(luk_dirs):
    ctx = ApiContext.from_env(verbose=True)
    try:
        assert ctx.verbose is True and ctx.settings.cache_dir == luk_dirs[1]
        assert ctx.store.path == luk_dirs[0] / "session.json"
        assert ctx.anonymous() is ctx.anonymous() and ctx.anonymous().is_private is False
    finally:
        ctx.close()
