"""Parsers against the public fixtures (minimized captures of real Luk pages, all content fake) and the synthetic
pages (spec §2.1, §5.2, §5.5, §8.1, §8.2)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from luk_cli import parsers
from luk_cli.config import DEFAULT_ALGOLIA_INDEX
from luk_cli.errors import AuthRequired, Blocked, SiteChanged
from luk_cli.models import Address, AreaRef, Salary

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.takealuk.com"
TODAY = date(2026, 9, 23)  # the day the public fixtures were captured
JSON_CT = "application/json; charset=utf-8"


def public(name: str) -> str:
    return (FIXTURES / "public" / name).read_text(encoding="utf-8")


def synthetic(name: str) -> str:
    return (FIXTURES / "synthetic" / name).read_text(encoding="utf-8")


def search(name: str, page: int = 1):
    return parsers.parse_search(public(name), page=page, today=TODAY, base_url=BASE, capture_path="/job_offers?x=1")


def job(html: str, slug: str = "analista-demo-01-empresa-demo-01", today: date = TODAY):
    return parsers.parse_job(html, slug=slug, base_url=BASE, today=today, capture_path=f"/job_offers/{slug}")


def by_slug(cards, slug):
    return next(card for card in cards if card.slug == slug)


# --- synthetic fixtures carry the §8.1 marker on line 1 ----------------------------------------


@pytest.mark.synthetic
@pytest.mark.parametrize("path", sorted((FIXTURES / "synthetic").iterdir()), ids=lambda p: p.name)
def test_every_synthetic_fixture_is_marked_on_line_one(path: Path) -> None:
    assert "SYNTHETIC:" in path.read_text(encoding="utf-8").splitlines()[0]


# --- search -----------------------------------------------------------------------------------


def test_search_page_one_total_pagination_and_extras() -> None:
    result = search("search_analista.html")
    assert result.total == 273
    assert len(result.cards) == 15
    assert result.pagination == parsers.Pagination(last_page=19, has_next=True)
    assert result.effective_location == AreaRef(id=1021, display_path="Chile", area_type_label="País")
    assert result.related_roles == [
        "Analista De Business Intelligence", "Analista De Churn", "Analista Inteligencia De Negocio",
    ]


def test_search_card_fields_are_typed_by_value() -> None:
    card = search("search_analista.html").cards[0]
    assert card.slug == "analista-demo-01-empresa-demo-01"
    assert card.url == f"{BASE}/job_offers/analista-demo-01-empresa-demo-01"
    assert card.title == "Analista Demo 01"
    assert card.company == "Empresa Demo 01 SpA"
    assert card.location == "Las Condes, Santiago, Región Metropolitana, Chile"
    assert card.salary == Salary(raw="CLP $650.000 - $850.000", currency="CLP", min=650000, max=850000)
    assert card.employment_type == "full_time"
    assert card.modality == "on_site"
    assert card.labels == ["Jornada Completa", "Presencial"]
    assert card.posted_ago == "Hace 1 día"
    assert card.posted_at_approx == TODAY - timedelta(days=1)


def test_search_salary_only_on_two_of_fifteen_cards() -> None:
    cards = search("search_analista.html").cards
    salaries = [card.salary for card in cards if card.salary is not None]
    assert len(salaries) == 2
    assert (salaries[1].min, salaries[1].max) == (950000, 1400000)


def test_search_empty_company_is_null_and_whitespace_is_collapsed() -> None:
    cards = search("search_analista.html").cards
    assert by_slug(cards, "analista-demo-04-chile").company is None
    assert by_slug(cards, "analista-demo-09-empresa-demo-11").company == "Empresa Demo 11 SpA"


@pytest.mark.parametrize(
    ("slug", "age", "days"),
    [
        ("analista-demo-07-empresa-demo-09", "Hace menos de 1 hora", 0),
        ("analista-demo-08-empresa-demo-10", "Hace 23 horas", 0),
        ("analista-demo-03-empresa-demo-03", "Hace 20 días", 20),
        ("analista-demo-06-empresa-demo-07", "Hace 5 meses", 150),
        ("analista-demo-05-empresa-demo-05", "Hace 1 mes", 30),
    ],
)
def test_search_posted_at_approx_from_the_age(slug: str, age: str, days: int) -> None:
    card = by_slug(search("search_analista.html").cards, slug)
    assert card.posted_ago == age
    assert card.posted_at_approx == TODAY - timedelta(days=days)


@pytest.mark.parametrize("age", ["Hace 2 años", "Hace un momento", "Publicada ayer"])
def test_search_unparsable_age_gives_no_date(age: str) -> None:
    html = public("search_analista.html").replace("<span>Hace 1 día</span>", f"<span>{age}</span>", 1)
    card = parsers.parse_search(html, page=1, today=TODAY, base_url=BASE, capture_path="/job_offers").cards[0]
    assert card.posted_at_approx is None
    assert card.posted_ago == (age if age.startswith("Hace") else None)


def test_search_page_two_keeps_nav_labels_and_ignores_locale_links() -> None:
    result = search("search_p2.html", page=2)
    assert result.pagination == parsers.Pagination(last_page=19, has_next=True)
    assert all(card.salary is None for card in result.cards)
    no_age = by_slug(result.cards, "analista-demo-18-empresa-demo-20")
    assert (no_age.posted_ago, no_age.posted_at_approx) == (None, None)
    assert by_slug(result.cards, "analista-demo-22-empresa-demo-24").title == "Analista Demo 22"
    assert result.effective_location is not None and result.effective_location.id == 1021


def test_search_worldwide_has_no_effective_location_and_parses_cop_salaries() -> None:
    result = search("search_worldwide.html")
    assert result.total == 735
    assert result.effective_location is None
    assert result.pagination.last_page == 49
    cop = by_slug(result.cards, "analista-demo-04-empresa-demo-41")
    assert cop.salary == Salary(raw="COP $2.150.000,00 - $2.150.000,00", currency="COP", min=2150000, max=2150000)


def test_search_zero_results_is_empty_not_an_error() -> None:
    result = search("search_zero.html")
    assert (result.total, result.cards, result.related_roles) == (0, [], [])
    assert result.pagination == parsers.Pagination(last_page=1, has_next=False)


def test_search_last_page_counts_the_unlabelled_current_page() -> None:
    result = search("search_last.html", page=19)
    assert len(result.cards) == 4
    assert result.pagination == parsers.Pagination(last_page=19, has_next=False)


def test_search_past_the_last_page_is_empty_not_an_error() -> None:
    result = search("search_past_last.html", page=20)
    assert (result.total, result.cards) == (274, [])
    assert result.pagination == parsers.Pagination(last_page=19, has_next=False)


def test_search_single_page_without_nav_and_unmapped_labels() -> None:
    result = search("search_single_page.html")
    assert result.pagination == parsers.Pagination(last_page=1, has_next=False)
    intern, other = result.cards
    assert (intern.employment_type, intern.modality) == ("intern", "on_site")
    assert (other.employment_type, other.modality, other.labels) == (None, None, ["30 horas"])


def test_search_trimmed_capture() -> None:
    result = search("takealuk_list.html")
    assert (result.total, len(result.cards)) == (1828, 3)
    assert result.cards[2].posted_ago is None
    assert result.effective_location is None


def test_search_without_results_frame_is_site_changed() -> None:
    with pytest.raises(SiteChanged) as info:
        parsers.parse_search(public("root.html"), page=1, today=TODAY, base_url=BASE, capture_path="/job_offers?q=1")
    assert "turbo-frame#job_offers_results" in info.value.message
    assert 'luk debug capture "/job_offers?q=1"' in info.value.message
    assert info.value.exit_code == 5


def test_search_non_integer_total_is_site_changed() -> None:
    html = public("search_analista.html").replace('<b class="text-primary">273</b>', '<b class="text-primary">muchos</b>')
    with pytest.raises(SiteChanged, match="job-offers-results-count"):
        parsers.parse_search(html, page=1, today=TODAY, base_url=BASE, capture_path="/job_offers")


def test_search_total_without_cards_on_a_real_page_is_site_changed() -> None:
    html = public("search_analista.html").replace("data-scroll-restore-slug=", "data-renamed-slug=")
    with pytest.raises(SiteChanged, match="data-scroll-restore-slug"):
        parsers.parse_search(html, page=1, today=TODAY, base_url=BASE, capture_path="/job_offers")


def test_search_card_without_title_is_site_changed() -> None:
    html = re.sub(
        r'(<h2 class="card-title job-offer-card__title min-w-0">)\s*Analista Demo 01\s*(</h2>)', r"\1\2",
        public("search_analista.html"), count=1,
    )
    with pytest.raises(SiteChanged, match="h2.job-offer-card__title"):
        parsers.parse_search(html, page=1, today=TODAY, base_url=BASE, capture_path="/job_offers")


def test_search_card_with_a_malformed_slug_is_site_changed() -> None:
    html = public("search_analista.html").replace(
        'data-scroll-restore-slug="analista-demo-01-empresa-demo-01"', 'data-scroll-restore-slug="../saved_jobs"'
    )
    with pytest.raises(SiteChanged, match="data-scroll-restore-slug"):
        parsers.parse_search(html, page=1, today=TODAY, base_url=BASE, capture_path="/job_offers")


@pytest.mark.synthetic
def test_search_challenge_page_is_blocked_not_site_changed() -> None:
    with pytest.raises(Blocked) as info:
        parsers.parse_search(synthetic("challenge_403.html"), page=1, today=TODAY, base_url=BASE, capture_path="/job_offers")
    assert info.value.exit_code == 4


# --- job detail -------------------------------------------------------------------------------


def test_job_detail_merges_json_ld_and_dom() -> None:
    parsed = job(public("detail.html"))
    posting = parsed.value
    assert parsed.warnings == []
    assert posting.slug == "analista-demo-01-empresa-demo-01"
    assert posting.url == f"{BASE}/job_offers/analista-demo-01-empresa-demo-01"
    assert posting.title == "Analista Demo 01"
    assert posting.offer_id == 10001
    assert posting.company == "Empresa Demo 01 SpA"
    assert posting.company_slug == "empresa-demo-01"
    assert posting.company_url == f"{BASE}/companies/empresa-demo-01"
    assert posting.location == "Las Condes, Santiago, Región Metropolitana, Chile"
    assert posting.address == Address(locality="Santiago", region="Región Metropolitana", country="CL")
    assert posting.salary == Salary(
        raw="CLP $650.000 - $850.000", currency="CLP", min=650000, max=850000, period="month"
    )
    assert (posting.employment_type, posting.modality) == ("full_time", "on_site")
    assert posting.labels == ["Jornada Completa", "Presencial"]
    assert posting.vacancies == 1
    assert posting.work_hours == "40 hours per week"
    assert (posting.date_posted, posting.valid_through) == (date(2026, 9, 22), date(2026, 12, 22))
    assert posting.status == "open"
    assert posting.direct_apply is True
    assert (posting.posted_ago, posting.posted_at_approx) == ("Hace 1 día", date(2026, 9, 22))
    assert (posting.canonical_slug, posting.text_truncated) == (None, False)


def test_job_detail_texts_keep_paragraphs_and_list_items() -> None:
    posting = job(public("detail.html")).value
    assert posting.description_text.startswith("Párrafo de ejemplo 1 sobre el cargo.")
    assert "Cargo:" not in posting.description_text
    assert "\n\nPárrafo de ejemplo 2:\n- Tarea de ejemplo 1." in posting.description_text
    assert "- Tarea de ejemplo 5." in posting.description_text
    assert "Ver más" not in posting.description_text
    assert posting.requirements_text is not None
    assert posting.requirements_text.startswith("- Requisito de ejemplo 1.\n- Requisito de ejemplo 2.")


def test_job_detail_is_expired_after_valid_through() -> None:
    assert job(public("detail.html"), today=date(2026, 12, 23)).value.status == "expired"
    assert job(public("detail.html"), today=date(2026, 12, 22)).value.status == "open"


def test_job_detail_never_takes_a_reserved_company_link() -> None:
    html = public("detail.html").replace(
        '"sameAs":"https://www.takealuk.com/companies/empresa-demo-01"',
        '"sameAs":"https://www.takealuk.com/companies/home"',
    )
    posting = job(html).value
    assert posting.company_slug == "empresa-demo-01"  # from the BreadcrumbList, not /companies/home


def test_job_detail_without_json_ld_parses_the_dom_and_warns() -> None:
    html = re.sub(r'<script type="application/ld\+json">.*?</script>', "", public("detail.html"), flags=re.DOTALL)
    parsed = job(html)
    posting = parsed.value
    assert len(parsed.warnings) == 1 and "JSON-LD" in parsed.warnings[0]
    assert posting.title == "Analista Demo 01"
    assert posting.company == "Empresa Demo 01 SpA"
    assert posting.company_slug == "empresa-demo-01"  # the title-block link, never the nav's /companies/home
    assert (posting.offer_id, posting.date_posted, posting.address, posting.work_hours) == (None, None, None, None)
    assert posting.salary == Salary(raw="CLP $650.000 - $850.000", currency="CLP", min=650000, max=850000)
    assert posting.employment_type == "full_time"  # from the DOM label
    assert posting.description_text.startswith("Párrafo de ejemplo 1")
    assert posting.status == "open"


def test_job_detail_trimmed_capture_uses_json_ld_text_without_the_boilerplate() -> None:
    posting = job(public("takealuk_detail.html"), slug="barista-demo-44-empresa-demo-86").value
    assert posting.title == "Barista Demo 44"
    assert posting.offer_id == 10002
    assert posting.company_slug == "empresa-demo-86"
    assert (posting.salary, posting.location, posting.vacancies) == (None, None, None)
    assert posting.address == Address(country="CL")
    assert posting.description_text.startswith("Párrafo de ejemplo 1 sobre el cargo.")
    assert "Cargo:" not in posting.description_text
    assert "\n- Tarea de ejemplo 1." in posting.description_text
    assert posting.requirements_text is None


def test_job_error_page_is_site_changed_not_blocked() -> None:
    with pytest.raises(SiteChanged, match="job-offer-show-title h1"):
        job(public("detail_410.html"), slug="zzzz-no-existe-12345")


@pytest.mark.synthetic
def test_job_404_page_is_site_changed_and_challenge_is_blocked() -> None:
    with pytest.raises(SiteChanged):
        job(synthetic("not_found_404.html"))
    with pytest.raises(Blocked):
        job(synthetic("challenge_403.html"))


# --- companies --------------------------------------------------------------------------------


def companies(name: str):
    return parsers.parse_companies(public(name), base_url=BASE, capture_path="/companies")


def test_companies_directory_ignores_the_skeleton_cards() -> None:
    result = companies("companies.html")
    assert result.total == 2138
    assert len(result.cards) == 24
    assert result.pagination == parsers.Pagination(last_page=90, has_next=True)
    first = result.cards[0]
    assert (first.slug, first.url, first.name) == ("empresa-demo-44", f"{BASE}/companies/empresa-demo-44", "Empresa Demo 44 SpA")
    assert (first.location, first.sector, first.size) == ("Antofagasta, Chile", None, None)
    assert first.active_offers == 99
    assert first.offer_locations == ["Antofagasta, Chile"]


def test_companies_query_tags_offers_and_missing_location() -> None:
    result = companies("companies_q.html")
    assert (result.total, len(result.cards)) == (11, 11)
    assert result.pagination == parsers.Pagination(last_page=1, has_next=False)
    cards = {card.slug: card for card in result.cards}
    assert [card.slug for card in result.cards if card.sector or card.size] == ["empresa-demo-63"]
    assert (cards["empresa-demo-63"].sector, cards["empresa-demo-63"].size) == ("Finanzas", "51-200 empleados")
    assert cards["empresa-demo-63"].active_offers == 1
    assert cards["empresa-demo-65"].active_offers == 4
    assert (cards["empresa-demo-66"].active_offers, cards["empresa-demo-66"].offer_locations) == (0, [])
    assert cards["empresa-demo-70"].location is None


def test_companies_with_location_and_an_unnamed_company() -> None:
    result = companies("companies_location.html")
    assert (result.total, result.pagination.last_page) == (226, 10)
    assert by_slug(result.cards, "empresa-demo-74").name == ""


def test_companies_without_frame_or_integer_total_is_site_changed() -> None:
    with pytest.raises(SiteChanged, match="companies_marketplace_results"):
        parsers.parse_companies(public("root.html"), base_url=BASE, capture_path="/companies")
    html = public("companies_q.html").replace('<b class="text-primary">11</b>', '<b class="text-primary"></b>')
    with pytest.raises(SiteChanged, match="b.text-primary"):
        parsers.parse_companies(html, base_url=BASE, capture_path="/companies")


def test_companies_card_without_name_is_site_changed() -> None:
    html = public("companies_q.html").replace('<h2 class="company-card__name">Empresa Demo 65 SpA</h2>', "")
    with pytest.raises(SiteChanged, match="company-card__name"):
        parsers.parse_companies(html, base_url=BASE, capture_path="/companies")


# --- company page -----------------------------------------------------------------------------


def company(name: str, slug: str, page: int = 1):
    return parsers.parse_company(public(name), slug=slug, page=page, base_url=BASE, today=TODAY, capture_path=f"/companies/{slug}")


def test_company_page_skips_the_breadcrumb_trap() -> None:
    result = company("company.html", "empresa-demo-86")
    assert (result.slug, result.url) == ("empresa-demo-86", f"{BASE}/companies/empresa-demo-86")
    assert result.name == "Empresa Demo 86 SpA"
    assert result.location == "Santa Cruz, Colchagua, O'Higgins, Chile"
    assert result.active_offers == 2
    assert [job.slug for job in result.jobs][:1] == ["barista-demo-44-empresa-demo-86"]
    assert len(result.jobs) == 2
    assert (result.page, result.has_more, result.next_page) == (1, False, None)


def test_company_pagination_inside_the_jobs_frame() -> None:
    first = company("company_paginated.html", "empresa-demo-54")
    assert (len(first.jobs), first.active_offers, first.has_more, first.next_page) == (20, 29, True, 2)
    second = company("company_paginated_p2.html", "empresa-demo-54", page=2)
    assert (len(second.jobs), second.page, second.has_more, second.next_page) == (9, 2, False, None)


def test_company_page_without_h1_or_jobs_frame_is_site_changed() -> None:
    with pytest.raises(SiteChanged, match="company_job_offers_results"):
        parsers.parse_company(public("detail.html"), slug="x", page=1, base_url=BASE, today=TODAY, capture_path="/companies/x")
    with pytest.raises(SiteChanged, match="h1"):
        parsers.parse_company(
            public("company.html").replace("<h1", "<h2").replace("</h1>", "</h2>"),
            slug="x", page=1, base_url=BASE, today=TODAY, capture_path="/companies/x",
        )


# --- JSON endpoints ---------------------------------------------------------------------------


def test_areas_keep_server_order_and_all_fields() -> None:
    areas = parsers.parse_areas(public("areas.json"), JSON_CT)
    assert [area.id for area in areas] == [1318, 1348, 17863, 9579, 15804]
    first = areas[0]
    assert (first.name, first.display_path) == ("Santiago", "Santiago, Región Metropolitana, Chile")
    assert (first.area_type, first.area_type_label, first.offer_count) == ("administrative_area_level_2", "Provincia", 601)
    assert (first.depth, first.visitor_country_match) == (2, True)
    assert areas[3].visitor_country_match is False
    assert parsers.parse_areas(public("areas_companies.json"), JSON_CT)[0].offer_count == 284


def test_areas_contract_violations() -> None:
    with pytest.raises(SiteChanged, match="JSON"):
        parsers.parse_areas(public("areas.json"), "text/html; charset=utf-8")
    with pytest.raises(SiteChanged, match="areas"):
        parsers.parse_areas('{"items": []}', JSON_CT)
    with pytest.raises(SiteChanged):
        parsers.parse_areas("{not json", JSON_CT)
    with pytest.raises(SiteChanged):
        parsers.parse_areas('{"areas": [{"id": "1318", "name": "Santiago"}]}', JSON_CT)
    with pytest.raises(Blocked):
        parsers.parse_areas(synthetic("challenge_403.html"), "text/html")


def test_similar_roles() -> None:
    result = parsers.parse_similar_roles(public("similar.json"), JSON_CT, role="analista financiero")
    assert result.role == "analista financiero"
    assert result.resolved_name == "Analista Financiero"
    assert len(result.items) == 9 and result.items[0] == "Analista De Finanzas"
    assert result.next_page == 2
    with pytest.raises(SiteChanged, match="items"):
        parsers.parse_similar_roles('{"resolved_name": "x"}', JSON_CT, role="x")


@pytest.mark.synthetic
def test_algolia_suggestions() -> None:
    suggestions = parsers.parse_suggestions(synthetic("algolia_suggestions.json"), "application/json; charset=UTF-8")
    assert [(s.query, s.popularity) for s in suggestions] == [
        ("analista financiero", 412), ("analista de finanzas", 97), ("analista financiero senior", None),
    ]
    with pytest.raises(SiteChanged, match="hits"):
        parsers.parse_suggestions('{"results": []}', JSON_CT)


def test_algolia_config_is_discovered_from_the_home_page() -> None:
    config = parsers.parse_algolia_config(public("root.html"))
    assert config.app_id == "TESTAPPID0"
    assert config.api_key
    assert config.index == "JobOffer_query_suggestions"


def test_algolia_config_contract() -> None:
    root = public("root.html")
    no_index = root.replace("data-query-suggestions-index=", "data-other=")
    assert parsers.parse_algolia_config(no_index).index == DEFAULT_ALGOLIA_INDEX
    with pytest.raises(SiteChanged, match="application-id"):
        parsers.parse_algolia_config(root.replace("data-search-pills-application-id-value=", "data-x="))
    with pytest.raises(SiteChanged, match="api-key"):
        parsers.parse_algolia_config(root.replace("data-search-pills-search-api-key-value=", "data-x="))
    with pytest.raises(SiteChanged, match="application-id"):
        parsers.parse_algolia_config(root.replace('"TESTAPPID0"', '"evil.example/x"'))


# --- whoami -----------------------------------------------------------------------------------


def test_whoami_on_the_anonymous_home_page_is_auth_required() -> None:
    with pytest.raises(AuthRequired):
        parsers.parse_whoami(public("root.html"))


@pytest.mark.synthetic
def test_whoami_logged_in_header() -> None:
    who = parsers.parse_whoami(synthetic("root_logged_in.html"))
    assert (who.logged_in, who.name, who.email) == (True, "Paz Prueba", "paz.prueba@example.com")
    assert parsers.parse_whoami(synthetic("onboarding.html")).logged_in is True


def test_whoami_name_from_the_avatar_label_alone() -> None:
    who = parsers.parse_whoami('<header id="main-header"><button aria-label="Avatar de Ana Soto">AS</button></header>')
    assert (who.name, who.email) == ("Ana Soto", None)


def test_whoami_user_menu_without_a_name() -> None:
    who = parsers.parse_whoami('<header id="main-header"><div id="header-user-menu"><p class="header-dropdown-email">a@example.com</p></div></header>')
    assert (who.logged_in, who.name, who.email) == (True, None, "a@example.com")


def test_whoami_without_either_marker_is_site_changed() -> None:
    with pytest.raises(SiteChanged, match='luk debug capture "/"'):
        parsers.parse_whoami(public("detail_410.html"))


# --- private pages (unverified until the first capture) ---------------------------------------


def test_private_parsers_ship_unverified() -> None:
    assert parsers.VERIFIED == {"saved_jobs": False, "application_histories": False, "cvs": False}


@pytest.mark.synthetic
def test_saved_jobs_reuse_the_search_card() -> None:
    result = parsers.parse_saved_jobs(synthetic("saved_jobs.html"), today=TODAY, base_url=BASE)
    assert result.verified is False
    assert result.warnings == ["parser unverified — run `luk debug capture /saved_jobs`"]
    assert result.pagination == parsers.Pagination(last_page=2, has_next=True)
    first, second, third = result.items
    assert first.slug == "analista-de-datos-empresa-ficticia"
    assert (first.salary.min, first.salary.max, first.modality) == (1200000, 1500000, "hybrid")
    assert first.posted_at_approx == TODAY - timedelta(days=3)
    assert (second.title, second.company, second.employment_type, second.modality) == (
        "Ejecutivo Comercial", None, "part_time", "remote",
    )
    assert (third.employment_type, third.posted_ago) == ("intern", None)


@pytest.mark.synthetic
def test_saved_jobs_empty_is_not_an_error() -> None:
    result = parsers.parse_saved_jobs(synthetic("saved_jobs_empty.html"), today=TODAY, base_url=BASE)
    assert result.items == []
    assert result.warnings == ["parser unverified — run `luk debug capture /saved_jobs`"]


def test_saved_jobs_fallback_reads_offer_links_outside_the_page_chrome() -> None:
    html = """
    <header id="main-header"><a href="/job_offers/in-the-header">Header</a></header>
    <main>
      <ul class="saved-list">
        <li><a href="/job_offers/contador-senior"><h3> Contador   Senior </h3></a>
            <form method="post" action="/job_offers/contador-senior/save_later"></form></li>
        <li><a href="https://www.takealuk.com/job_offers/disenador-ux?ref=saved">Diseñador UX</a></li>
        <li><a href="/job_offers/contador-senior">Contador Senior (again)</a></li>
        <li><a href="/job_offers/x/save_later">not an offer</a><a href="https://evil.example/job_offers/y">evil</a></li>
      </ul>
    </main>
    <footer><a href="/job_offers/in-the-footer">Footer</a></footer>
    """
    result = parsers.parse_saved_jobs(html, today=TODAY, base_url=BASE)
    assert [(card.slug, card.title) for card in result.items] == [
        ("contador-senior", "Contador Senior"), ("disenador-ux", "Diseñador UX"),
    ]
    assert result.items[1].url == f"{BASE}/job_offers/disenador-ux"


def test_private_page_showing_the_anonymous_header_is_auth_required() -> None:
    with pytest.raises(AuthRequired):
        parsers.parse_saved_jobs(public("root.html"), today=TODAY, base_url=BASE)
    with pytest.raises(AuthRequired):
        parsers.parse_applications(public("root.html"), base_url=BASE)
    with pytest.raises(AuthRequired):
        parsers.parse_cvs(public("root.html"))


@pytest.mark.synthetic
def test_applications_raw_text_with_statuses() -> None:
    result = parsers.parse_applications(synthetic("application_histories.html"), base_url=BASE)
    assert result.warnings == ["parser unverified — run `luk debug capture /profile/application_histories`"]
    assert result.pagination == parsers.Pagination(last_page=1, has_next=False)
    first = result.items[0]
    assert (first.slug, first.url) == ("analista-de-datos-empresa-ficticia", f"{BASE}/job_offers/analista-de-datos-empresa-ficticia")
    assert (first.title, first.company) == ("Analista de Datos", "Empresa Ficticia S.A.")
    assert (first.status_text, first.applied_at_text) == ("En revisión", "Postulaste el 12 de septiembre de 2026")
    assert [item.status_text for item in result.items] == ["En revisión", "Enviada", "Proceso finalizado"]
    assert result.items[1].applied_at_text == "Postulaste hace 3 días"


def test_applications_fields_never_leak_from_the_next_item() -> None:
    html = """
    <main><ul>
      <li><a href="/job_offers/uno"><h3>Uno</h3></a></li>
      <li><a href="/job_offers/dos"><h3>Dos</h3></a><span class="application-status">Enviada</span><p>Postulaste hace 2 días</p></li>
    </ul></main>
    """
    items = parsers.parse_applications(html, base_url=BASE).items
    assert [(item.slug, item.status_text, item.applied_at_text) for item in items] == [
        ("uno", None, None), ("dos", "Enviada", "Postulaste hace 2 días"),
    ]


def test_applications_fallback_without_offer_links() -> None:
    html = """
    <main>
      <div class="application-history">
        <h3>Jefe de Bodega</h3><p class="item-title">Logística Ejemplo</p>
        <span class="tag-navy">Jornada Completa</span><span class="tag-danger">Oferta cerrada</span>
        <time datetime="2026-08-01">1 de agosto de 2026</time>
      </div>
      <div class="application-history"><p>sin título</p></div>
    </main>
    """
    result = parsers.parse_applications(html, base_url=BASE)
    assert len(result.items) == 1
    item = result.items[0]
    assert (item.slug, item.url, item.title, item.company) == (None, None, "Jefe de Bodega", "Logística Ejemplo")
    assert (item.status_text, item.applied_at_text) == ("Oferta cerrada", "1 de agosto de 2026")


@pytest.mark.synthetic
def test_cvs_metadata_only() -> None:
    result = parsers.parse_cvs(synthetic("cvs.html"))
    assert result.warnings == ["parser unverified — run `luk debug capture /profile/cvs`"]
    assert [(cv.name, cv.updated_at_text) for cv in result.items] == [
        ("cv-1.pdf", "Actualizado el 20 de septiembre de 2026"), ("cv-2.docx", "Subido hace 2 meses"),
    ]


def test_cvs_fallback_by_cv_class_without_file_extension() -> None:
    html = """
    <main><section class="cv-list">
      <article class="cv-item"><h3>CV Principal</h3><p>Actualizado hace 5 días</p></article>
      <article class="cv-item"><h3>CV Inglés</h3></article>
    </section></main>
    """
    result = parsers.parse_cvs(html)
    assert [(cv.name, cv.updated_at_text) for cv in result.items] == [
        ("CV Principal", "Actualizado hace 5 días"), ("CV Inglés", None),
    ]


@pytest.mark.synthetic
def test_private_parsers_on_the_onboarding_page_find_nothing() -> None:
    # The final-path check in http (redirect to /onboarding → AUTH_REQUIRED) protects these parsers.
    assert parsers.parse_saved_jobs(synthetic("onboarding.html"), today=TODAY, base_url=BASE).items == []
    assert parsers.parse_cvs(synthetic("onboarding.html")).items == []


# --- missing_anchor ---------------------------------------------------------------------------


def test_missing_anchor_names_page_selector_and_capture_path() -> None:
    err = parsers.missing_anchor("<html><body>ok</body></html>", page="search", selector="x.y", capture_path="/job_offers?a=1")
    assert isinstance(err, SiteChanged)
    assert err.message == 'Luk search page changed: missing x.y. Run `luk debug capture "/job_offers?a=1"` and report it.'
    challenge = parsers.missing_anchor("<title>Just a moment...</title>", page="search", selector="x", capture_path="/")
    assert isinstance(challenge, Blocked)
