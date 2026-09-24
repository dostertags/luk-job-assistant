"""Capture scrubber (spec §8.1): worst case, header identity, fail-closed check, structure kept."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from luk_cli import parsers
from luk_cli.errors import ScrubFailed
from luk_cli.inputs import fold
from luk_cli.scrub import Identity, find_identity, identity_strings, scrub

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.takealuk.com"
PAZ = Identity(name="Paz Íñiguez Sentinela", email="paz.iniguez@sentinela.example")
SENTINELS = [
    "SENTINEL-CSRF-META", "SENTINEL-CSP-NONCE", "SENTINEL-AUTH-TOKEN", "SENTINEL-ONE-TAP", "SENTINEL-AMP-USER",
    "SENTINEL-AMP-DEVICE", "SENTINEL-STREAM", "SENTINELBLOBKEY", "SENTINELAMZSIG", "SENTINELAMZCRED",
    "SENTINELSIG1", "SENTINELSIGNATURE", "SENTINELTOKEN", "SENTINELSIGNEDBLOB", "SENTINELDIGEST",
    "SENTINEL-GOOGLE-AVATAR", "SENTINEL-LICDN", "sentinel-linkedin-handle", "SENTINEL-USER-ATTR",
    "SENTINEL-EMAIL-ATTR", "SENTINEL-PHONE-ATTR", "SENTINEL-RUT-ATTR", "SENTINEL-AVATAR-ATTR",
    "SENTINEL-INPUT-VALUE", "SENTINEL-TEXTAREA", "SENTINEL CV Final", "SENTINEL_CV_2026", "SENTINEL-SCRIPT-BODY",
    "SENTINEL-COMMENT", "contacto.sentinel@empresa.example", "reclutamiento.sentinel", "12.345.678-0", "9876543-K",
    "76.543.210-K", "+56 9 0000 0001", "900000002", "Ñuñoa",
]


def worst_case() -> str:
    return (FIXTURES / "synthetic" / "scrub_worst_case.html").read_text(encoding="utf-8")


def public(name: str) -> str:
    return (FIXTURES / "public" / name).read_text(encoding="utf-8")


def test_identity_strings_cover_the_variants() -> None:
    strings = identity_strings(Identity(name="José Ñúñez del Río", email="jose.nunez@example.org"))
    keys = {fold(s) for s in strings}
    assert {"jose nunez del rio", "jose.nunez@example.org", "jose.nunez"} <= keys
    assert {"jose-nunez-del-rio", "jose_nunez_del_rio", "josenunezdelrio"} <= keys
    assert {"jose", "nunez", "rio"} <= keys
    assert "del" not in keys  # a name particle would match ordinary Spanish text everywhere
    assert strings == sorted(strings, key=len, reverse=True)
    assert identity_strings(Identity(name=None, email=None)) == []


def test_identity_strings_skip_a_short_email_local_part() -> None:
    assert {fold(s) for s in identity_strings(Identity(name=None, email="jp@example.org"))} == {"jp@example.org"}


def test_find_identity_is_case_and_accent_insensitive_on_word_boundaries() -> None:
    who = Identity(name="Ana Íñiguez", email=None)
    assert find_identity("<p>Analista</p><p>Anaís</p>", who) == []
    assert find_identity("<p>ANA INIGUEZ</p>", who) == ["text in <p>"]
    assert find_identity('<a title="por iñiguez">x</a>', who) == ["attribute title on <a>"]
    assert find_identity('<a href="/p?n=I%C3%B1iguez">x</a>', who) == ["attribute href on <a>"]
    assert find_identity('<div data-ana-x="1"></div>', who) == ["markup outside text and attribute values"]
    assert find_identity('<div data-ana="Ana"></div>', who) == ["markup outside text and attribute values"]
    assert find_identity('<a href="/f/cv_ana_iniguez.pdf">x</a>', who) == ["attribute href on <a>"]


@pytest.mark.synthetic
def test_worst_case_page_leaves_no_sentinel_and_no_identity() -> None:
    out = scrub(worst_case(), path="/profile/cvs", identity=PAZ)
    lowered = out.casefold()
    survivors = [s for s in SENTINELS if s.casefold() in lowered]
    assert survivors == []
    assert find_identity(out, PAZ) == []
    for token in ("iniguez", "sentinela", "paziniguez"):
        assert token not in fold(out)


@pytest.mark.synthetic
def test_worst_case_page_keeps_its_structure() -> None:
    tree = HTMLParser(scrub(worst_case(), path="/profile/cvs", identity=PAZ))
    assert tree.css_first("turbo-frame#profile_frame div.cv-card p.cv-card__name").text() == "cv-1.pdf"
    assert tree.css_first("a[download]").attributes["download"] == "cv-2.docx"
    assert tree.css_first("button.header-avatar").attributes["aria-label"] == "Avatar de SCRUBBED"
    assert tree.css_first('meta[name="csrf-token"]').attributes["content"] == "SCRUBBED"
    assert tree.css_first('input[name="authenticity_token"]').attributes["value"] == "SCRUBBED"
    assert tree.css_first("textarea").text() == "SCRUBBED"
    scripts = tree.css("script")
    assert scripts[0].text() == ""
    ld = json.loads(scripts[1].text())
    assert ld["@type"] == "JobPosting" and ld["title"] == "Analista de Datos"
    assert ld["hiringOrganization"]["identifier"]["value"] == "SCRUBBED"
    assert "<" not in scripts[1].text()  # re-serialised JSON keeps Rails' < escaping


@pytest.mark.synthetic
def test_identity_shown_in_the_header_is_scrubbed_even_without_meta() -> None:
    out = scrub(worst_case(), path="/profile/cvs", identity=Identity(name=None, email=None))
    assert find_identity(out, PAZ) == []


def test_inputs_are_only_scrubbed_on_profile_pages() -> None:
    html = '<form><input name="q" value="contador"><textarea>nota</textarea></form>'
    kept = HTMLParser(scrub(html, path="/saved_jobs", identity=PAZ))
    assert (kept.css_first("input").attributes["value"], kept.css_first("textarea").text()) == ("contador", "nota")
    gone = HTMLParser(scrub(html, path="/profile/application_histories?page=2", identity=PAZ))
    assert (gone.css_first("input").attributes["value"], gone.css_first("textarea").text()) == ("SCRUBBED", "SCRUBBED")


def test_fail_closed_names_the_location_without_the_identity() -> None:
    html = '<html><body><div data-sentinela-flag="1">hola</div></body></html>'
    with pytest.raises(ScrubFailed) as info:
        scrub(html, path="/profile/cvs", identity=PAZ)
    message = info.value.message
    assert "markup outside text and attribute values" in message
    assert "sentinela" not in message.casefold()
    assert info.value.exit_code == 1


def test_public_captures_still_parse_after_scrubbing() -> None:
    search = scrub(public("search_analista.html"), path="/job_offers?job_positions=analista", identity=PAZ)
    result = parsers.parse_search(search, page=1, today=date(2026, 9, 23), base_url="https://www.takealuk.com", capture_path="/job_offers")
    assert (result.total, len(result.cards), result.pagination.last_page) == (273, 15, 19)
    assert result.cards[0].salary is not None and result.cards[0].salary.min == 650000

    detail = scrub(public("detail.html"), path="/job_offers/analista-demo-01-empresa-demo-01", identity=PAZ)
    posting = parsers.parse_job(
        detail, slug="analista-demo-01-empresa-demo-01", base_url="https://www.takealuk.com",
        today=date(2026, 9, 23), capture_path="/job_offers/x",
    ).value
    assert (posting.offer_id, posting.company_slug, posting.vacancies) == (10001, "empresa-demo-01", 1)
    assert posting.description_text.startswith("Párrafo de ejemplo 1")


def test_value_rules_keep_structure_and_ordinary_numbers() -> None:
    html = (
        '<div class="job-offer-show-page--with-apply-banner"><svg><path d="M 1.111.111-1.2z"></path></svg>'
        "<p>Oferta 19000000020 · RUT 11.111.111-0 · +56 9 0000 0003</p>"
        '<a href="/x?page=2&amp;token=abc&amp;X-Amz-Date=1">x</a></div>'
    )
    tree = HTMLParser(scrub(html, path="/job_offers", identity=Identity(name=None, email=None)))
    assert tree.css_first("div").attributes["class"] == "job-offer-show-page--with-apply-banner"
    assert tree.css_first("path").attributes["d"] == "M 1.111.111-1.2z"
    assert tree.css_first("p").text() == "Oferta 19000000020 · RUT SCRUBBED · SCRUBBED"
    assert tree.css_first("a").attributes["href"] == "/x?page=2&token=SCRUBBED&X-Amz-Date=SCRUBBED"


def test_scripts_json_ld_and_file_names() -> None:
    html = (
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">{"title":"Contador & Auditor"}</script>'
        '<p>Mi CV.pdf</p><a download="Mi CV.pdf" title="otro.DOCX">a</a><a download="cv">b</a>'
        '<input type="file" accept=".pdf,.doc,.docx"><a href="/files/cv.pdf">c</a>'
    )
    tree = HTMLParser(scrub(html, path="/profile/cvs", identity=PAZ))
    broken, kept = tree.css("script")
    assert (broken.text(), kept.text()) == ("", '{"title":"Contador & Auditor"}')  # unchanged JSON-LD is not rewritten
    first, second, third = tree.css("a")
    assert tree.css_first("p").text() == "cv-1.pdf"
    assert (first.attributes["download"], first.attributes["title"]) == ("cv-1.pdf", "cv-2.docx")
    assert second.attributes["download"] == "SCRUBBED"
    assert tree.css_first("input").attributes["accept"] == ".pdf,.doc,.docx"
    assert third.attributes["href"] == "/files/cv.pdf"


@pytest.mark.parametrize(
    ("name", "parse"),
    [
        ("search_analista.html", lambda h: parsers.parse_search(h, page=1, today=date(2026, 9, 23), base_url=BASE, capture_path="/")),
        ("search_worldwide.html", lambda h: parsers.parse_search(h, page=1, today=date(2026, 9, 23), base_url=BASE, capture_path="/")),
        ("detail.html", lambda h: parsers.parse_job(h, slug="x", base_url=BASE, today=date(2026, 9, 23), capture_path="/")),
        ("companies_q.html", lambda h: parsers.parse_companies(h, base_url=BASE, capture_path="/")),
        ("company_paginated.html", lambda h: parsers.parse_company(h, slug="x", page=1, base_url=BASE, today=date(2026, 9, 23), capture_path="/")),
        ("root.html", parsers.parse_algolia_config),
    ],
)
def test_scrubbing_a_public_page_changes_nothing_its_parser_reads(name, parse) -> None:
    html = public(name)
    assert parse(scrub(html, path="/job_offers", identity=Identity(name=None, email=None))) == parse(html)


@pytest.mark.synthetic
@pytest.mark.parametrize(
    ("name", "path", "parse"),
    [
        ("saved_jobs.html", "/saved_jobs", lambda h: parsers.parse_saved_jobs(h, today=date(2026, 9, 23), base_url=BASE)),
        ("application_histories.html", "/profile/application_histories", lambda h: parsers.parse_applications(h, base_url=BASE)),
        ("cvs.html", "/profile/cvs", parsers.parse_cvs),
    ],
)
def test_scrubbing_a_private_page_changes_nothing_its_parser_reads(name, path, parse) -> None:
    html = (FIXTURES / "synthetic" / name).read_text(encoding="utf-8")
    scrubbed = scrub(html, path=path, identity=Identity(name="Paz Prueba", email="paz.prueba@example.com"))
    assert parse(scrubbed) == parse(html)
    assert "prueba" not in fold(scrubbed)
