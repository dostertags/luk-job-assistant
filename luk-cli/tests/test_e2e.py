"""End to end, offline: `luk_cli.api` (and `luk` via `cli.main`) over httpx.MockTransport serving the
public fixtures (the real page structure, fake content) through the REAL http layer and parsers (spec §5, §5.1, §5.2, §5.5, §8.2).

Private pages use the synthetic fixtures (§8.1), so those tests carry `@pytest.mark.synthetic`.
Every envelope is re-validated against its `KIND_MODELS` model through its own JSON, which is what
`--json` and the MCP tools print.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from luk_cli import api, cli, mcp_server, parsers
from luk_cli.api import ApiContext
from luk_cli.errors import AuthRequired, Blocked, NotFound
from luk_cli.models import KIND_MODELS, Envelope, ErrorEnvelope
from luk_cli.ratelimit import ALGOLIA_LIMITER

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.takealuk.com"
TODAY = date(2026, 9, 23)  # the capture date of the public fixtures
DETAIL_SLUG = "analista-demo-01-empresa-demo-01"
GONE_SLUG = "zzzz-no-existe-12345"
ALGOLIA_HOST = "testappid0-dsn.algolia.net"  # the (fake) app id in public/root.html, lower-cased
Handler = Callable[[httpx.Request], httpx.Response]


def page(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / name).read_bytes(),
                          headers={"content-type": "text/html; charset=utf-8"})


def json_file(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / name).read_bytes(),
                          headers={"content-type": "application/json; charset=utf-8"})


def redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location})


def search_page(request: httpx.Request) -> httpx.Response:
    """The Phase-0 capture that matches the query (docs/endpoints.md §4)."""
    params = request.url.params
    if params.get("job_positions") == "zzqxwvkj":
        return page("public/search_zero.html")
    if params.get("worldwide") == "1":
        return page("public/search_worldwide.html")
    if "job_types[]" in params:
        return page("public/search_single_page.html")
    return page({"2": "public/search_p2.html", "19": "public/search_last.html",
                 "20": "public/search_past_last.html"}.get(params.get("page", "1"), "public/search_analista.html"))


def job_page(request: httpx.Request) -> httpx.Response:
    slug = request.url.path.removeprefix("/job_offers/")
    return page("public/detail.html") if slug == DETAIL_SLUG else page("public/detail_410.html", 410)


def companies_page(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    if "q" in params:
        return page("public/companies_q.html")
    return page("public/companies_location.html" if "locations" in params else "public/companies.html")


def company_page(request: httpx.Request) -> httpx.Response:
    slug = request.url.path.removeprefix("/companies/")
    if slug == "empresa-demo-54":
        return page("public/company_paginated_p2.html" if request.url.params.get("page") == "2"
                    else "public/company_paginated.html")
    return page("public/company.html") if slug == "empresa-demo-86" else page("public/detail_410.html", 404)


def areas_page(request: httpx.Request) -> httpx.Response:
    companies = request.url.params.get("context") == "companies"
    return json_file("public/areas_companies.json" if companies else "public/areas.json")


class Site:
    """takealuk.com as the fixtures show it; `routes[path]` overrides one path. Records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Handler] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.routes:
            return self.routes[path](request)
        if path == "/":
            return page("public/root.html")
        if path == "/job_offers":
            return search_page(request)
        if path.startswith("/job_offers/"):
            return job_page(request)
        if path == "/companies":
            return companies_page(request)
        if path.startswith("/companies/"):
            return company_page(request)
        if path == "/flexible_search/areas":
            return areas_page(request)
        if path == "/job_titles/similar_roles":
            return json_file("public/similar.json")
        if path in ("/saved_jobs", "/profile/application_histories", "/profile/cvs"):
            return redirect(f"{BASE}/users/sign_in")  # anonymous behaviour, recon 2026-09-23
        return page("synthetic/not_found_404.html", 404)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture(autouse=True)
def _no_algolia_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ALGOLIA_LIMITER, "min_interval_s", 0.0)


@pytest.fixture
def site() -> Site:
    return Site()


@pytest.fixture
def algolia() -> list[httpx.Request]:
    return []


@pytest.fixture
def make_ctx(settings, store, limiter, site, algolia):
    made: list[ApiContext] = []

    def algolia_handler(request: httpx.Request) -> httpx.Response:
        algolia.append(request)
        return json_file("synthetic/algolia_suggestions.json")

    def make(today: date = TODAY) -> ApiContext:
        ctx = ApiContext(settings, store, limiter, luk_transport=httpx.MockTransport(site),
                         algolia_transport=httpx.MockTransport(algolia_handler), today=lambda: today)
        made.append(ctx)
        return ctx

    yield make
    for ctx in made:
        ctx.close()


@pytest.fixture
def ctx(make_ctx) -> ApiContext:
    return make_ctx()


def valid(envelope: Envelope[Any] | dict[str, Any]) -> Any:
    """Re-validate a `--json` document (or the one an envelope prints) against its kind's model."""
    document = envelope if isinstance(envelope, dict) else json.loads(
        json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False))
    assert document["schema_version"] == 2 and set(document) == {"schema_version", "kind", "data", "warnings"}
    return KIND_MODELS[document["kind"]].model_validate(document["data"])


def wire(request: httpx.Request) -> list[tuple[str, str]]:
    return request.url.params.multi_items()


# -- search (§5.1) ------------------------------------------------------------------------------------------

def test_search_default_sends_chile_and_parses_the_real_page(ctx, site):
    data = valid(api.search_jobs(ctx, roles=["analista"]))

    assert [wire(r) for r in site.requests] == [[("job_positions", "analista"), ("locations", "1021")]]
    assert (data.total, len(data.results), data.per_page, data.last_page) == (273, 15, 15, 19)
    assert (data.has_more, data.next_page, data.page, data.last_fetched_page) == (True, 2, 1, 1)
    assert data.effective_location.id == 1021 and data.effective_location.area_type_label == "País"
    assert len(data.related_roles) == 3 and data.query.location_ids == [1021]
    assert all(card.slug and card.title and card.url == f"{BASE}/job_offers/{card.slug}" for card in data.results)
    first = data.results[0]
    assert first.slug == DETAIL_SLUG and first.salary.model_dump() == {
        "raw": "CLP $650.000 - $850.000", "currency": "CLP", "min": 650000, "max": 850000, "period": None}
    assert sum(card.salary is not None for card in data.results) == 2  # §8.1 trap: salary on 2/15 cards
    assert first.posted_at_approx is not None and first.posted_at_approx <= TODAY


def test_search_limit_pages_on_and_dedupes(ctx, site):
    data = valid(api.search_jobs(ctx, roles=["analista"], limit=20))

    assert [r.url.params.get("page") for r in site.requests] == [None, "2"]
    assert len(data.results) == 20 == len({card.slug for card in data.results})
    # page 2 was cut after its first 5 offers: the 6th comes next
    assert (data.page, data.last_fetched_page, data.has_more, data.next_page, data.next_offset) == (1, 2, True, 2, 5)


def test_following_the_cursor_after_a_small_limit_skips_no_offer(ctx):
    """max_results=3 (the §9 acceptance call) cuts page 1: the cursor is (page 1, offset 3)."""
    whole_page = [card.slug for card in valid(api.search_jobs(ctx, roles=["analista"])).results]
    cut = api.search_jobs(ctx, roles=["analista"], limit=3)
    assert [card.slug for card in cut.data.results] == whole_page[:3]
    assert (cut.data.has_more, cut.data.next_page, cut.data.next_offset) == (True, 1, 3) and cut.warnings == []
    more = valid(api.search_jobs(ctx, roles=["analista"], page=1, offset=3, limit=3))
    assert [card.slug for card in more.results] == whole_page[3:6] and (more.next_page, more.next_offset) == (1, 6)

    single = valid(api.search_jobs(ctx, roles=["analista"], job_types=["intern"], limit=1))  # 2 offers, 1 page
    assert (len(single.results), single.has_more, single.next_page, single.next_offset) == (1, True, 1, 1)
    rest = valid(api.search_jobs(ctx, roles=["analista"], job_types=["intern"], offset=1, limit=1))
    assert (len(rest.results), rest.has_more, rest.next_page, rest.next_offset) == (1, False, None, None)
    assert rest.results[0].slug != single.results[0].slug


WALK_PAGES = {1: "public/search_analista.html", 2: "public/search_p2.html", 3: "public/search_last.html"}


def walk_pages(site: Site) -> list[str]:
    """Serve pages 1-3 of the 'analista' search fixtures (15 + 15 + 4 offers, the last with no next link); the
    whole list, in order, as the parser reads it straight from the fixtures."""
    site.routes["/job_offers"] = lambda request: page(WALK_PAGES[int(request.url.params.get("page", "1"))])
    return [card.slug for number, name in WALK_PAGES.items() for card in parsers.parse_search(
        (FIXTURES / name).read_text("utf-8"), page=number, today=TODAY, base_url=BASE, capture_path="/job_offers",
    ).cards]


def test_mcp_search_jobs_continuation_never_skips_or_repeats(settings, store, no_limit, site):
    """The §7.2 continuation through the real MCP server, api, http layer and parsers: for every max_results
    1-31 (2 × 15 + 1), calling search_jobs again with page=next_page and offset=next_offset returns the 34
    offers of pages 1-3 exactly once, in order."""
    pytest.importorskip("mcp.server.fastmcp")
    whole = walk_pages(site)
    assert len(whole) == len(set(whole)) == 34
    server = mcp_server.build_server(lambda: ApiContext(
        settings, store, no_limit, luk_transport=httpx.MockTransport(site), today=lambda: TODAY))
    for limit in range(1, 2 * 15 + 2):
        got: list[str] = []
        cursor: dict[str, int] = {}
        for _ in range(len(whole) + 1):
            (block,) = anyio.run(server.call_tool, "search_jobs", {"roles": ["analista"], "max_results": limit,
                                                                   **cursor})
            data = valid(json.loads(block.text))
            got += [card.slug for card in data.results]
            if not data.has_more:
                break
            cursor = {"page": data.next_page, "offset": data.next_offset}
        assert got == whole, f"max_results={limit}: the continuation skipped or repeated an offer"


def test_cli_offset_continues_where_the_limit_stopped(luk, site):
    whole = walk_pages(site)
    code, out, _ = luk("search", "analista", "--limit", "20", "--json")
    first = valid(json.loads(out))
    assert code == 0 and (first.next_page, first.next_offset) == (2, 5)
    code, out, _ = luk("search", "analista", "--page", "2", "--offset", "5", "--limit", "20", "--json")
    rest = valid(json.loads(out))
    assert code == 0 and [c.slug for c in [*first.results, *rest.results]] == whole and not rest.has_more


def test_search_location_resolves_through_areas_and_caches_it(make_ctx, site):
    envelope = api.search_jobs(make_ctx(), roles=["analista"], locations=["Santiago"], limit=5)
    data = valid(envelope)

    areas, search = site.requests
    assert areas.url.path == "/flexible_search/areas" and wire(areas) == [("q", "Santiago")]
    assert areas.headers["accept"] == "application/json"
    assert wire(search) == [("job_positions", "analista"), ("locations", "1318")]
    assert data.query.location_ids == [1318] and len(data.results) == 5 and data.has_more
    assert [a.id for a in data.location_alternatives] == [1348, 17863, 9579]
    assert envelope.warnings[0] == "Ubicación: Santiago, Región Metropolitana, Chile [Provincia] (1318)"

    api.search_jobs(make_ctx(), roles=["analista"], locations=["Santiago"])  # a new context: the 24 h areas/ cache
    assert [r.url.path for r in site.requests[2:]] == ["/job_offers"]


def test_search_unknown_area_is_not_found_with_no_search(ctx, site):
    site.routes["/flexible_search/areas"] = lambda r: httpx.Response(200, json={"areas": []})
    with pytest.raises(NotFound, match="No Luk area matches 'Atlantis'"):
        api.search_jobs(ctx, roles=["analista"], locations=["Atlantis"])
    assert [r.url.path for r in site.requests] == ["/flexible_search/areas"]


def test_search_filters_go_on_the_wire_exactly(ctx, site):
    data = valid(api.search_jobs(ctx, roles=["analista"], job_types=["part_time", "intern"], posted_within="1w",
                                 min_salary=1_000_000, limit=3))

    assert wire(site.requests[0]) == [
        ("job_positions", "analista"), ("locations", "1021"), ("job_types[]", "part_time"),
        ("job_types[]", "intern"), ("date", "last_week"), ("min_salary", "1000000"), ("salary_currency", "CLP"),
    ]
    assert (data.total, len(data.results), data.last_page, data.has_more, data.per_page) == (2, 2, 1, False, None)
    assert data.results[0].employment_type == "intern" and data.results[0].modality == "on_site"
    assert data.results[1].labels == ["30 horas"] and data.results[1].employment_type is None
    assert data.query.filters.currency == "CLP"


def test_worldwide_and_countries_never_send_locations(ctx, site):
    world = valid(api.search_jobs(ctx, roles=["analista"], worldwide=True, limit=3))
    colombia = valid(api.search_jobs(ctx, roles=["analista"], countries=["CO"], limit=3))

    assert wire(site.requests[0]) == [("job_positions", "analista"), ("worldwide", "1")]
    assert wire(site.requests[1]) == [("job_positions", "analista"), ("worldwide", "1"), ("countries[]", "Colombia")]
    assert (world.total, world.effective_location, world.query.worldwide) == (735, None, True)
    assert colombia.query.countries == ["CO"] and colombia.query.location_ids == []


def test_zero_results_is_an_empty_success(ctx):
    data = valid(api.search_jobs(ctx, roles=["zzqxwvkj"]))
    assert (data.total, data.results, data.related_roles, data.has_more) == (0, [], [], False)


def test_last_and_past_last_pages(ctx):
    last = valid(api.search_jobs(ctx, roles=["analista"], page=19))
    past = api.search_jobs(ctx, roles=["analista"], page=20)

    assert (len(last.results), last.last_page, last.has_more, last.next_page) == (4, 19, False, None)
    assert valid(past).results == [] and past.warnings == ["page 20 is past the last page (19)"]


# -- show / get_jobs (§5.2) ------------------------------------------------------------------------------------

def test_show_merges_json_ld_and_dom(ctx, site):
    job = valid(api.get_job(ctx, f"{BASE}/job_offers/{DETAIL_SLUG}?utm_source=x#top"))

    assert [r.url.path for r in site.requests] == [f"/job_offers/{DETAIL_SLUG}"] and not site.requests[0].url.query
    assert (job.offer_id, job.company, job.company_slug) == (10001, "Empresa Demo 01 SpA", "empresa-demo-01")
    assert job.company_url == f"{BASE}/companies/empresa-demo-01"  # never /companies/home (§8.1 trap)
    assert (job.employment_type, job.modality, job.vacancies, job.status) == ("full_time", "on_site", 1, "open")
    assert job.location == "Las Condes, Santiago, Región Metropolitana, Chile"
    assert job.address.model_dump() == {"locality": "Santiago", "region": "Región Metropolitana", "country": "CL"}
    assert job.salary.period == "month" and job.salary.min == 650000
    assert not job.description_text.startswith("Cargo:") and "\n- " in job.description_text
    assert job.requirements_text.startswith("- ") and job.canonical_slug is None and not job.text_truncated


def test_show_after_valid_through_is_expired(make_ctx):
    job = valid(api.get_job(make_ctx(today=date(2027, 6, 1)), DETAIL_SLUG))
    assert job.status == "expired" and job.valid_through < date(2027, 6, 1)


def test_show_follows_a_redirect_to_another_offer(ctx, site):
    site.routes["/job_offers/old-slug"] = lambda r: redirect(f"/job_offers/{DETAIL_SLUG}")
    job = valid(api.get_job(ctx, "old-slug"))
    assert (job.slug, job.canonical_slug) == ("old-slug", DETAIL_SLUG)
    assert [r.url.path for r in site.requests] == ["/job_offers/old-slug", f"/job_offers/{DETAIL_SLUG}"]


def test_show_unknown_slug_is_not_found(ctx):
    with pytest.raises(NotFound, match="HTTP 410"):
        api.get_job(ctx, GONE_SLUG)


def test_get_jobs_puts_gone_offers_in_not_found(ctx):
    data = valid(api.get_jobs(ctx, [DETAIL_SLUG, GONE_SLUG]))
    assert [job.slug for job in data.results] == [DETAIL_SLUG] and data.not_found == [GONE_SLUG]


# -- areas, roles, suggestions ---------------------------------------------------------------------------------

def test_areas_for_jobs_and_companies(ctx, site):
    jobs = valid(api.find_areas(ctx, "santiago"))
    companies = valid(api.find_areas(ctx, "santiago", context="companies"))

    assert [a.id for a in jobs.results][:3] == [1318, 1348, 17863]
    assert jobs.results[0].offer_count == 601 and companies.results[0].offer_count == 284
    assert wire(site.requests[1]) == [("q", "santiago"), ("context", "companies")]


def test_similar_roles_request_and_resolved_name(ctx, site):
    data = valid(api.similar_roles(ctx, "analista financiero"))

    assert wire(site.requests[0]) == [
        ("role_name", "analista financiero"), ("page", "1"), ("ring", "1"), ("limit", "9")]
    assert data.resolved_name == "Analista Financiero" and len(data.items) == 9 and data.next_page == 2


@pytest.mark.synthetic
def test_related_roles_discovers_algolia_from_the_real_root_page(ctx, site, algolia):
    data = valid(api.related_roles(ctx, "analista financiero"))
    again = valid(api.suggest(ctx, "analista fin"))

    assert site.calls("/") == site.requests[1:2]  # one discovery, then the 24 h cache
    assert data.resolved_name == "Analista Financiero" and len(data.similar) == 9
    assert [s.query for s in data.suggestions] == ["analista financiero", "analista de finanzas",
                                                   "analista financiero senior"]
    assert again.results == data.suggestions
    request = algolia[0]
    assert (request.method, request.url.host, request.url.path) == ("POST", ALGOLIA_HOST, "/1/indexes/*/queries")
    assert request.headers["x-algolia-application-id"] == "TESTAPPID0"
    assert not {"cookie", "origin", "referer"} & set(request.headers)
    body = json.loads(request.content)["requests"][0]
    assert body == {"indexName": "JobOffer_query_suggestions", "query": "analista financiero", "hitsPerPage": 5,
                    "attributesToRetrieve": ["query", "popularity"]}


# -- companies (§2.1) --------------------------------------------------------------------------------------------

def test_companies_ignores_skeletons(ctx):
    data = valid(api.search_companies(ctx))
    assert (data.total, len(data.results), data.per_page, data.last_page, data.next_page) == (2138, 24, 24, 90, 2)


def test_companies_query_and_tags(ctx, site):
    data = valid(api.search_companies(ctx, query="banco"))

    assert wire(site.requests[0]) == [("q", "banco")]
    assert (data.total, len(data.results), data.has_more) == (11, 11, False)
    tagged = [c for c in data.results if c.sector or c.size]
    assert [(c.sector, c.size) for c in tagged] == [("Finanzas", "51-200 empleados")]
    assert sum(c.location is None for c in data.results) == 1


def test_companies_location_resolves_with_the_companies_context(ctx, site):
    data = valid(api.search_companies(ctx, location="Santiago", limit=3))

    assert wire(site.requests[0]) == [("q", "Santiago"), ("context", "companies")]
    assert wire(site.requests[1]) == [("locations", "1318")]
    assert (data.total, len(data.results), data.last_page) == (226, 3, 10)


def test_company_page_and_its_pagination(ctx, site):
    small = valid(api.get_company(ctx, "empresa-demo-86"))
    first = valid(api.get_company(ctx, f"{BASE}/companies/empresa-demo-54"))
    second = valid(api.get_company(ctx, "empresa-demo-54", page=2))

    assert (small.name, small.active_offers, len(small.jobs), small.has_more) == ("Empresa Demo 86 SpA", 2, 2, False)
    assert small.location == "Santa Cruz, Colchagua, O'Higgins, Chile"  # not the breadcrumb "›"
    assert (len(first.jobs), first.has_more, first.next_page, first.active_offers) == (20, True, 2, 29)
    assert (len(second.jobs), second.page, second.has_more) == (9, 2, False)
    assert wire(site.requests[-1]) == [("page", "2")]


def test_company_page_past_the_last_reports_the_page_luk_served(luk, site):
    """Live 2026-09-24: `/companies/empresa-demo-54?page=99` → 302 → `?page=2`. The 9 jobs are page 2's, so the
    document says page 2 (not 99) and stderr names the last page, as `search --page` past the end does."""
    def paginated(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "99":
            return redirect("/companies/empresa-demo-54?page=2")
        return company_page(request)

    site.routes["/companies/empresa-demo-54"] = paginated
    code, out, err = luk("company", "empresa-demo-54", "--page", "99", "--json")

    assert code == 0 and [wire(r) for r in site.requests] == [[("page", "99")], [("page", "2")]]
    document = json.loads(out)
    data = valid(document)
    assert (data.page, len(data.jobs), data.has_more, data.next_page) == (2, 9, False, None)
    assert document["warnings"] == ["page 99 is past the last page (2)"] and err == "page 99 is past the last page (2)\n"


def test_unknown_company_is_not_found(ctx):
    with pytest.raises(NotFound):
        api.get_company(ctx, "no-such-company")


# -- blocks (§4.9) -------------------------------------------------------------------------------------------------

@pytest.mark.synthetic
def test_a_challenge_page_blocks_after_one_request(ctx, site):
    site.routes["/job_offers"] = lambda r: page("synthetic/challenge_403.html", 403)
    with pytest.raises(Blocked):
        api.search_jobs(ctx, roles=["analista"])
    assert len(site.requests) == 1


# -- private pages (§4.5, synthetic fixtures) --------------------------------------------------------------------

@pytest.fixture
def logged_in(site, saved_session, session_value):
    """A saved session, and a site that serves the synthetic logged-in pages to it."""
    saved_session()
    cookie = f"_portal_de_empleos_session={session_value}"

    def private(name: str) -> Handler:
        def serve(request: httpx.Request) -> httpx.Response:
            if request.headers.get("cookie") != cookie:
                return redirect(f"{BASE}/users/sign_in")
            return page(name)
        return serve

    site.routes.update({
        "/saved_jobs": private("synthetic/saved_jobs.html"),
        "/profile/application_histories": private("synthetic/application_histories.html"),
        "/profile/cvs": private("synthetic/cvs.html"),
    })
    return cookie


@pytest.mark.synthetic
def test_private_pages_with_the_session(ctx, site, logged_in):
    saved = api.saved_jobs(ctx, limit=3)
    applications = api.applications(ctx)
    cvs = api.cvs(ctx)

    assert [r.headers["cookie"] for r in site.requests] == [logged_in] * 3
    assert [c.slug for c in valid(saved).results] == [
        "analista-de-datos-empresa-ficticia", "ejecutivo-comercial-part-time", "practica-contabilidad-otra-empresa"]
    assert saved.warnings == ["parser unverified — run `luk debug capture /saved_jobs`"]
    assert len(valid(applications).results) == 3 and all(a.status_text for a in applications.data.results)
    assert len(valid(cvs).results) == 2


@pytest.mark.synthetic
def test_saved_details_use_the_anonymous_client(ctx, site, logged_in):
    site.routes["/job_offers/analista-de-datos-empresa-ficticia"] = lambda r: page("public/detail.html")
    data = valid(api.saved_jobs(ctx, limit=3, details=True))

    details = [r for r in site.requests if r.url.path.startswith("/job_offers/")]
    assert len(details) == 3 and not any("cookie" in r.headers for r in details)
    assert [d.slug for d in data.details] == ["analista-de-datos-empresa-ficticia"]
    assert data.not_found == ["ejecutivo-comercial-part-time", "practica-contabilidad-otra-empresa"]


@pytest.mark.synthetic
def test_whoami_reads_the_logged_in_header(ctx, site, logged_in):
    site.routes["/"] = lambda r: page("synthetic/root_logged_in.html" if "cookie" in r.headers else "public/root.html")
    me = valid(api.whoami(ctx))
    assert (me.logged_in, me.name, me.email) == (True, "Paz Prueba", "paz.prueba@example.com")


def capture_meta(written: Path) -> dict[str, Any]:
    return json.loads(written.with_name(f"{written.stem}.meta.json").read_text("utf-8"))


def test_debug_capture_of_a_public_page_keeps_it_parseable(ctx, site, tmp_path):
    written = api.debug_capture(ctx, f"/job_offers/{DETAIL_SLUG}", out_dir=tmp_path)
    meta = capture_meta(written)

    assert "cookie" not in site.requests[0].headers
    assert set(meta) == {"path", "final_url", "status", "captured_at", "luk_cli_version", "sha256"}
    assert (meta["path"], meta["status"]) == (f"/job_offers/{DETAIL_SLUG}", 200)
    assert meta["sha256"] == hashlib.sha256(written.read_bytes()).hexdigest()
    parsed = parsers.parse_job(written.read_text("utf-8"), slug=DETAIL_SLUG, base_url=BASE, today=TODAY,
                               capture_path=meta["path"])
    assert parsed.value.offer_id == 10001


@pytest.mark.synthetic
def test_debug_capture_of_a_private_page_scrubs_the_identity(ctx, site, logged_in, tmp_path):
    written = api.debug_capture(ctx, "/saved_jobs", out_dir=tmp_path)
    text = written.read_text("utf-8")

    assert site.requests[0].headers["cookie"] == logged_in
    assert "paz prueba" not in text.casefold() and "paz.prueba@example.com" not in text.casefold()
    assert len(parsers.parse_saved_jobs(text, today=TODAY, base_url=BASE).items) == 3


def test_public_commands_never_send_the_session(ctx, site, saved_session):
    saved_session()
    api.search_jobs(ctx, roles=["analista"])
    api.get_job(ctx, DETAIL_SLUG)
    api.get_company(ctx, "empresa-demo-86")
    assert site.requests and not any("cookie" in r.headers for r in site.requests)


def test_private_without_a_session_sends_nothing(ctx, site):
    with pytest.raises(AuthRequired):
        api.saved_jobs(ctx)
    assert site.requests == []


# -- the CLI over the real stack (§5.3, §5.4) ---------------------------------------------------------------------

@pytest.fixture
def luk(monkeypatch, make_ctx, capsys):
    """Run `luk ARGS` in-process on the real api (MockTransport); returns (exit code, stdout, stderr)."""
    monkeypatch.setattr(ApiContext, "from_env", classmethod(lambda cls, *, verbose=False: make_ctx()))

    def run(*args: str) -> tuple[int, str, str]:
        code = cli.main(list(args))
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def test_cli_search_json_validates_and_notes_go_to_stderr(luk):
    code, out, err = luk("search", "analista", "--location", "Santiago", "--limit", "5", "--json")

    assert code == 0
    data = valid(json.loads(out))
    assert len(data.results) == 5 and data.total > 0 and data.query.location_ids == [1318]
    assert "Ubicación: Santiago, Región Metropolitana, Chile [Provincia] (1318)" in err


def test_cli_sign_in_redirect_exits_2_with_the_error_document(luk, site, saved_session, session_value):
    saved_session()
    code, out, err = luk("saved", "--json")

    assert code == 2 and [r.url.path for r in site.requests] == ["/saved_jobs"]
    error = ErrorEnvelope.model_validate(json.loads(out)).error
    assert (error.code, error.exit_code) == ("AUTH_REQUIRED", 2)
    assert err.strip() == "luk: Session expired or missing. Run `luk login`."
    assert session_value not in out + err


@pytest.mark.synthetic
def test_cli_onboarding_redirect_exits_2(luk, site, saved_session):
    saved_session()
    site.routes["/saved_jobs"] = lambda r: redirect(f"{BASE}/onboarding")
    site.routes["/onboarding"] = lambda r: page("synthetic/onboarding.html")
    code, out, _ = luk("saved", "--json")

    assert code == 2
    assert json.loads(out)["error"]["message"] == (
        "Luk redirected to /onboarding — finish your profile in the browser, then retry")


def test_cli_no_session_exits_2_without_a_request(luk, site):
    code, out, _ = luk("saved", "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "AUTH_REQUIRED" and site.requests == []


def test_cli_gone_offer_exits_3(luk):
    code, out, _ = luk("show", GONE_SLUG, "--json")
    assert code == 3 and json.loads(out)["error"]["code"] == "NOT_FOUND"


def test_cli_open_without_a_browser_is_an_error_naming_the_url(luk, site, monkeypatch):
    """The real `webbrowser.open` with nothing registered (SSH without DISPLAY, a container) returns False:
    `luk open` then exits 1 with the URL to open by hand, never "Opened …"; no request reaches Luk."""
    import webbrowser

    monkeypatch.setattr(webbrowser, "_tryorder", [])
    monkeypatch.setattr(webbrowser, "_browsers", {})
    code, out, err = luk("open", DETAIL_SLUG)

    assert (code, out, site.requests) == (1, "", [])
    assert err.splitlines()[0] == f"luk: could not open a browser — open this URL yourself: {BASE}/job_offers/{DETAIL_SLUG}"
    assert "Opened" not in err


def test_cli_csv_of_a_real_search(luk):
    code, out, _ = luk("search", "analista", "--csv")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("﻿slug,url,title,") and len(lines) == 16


ESC = "\x1b"
ST = ESC + "\\"  # string terminator
INJECTED_TITLE = f"Analista {ESC}]0;PWNED{ST} {ESC}[2J{ESC}[H\x9b2J\x07 Tributario"  # OSC title, clear, C1 CSI, BEL
INJECTED_PLACE = f"Santiago{ESC}]0;AREA{ST}{ESC}[2J, Chile"
INJECTED = (f"{ESC}]", f"{ESC}[2J", f"{ESC}[H", "\x9b", "\x07", ST)


def injected_site(site: Site) -> None:
    """Offer and area text carrying terminal control sequences (untrusted third-party data)."""
    def search(request: httpx.Request) -> httpx.Response:
        text = (FIXTURES / "public/search_analista.html").read_text("utf-8")
        body = text.replace("Analista Demo 01", INJECTED_TITLE, 1).replace("Empresa Demo 01 SpA", "&#x1b;[31mEvil", 1)
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/html; charset=utf-8"})

    def areas(request: httpx.Request) -> httpx.Response:
        data = json.loads((FIXTURES / "public/areas.json").read_text("utf-8"))
        data["areas"][0]["display_path"] = INJECTED_PLACE
        return httpx.Response(200, content=json.dumps(data).encode(), headers={"content-type": "application/json"})

    site.routes["/job_offers"] = search
    site.routes["/flexible_search/areas"] = areas


@pytest.mark.parametrize("output", [(), ("--csv",), ("--json",)])
def test_cli_never_writes_terminal_control_sequences_from_luk(luk, site, monkeypatch, output):
    monkeypatch.setenv("FORCE_COLOR", "1")  # rich treats stdout as a terminal
    monkeypatch.setenv("COLUMNS", "300")
    injected_site(site)
    code, out, err = luk("search", "analista", "--location", "Santiago", "--limit", "3", *output)

    assert code == 0
    for sequence in INJECTED:
        assert sequence not in out, (sequence, output)
        assert sequence not in err, (sequence, output)
    assert "PWNED" in out and "Ubicación: Santiago]0;AREA" in err  # the text stays, only the controls go


# -- Playwright stays out of every command but login (§3, §8.2) ---------------------------------------------------

PLAYWRIGHT_PROBE = r'''
import json, os, sys, webbrowser

webbrowser.open = lambda url, *args, **kwargs: True  # patched before luk_cli binds it: no real browser
tests, work = sys.argv[1], sys.argv[2]
sys.path.insert(0, tests)
from luk_cli import config

config.user_config_dir = lambda *a, **k: os.path.join(work, "config")  # never the user's real session
config.user_cache_dir = lambda *a, **k: os.path.join(work, "cache")
import anyio, httpx
from luk_cli import api, cli, mcp_server
from luk_cli.api import ApiContext
from luk_cli.config import load_settings
from luk_cli.models import SessionMeta
from luk_cli.ratelimit import ALGOLIA_LIMITER, RateLimiter
from luk_cli.session import SessionStore
import test_e2e as e2e

ALGOLIA_LIMITER.min_interval_s = 0.0
VALUE = "a" * 40 + "--" + "b" * 40
site = e2e.Site()
clock = [1_790_000_000.0]


def advance(seconds):
    clock[0] += max(0.0, seconds)


def logged_in(name):
    def serve(request):
        if request.headers.get("cookie") != "_portal_de_empleos_session=" + VALUE:
            return e2e.redirect(e2e.BASE + "/users/sign_in")
        return e2e.page(name)
    return serve


site.routes.update({
    "/saved_jobs": logged_in("synthetic/saved_jobs.html"),
    "/profile/application_histories": logged_in("synthetic/application_histories.html"),
    "/profile/cvs": logged_in("synthetic/cvs.html"),
    "/": lambda r: e2e.page("synthetic/root_logged_in.html" if "cookie" in r.headers else "public/root.html"),
})


def from_env(cls, *, verbose=False):
    settings = load_settings()
    return ApiContext(settings, SessionStore(settings),
                      RateLimiter.from_settings(settings, clock=lambda: clock[0], sleep=advance), verbose=verbose,
                      luk_transport=httpx.MockTransport(site),
                      algolia_transport=httpx.MockTransport(lambda r: e2e.json_file("synthetic/algolia_suggestions.json")),
                      today=lambda: e2e.TODAY)


ApiContext.from_env = classmethod(from_env)
settings = load_settings()
SessionStore(settings).save(
    {"cookies": [{"name": "_portal_de_empleos_session", "value": VALUE, "domain": "www.takealuk.com", "path": "/",
                  "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}], "origins": []},
    SessionMeta(logged_in_at="2026-09-23T12:00:00Z", browser="chromium", luk_cli_version="0.1.0"),
)
commands = [
    ["areas", "santiago", "--json"], ["search", "analista", "--location", "Santiago", "--csv"],
    ["show", e2e.DETAIL_SLUG], ["suggest", "analista fin"], ["similar-roles", "analista financiero"],
    ["companies", "--query", "banco"], ["company", "empresa-demo-86"], ["saved", "--details"],
    ["applications", "--csv"], ["cvs"], ["whoami", "--json"], ["session", "status", "--check"],
    ["open", e2e.DETAIL_SLUG], ["doctor"], ["doctor", "--live"], ["doctor", "--print-mcp-json"],
    ["debug", "capture", "/saved_jobs", "--out", os.path.join(work, "captures")], ["schema", "job"],
]
result = {"codes": {" ".join(c): cli.main(c) for c in commands}, "tool_errors": {}}
server = mcp_server.build_server()
result["tools"] = sorted(tool.name for tool in anyio.run(server.list_tools))
tool_calls = {
    "search_jobs": {"roles": ["analista"], "max_results": 3}, "get_jobs": {"slugs_or_urls": [e2e.DETAIL_SLUG]},
    "related_roles": {"role": "analista financiero"}, "find_locations": {"text": "santiago"},
    "search_companies": {"name": "banco"}, "get_company": {"slug_or_url": "empresa-demo-86"},
    "account_status": {"check": True}, "my_saved_jobs": {"details": True}, "my_applications": {}, "my_cvs": {},
}
for name, arguments in tool_calls.items():
    try:
        anyio.run(server.call_tool, name, arguments)
    except Exception as err:
        result["tool_errors"][name] = str(err)
result["codes"]["logout"] = cli.main(["logout"])
result["codes"]["mcp"] = cli.main(["mcp"])  # stdin is empty: the stdio server starts and ends at EOF
result["playwright"] = "playwright" in sys.modules
with open(os.path.join(work, "result.json"), "w", encoding="utf-8") as out:
    json.dump(result, out)
'''


def test_no_command_but_login_imports_playwright(tmp_path):
    """§3/§8.2: Playwright is lazy (login and refresh only). Every other command runs on the REAL api, http,
    session and parsers (MockTransport, fixtures, a saved session), then the MCP server is built, lists its
    tools and runs each one, and `luk mcp` serves stdio; 'playwright' is still not in sys.modules."""
    proc = subprocess.run([sys.executable, "-c", PLAYWRIGHT_PROBE, str(Path(__file__).parent), str(tmp_path)],
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=300, check=False)
    assert (tmp_path / "result.json").exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    result = json.loads((tmp_path / "result.json").read_text("utf-8"))

    doctor = {"doctor", "doctor --live"}  # their checks may fail on another machine; they still ran
    assert {c: code for c, code in result["codes"].items() if c not in doctor and code != 0} == {}
    assert result["tool_errors"] == {} and len(result["tools"]) == 10
    assert result["playwright"] is False
