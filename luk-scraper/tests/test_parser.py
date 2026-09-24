"""Listing-card parsing on the fake listing fixture and synthetic cards (offline)."""
from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup

from conftest import card, listing_page
from luk_scraper.models import Offer
from luk_scraper.parser import (extract_meta, parse_card, parse_listing, parse_location,
                                slug_from_href)

SLUG_1 = "analista-de-datos-empresa-demo-01-spa-chile"
SLUG_2 = "asistente-administrativo-a-empresa-demo-02-spa"
SLUG_3 = "supervisor-de-obra-empresa-demo-03-spa"


def _by_slug(offers):
    return {o.slug: o for o in offers}


def test_parse_listing_count_and_identity(listing_html):
    offers = parse_listing(listing_html)
    assert [o.slug for o in offers] == [SLUG_1, SLUG_2, SLUG_3]
    assert all(isinstance(o, Offer) for o in offers)
    assert all(o.url == f"https://www.takealuk.com/job_offers/{o.slug}" for o in offers)


def test_card_with_country_only_location(listing_html):
    o = _by_slug(parse_listing(listing_html))[SLUG_1]
    assert o.title == "Analista de Datos Demo"
    assert o.company == "Empresa Demo 01 SpA"
    assert o.location_text == "Chile"
    assert (o.region, o.comuna, o.country) == (None, None, "Chile")
    assert o.workday == "Jornada Completa"
    assert o.modality is None
    assert o.posted_relative == "Hace menos de 1 hora"
    # the card has no absolute date nor description: those come from the detail page
    assert o.published_at is None and o.description is None and o.external_id is None


def test_card_with_four_part_metropolitana_location(listing_html):
    o = _by_slug(parse_listing(listing_html))[SLUG_2]
    assert o.region == "Metropolitana"               # 'Región Metropolitana' -> canonical
    assert o.comuna == "Las Condes"
    assert o.country == "Chile"
    assert o.location_text == "Las Condes, Santiago, Región Metropolitana, Chile"
    assert (o.workday, o.modality) == ("Jornada Completa", "Presencial")
    assert o.title == "Asistente Administrativo(a) Demo"


def test_card_with_three_part_location_and_no_relative_date(listing_html):
    o = _by_slug(parse_listing(listing_html))[SLUG_3]
    assert (o.region, o.comuna) == ("Los Ríos", "Valdivia")
    assert o.posted_relative is None


def test_fetched_at_is_utc_iso_and_shared_by_the_page(listing_html):
    offers = parse_listing(listing_html)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", offers[0].fetched_at)
    assert len({o.fetched_at for o in offers}) == 1
    assert parse_listing(listing_html, fetched_at="2026-01-01T00:00:00+00:00")[0].fetched_at \
        == "2026-01-01T00:00:00+00:00"


def test_save_form_and_logo_are_not_mistaken_for_data(listing_html):
    # The card's "save offer" <form action="/job_offers/{slug}/save_later"> is ignored.
    offers = parse_listing(listing_html)
    assert all("save_later" not in o.url for o in offers)
    assert all(o.company and "example.com" not in o.company for o in offers)


def test_extract_meta_total_and_last_page(listing_html):
    assert extract_meta(listing_html) == (1828, 122)


def test_extract_meta_prefers_pagination_nav():
    html = ('<a href="/companies?page=900">x</a><nav class="pagination">'
            '<a href="?page=2">2</a><a href="?page=7">7</a></nav>')
    assert extract_meta(html) == (None, 7)


def test_extract_meta_thousands_separator():
    html = '<div class="job-offers-results-count"><b>1.837</b></div>'
    assert extract_meta(html) == (1837, None)


def test_empty_or_foreign_html_is_safe():
    assert parse_listing("") == []
    assert parse_listing(None) == []
    assert parse_listing("<html><body>nada</body></html>") == []
    assert extract_meta("") == (None, None)


def test_card_without_slug_is_skipped():
    skeleton = '<div class="job-offer-card loading-job-offer"><h2 class="card-title">...</h2></div>'
    assert parse_listing(skeleton + card("cargo-demo-1")) != []
    assert [o.slug for o in parse_listing(skeleton)] == []


def test_slug_falls_back_to_data_attribute():
    html = ('<div class="job-offer-card" data-scroll-restore-slug="cargo-demo-9">'
            '<h2 class="card-title">Cargo Demo</h2></div>')
    [o] = parse_listing(html)
    assert o.slug == "cargo-demo-9"


def test_listing_keeps_repeated_cards_for_the_crawler_to_dedupe():
    offers = parse_listing(listing_page(["a-1", "a-1", "b-2"]))
    assert [o.slug for o in offers] == ["a-1", "a-1", "b-2"]


@pytest.mark.parametrize("tags,expected", [
    (("Jornada Completa", "Presencial"), ("Jornada Completa", "Presencial")),
    (("Remoto", "Jornada Parcial"), ("Jornada Parcial", "Remoto")),   # order-independent
    (("Híbrido",), (None, "Híbrido")),
    (("Jornada Completa",), ("Jornada Completa", None)),
    (("Turnos", "Otro"), ("Turnos", "Otro")),                         # unknown: site order
    ((), (None, None)),
])
def test_workday_and_modality_tags(tags, expected):
    soup = BeautifulSoup(card("cargo-demo-1", tags=tags), "html.parser")
    o = parse_card(soup.select_one("div.job-offer-card"))
    assert (o.workday, o.modality) == expected


@pytest.mark.parametrize("href,slug", [
    ("/job_offers/cargo-demo", "cargo-demo"),
    ("https://www.takealuk.com/job_offers/x-y-z", "x-y-z"),
    ("/job_offers/x?ref=1", "x"),
    ("/job_offers/x#top", "x"),
    ("/job_offers/x/save_later", "x"),
    ("/job_offers/..", None),
    ("/job_offers/%2e%2e", None),
    ("/companies/empresa-demo-01", None),
    ("/job_offers?page=2", None),
    ("", None),
    (None, None),
])
def test_slug_from_href(href, slug):
    assert slug_from_href(href) == slug


@pytest.mark.parametrize("text,expected", [
    ("Las Condes, Santiago, Región Metropolitana, Chile", ("Metropolitana", "Las Condes", "Chile")),
    ("Rancagua, Cachapoal, O'Higgins, Chile", ("O'Higgins", "Rancagua", "Chile")),
    ("Calama, El Loa, Antofagasta, Chile", ("Antofagasta", "Calama", "Chile")),
    ("Valdivia, Los Ríos, Chile", ("Los Ríos", "Valdivia", "Chile")),
    ("Concepción, Concepción, Región del Biobío, Chile", ("Bío Bío", "Concepción", "Chile")),
    ("Región de Valparaíso, Chile", ("Valparaíso", None, "Chile")),   # region only
    ("Chile", (None, None, "Chile")),                                  # country only
    ("Miraflores, Lima, Perú", ("Lima", "Miraflores", "Perú")),       # other country: as-is
    ("Medellín, Antioquia, Colombia", ("Antioquia", "Medellín", "Colombia")),
    ("Providencia, Santiago", ("Santiago", "Providencia", None)),    # unknown region kept
    ("", (None, None, None)),
    (None, (None, None, None)),
])
def test_parse_location(text, expected):
    assert parse_location(text) == expected
