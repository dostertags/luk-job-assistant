"""Detail-page (JSON-LD JobPosting) parsing: fixture, salary shapes, odd inputs (offline)."""
from __future__ import annotations

import json

import pytest

from luk_scraper.models import DETAIL_FIELDS
from luk_scraper.parser import parse_detail


def _page(*objs) -> str:
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(o)}</script>'
                     for o in objs)
    return f"<html><head>{blocks}</head><body></body></html>"


def _job(**extra) -> dict:
    return {"@type": "JobPosting", "title": "Cargo Demo", "description": "<p>hola</p>", **extra}


def test_fixture_fields(detail_html):
    fields = parse_detail(detail_html)
    assert fields["published_at"] == "2026-09-23"        # datePosted
    assert fields["valid_through"] == "2026-12-22"       # validThrough
    assert fields["external_id"] == "9001"               # identifier.value (NOT the org's RUT)
    assert fields["description"].startswith("Cargo: Analista de Datos Demo - Chile")
    assert "<" not in fields["description"] and "&amp;" not in fields["description"]
    assert "- Preparar reportes de prueba." in fields["description"]   # list items kept
    assert "Requisito de ejemplo & conocimiento ficticio." in fields["description"]
    # hidden salary (baseSalary absent) -> no salary keys
    assert "salary_min" not in fields and "salary_max" not in fields
    assert set(fields) <= DETAIL_FIELDS


def test_employer_tax_id_is_never_read(detail_html):
    assert "11.111.111-0" not in json.dumps(parse_detail(detail_html))


def test_no_jsonld_or_no_jobposting_is_empty():
    assert parse_detail("<html><body>sin datos</body></html>") == {}
    assert parse_detail("") == {}
    assert parse_detail(None) == {}
    assert parse_detail(_page({"@type": "BreadcrumbList", "itemListElement": []})) == {}


def test_malformed_jsonld_block_is_skipped():
    html = ('<script type="application/ld+json">{not json</script>'
            + _page(_job(datePosted="2026-09-01")))
    assert parse_detail(html)["published_at"] == "2026-09-01"


@pytest.mark.parametrize("base_salary,expected", [
    ({"value": {"minValue": 500000, "maxValue": 900000}}, (500000, 900000)),   # range
    ({"value": {"value": 700000}}, (700000, None)),          # QuantitativeValue, single value
    ({"value": 1200000}, (1200000, None)),                    # scalar Number
    ({"value": 1200000.0}, (1200000, None)),                  # scalar float (no x10 bug)
    ({"value": "1.200.000"}, (1200000, None)),                # scalar Text, CL thousands
    ({"value": "$ 800.000 a $ 1.000.000"}, (800000, None)),   # text range: first number only
    ([{"value": {"minValue": 600000}}], (600000, None)),      # baseSalary as a LIST
    ([], (None, None)),                                       # empty list
    ({"minValue": 400000, "maxValue": 450000}, (400000, 450000)),  # MonetaryAmount min/max
    (950000, (950000, None)),                                 # bare scalar baseSalary
    ({"value": {"minValue": 900000, "maxValue": 500000}}, (500000, 900000)),  # swapped
    ({"value": {"minValue": 0, "maxValue": 0}}, (None, None)),  # zero = not published
    ({"value": {"value": {"weird": 1}}}, (None, None)),       # nested nonsense
    ({"value": True}, (None, None)),
    (None, (None, None)),                                     # absent (hidden salary)
])
def test_salary_shapes_never_crash(base_salary, expected):
    job = _job()
    if base_salary is not None:
        job["baseSalary"] = base_salary
    fields = parse_detail(_page(job))
    assert (fields.get("salary_min"), fields.get("salary_max")) == expected
    assert fields["description"] == "hola"          # description always survives


@pytest.mark.parametrize("description", [{"raro": "obj"}, ["a", "b"], 42, None, "   ", ""])
def test_non_string_or_blank_description_is_ignored(description):
    fields = parse_detail(_page(_job(description=description, datePosted="2026-09-02")))
    assert "description" not in fields
    assert fields["published_at"] == "2026-09-02"


@pytest.mark.parametrize("identifier,expected", [
    ({"@type": "PropertyValue", "value": "123"}, "123"),
    ({"@type": "PropertyValue", "value": 123}, "123"),
    ("456", "456"),
    ([{"value": "789"}], "789"),
    ({"value": None}, None),
    ({"value": {"x": 1}}, None),
    (True, None),
])
def test_identifier_shapes(identifier, expected):
    assert parse_detail(_page(_job(identifier=identifier))).get("external_id") == expected


def test_dates_accept_datetimes_and_reject_nonsense():
    fields = parse_detail(_page(_job(datePosted="2026-09-23T10:00:00-03:00",
                                     validThrough="2026-13-45")))
    assert fields["published_at"] == "2026-09-23"
    assert "valid_through" not in fields


def test_graph_container_and_type_list():
    page = _page({"@context": "https://schema.org",
                  "@graph": [{"@type": "Organization", "name": "Empresa Demo 01 SpA"},
                             _job(**{"@type": ["JobPosting"], "datePosted": "2026-09-05"})]})
    assert parse_detail(page)["published_at"] == "2026-09-05"


def test_first_jobposting_wins():
    page = _page(_job(identifier="1"), _job(identifier="2"))
    assert parse_detail(page)["external_id"] == "1"


def test_only_detail_fields_are_returned():
    fields = parse_detail(_page(_job(employmentType="FULL_TIME", title="X",
                                     hiringOrganization={"name": "Empresa Demo 01 SpA"})))
    assert set(fields) <= DETAIL_FIELDS
    assert "employmentType" not in fields and "company" not in fields
