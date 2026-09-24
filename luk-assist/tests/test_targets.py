"""Listings -> targets: JSONL / CSV / JSON / TXT, slugs, apply URLs, dedupe and limit."""

from __future__ import annotations

import json

import pytest

from luk_assist.targets import Target, TargetsError, apply_url, dedupe, load_targets, slug_from, target_from_row


@pytest.mark.parametrize("value,slug", [
    ("https://www.takealuk.com/job_offers/analista-demo-01?tab=apply", "analista-demo-01"),
    ("https://www.takealuk.com/job_offers/analista-demo-01/external_application_form", "analista-demo-01"),
    ("/job_offers/analista-demo-01", "analista-demo-01"),
    ("analista-demo-01", "analista-demo-01"),
    ("  analista-demo-01 \n", "analista-demo-01"),
    ("", None),
    (None, None),
    ("https://example.com/some/page", None),
    ("javascript:alert(1)", None),
    ("../etc/passwd", None),
    ("slug with spaces", None),
])
def test_slug_from(value, slug):
    assert slug_from(value) == slug


def test_apply_url():
    assert apply_url("analista-demo-01") == "https://www.takealuk.com/job_offers/analista-demo-01?tab=apply"
    assert Target("analista-demo-01").url == apply_url("analista-demo-01")
    with pytest.raises(ValueError):
        apply_url("https://example.com/x")


def test_load_jsonl_from_the_scraper(tmp_path):
    p = tmp_path / "offers.jsonl"
    rows = [
        {"slug": "analista-demo-01", "url": "https://www.takealuk.com/job_offers/analista-demo-01",
         "title": "Analista Demo", "company": "Empresa Demo 01 SpA"},
        {"url": "https://www.takealuk.com/job_offers/ejecutivo-demo-02", "title": "Ejecutivo Demo",
         "company": {"name": "Empresa Demo 02 SpA"}},
        {"slug": "analista-demo-01", "title": "duplicate"},
        {"title": "no slug at all"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n\nnot json\n", encoding="utf-8")
    targets = load_targets(p)
    assert targets == [
        Target("analista-demo-01", "Analista Demo", "Empresa Demo 01 SpA"),
        Target("ejecutivo-demo-02", "Ejecutivo Demo", "Empresa Demo 02 SpA"),
    ]


def test_load_csv_with_bom_and_odd_headers(tmp_path):
    p = tmp_path / "offers.csv"
    p.write_text(
        "﻿URL,Title,Company\n"
        "https://www.takealuk.com/job_offers/analista-demo-01,Analista Demo,Empresa Demo 01 SpA\n"
        "/job_offers/ejecutivo-demo-02,Ejecutivo Demo,Empresa Demo 02 SpA\n"
        ",sin slug,Empresa Demo 03 SpA\n",
        encoding="utf-8",
    )
    targets = load_targets(p)
    assert [t.slug for t in targets] == ["analista-demo-01", "ejecutivo-demo-02"]
    assert targets[0].company == "Empresa Demo 01 SpA"


def test_load_txt_one_url_or_slug_per_line(tmp_path):
    p = tmp_path / "slugs.txt"
    p.write_text("# my picks\n\nanalista-demo-01\nhttps://www.takealuk.com/job_offers/ejecutivo-demo-02?tab=apply\n"
                 "https://example.com/not-luk\nanalista-demo-01\n", encoding="utf-8")
    assert [t.slug for t in load_targets(p)] == ["analista-demo-01", "ejecutivo-demo-02"]


def test_load_luk_cli_json_envelope(tmp_path):
    p = tmp_path / "search.json"
    p.write_text(json.dumps({
        "schema_version": 2, "kind": "job_search", "warnings": [],
        "data": {"results": [
            {"slug": "analista-demo-01", "url": "https://www.takealuk.com/job_offers/analista-demo-01",
             "title": "Analista Demo", "company": "Empresa Demo 01 SpA"},
        ]},
    }), encoding="utf-8")
    assert load_targets(p) == [Target("analista-demo-01", "Analista Demo", "Empresa Demo 01 SpA")]


def test_load_plain_json_list(tmp_path):
    p = tmp_path / "offers.json"
    p.write_text(json.dumps([{"slug": "analista-demo-01"}, {"slug": "ejecutivo-demo-02"}]), encoding="utf-8")
    assert [t.slug for t in load_targets(p)] == ["analista-demo-01", "ejecutivo-demo-02"]


def test_limit_applies_after_dedupe(tmp_path):
    p = tmp_path / "slugs.txt"
    p.write_text("a-1\na-1\nb-2\nc-3\n", encoding="utf-8")
    assert [t.slug for t in load_targets(p, limit=2)] == ["a-1", "b-2"]


def test_dedupe_keeps_the_first():
    assert dedupe([Target("a-1", "first"), Target("b-2"), Target("a-1", "second")]) == [
        Target("a-1", "first"), Target("b-2")]


def test_target_from_row_prefers_a_valid_slug_column():
    assert target_from_row({"slug": "not a slug", "detail_url": "/job_offers/analista-demo-01"}).slug == \
        "analista-demo-01"
    assert target_from_row({"title": "x"}) is None


# --- encodings: Windows PowerShell 5.1 `luk ... --json > offers.json` writes UTF-16 LE with a BOM ------
LUK_CLI_SEARCH = {
    "schema_version": 2, "kind": "job_search", "warnings": [],
    "data": {"results": [
        {"slug": "analista-demo-01", "url": "https://www.takealuk.com/job_offers/analista-demo-01",
         "title": "Analista de Diseño", "company": "Empresa Demo 01 SpA"},
        {"slug": "ejecutivo-demo-02", "title": "Ejecutivo Comercial", "company": "Empresa Demo 02 SpA"},
    ]},
}
BOMS = {
    "utf-8-sig": b"",  # the codec writes its own BOM
    "utf-16-le": b"\xff\xfe",
    "utf-16-be": b"\xfe\xff",
    "utf-32-le": b"\xff\xfe\x00\x00",
    "utf-32-be": b"\x00\x00\xfe\xff",
}


@pytest.mark.parametrize("encoding", list(BOMS))
def test_luk_cli_json_in_any_bom_encoding(tmp_path, encoding):
    p = tmp_path / "offers.json"
    text = json.dumps(LUK_CLI_SEARCH, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    p.write_bytes(BOMS[encoding] + text.encode(encoding))
    assert load_targets(p) == [Target("analista-demo-01", "Analista de Diseño", "Empresa Demo 01 SpA"),
                               Target("ejecutivo-demo-02", "Ejecutivo Comercial", "Empresa Demo 02 SpA")]


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le"])
def test_jsonl_in_utf16_or_utf32_with_bom(tmp_path, encoding):
    p = tmp_path / "offers.jsonl"
    rows = LUK_CLI_SEARCH["data"]["results"]
    text = "\r\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\r\n"
    p.write_bytes(BOMS[encoding] + text.encode(encoding))
    assert [t.slug for t in load_targets(p)] == ["analista-demo-01", "ejecutivo-demo-02"]
    assert load_targets(p)[0].title == "Analista de Diseño"


def test_jsonl_keeps_a_line_separator_inside_a_string(tmp_path):
    p = tmp_path / "offers.jsonl"
    row = {"slug": "analista-demo-01", "title": "Analista Demo"}
    p.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert [t.slug for t in load_targets(p)] == ["analista-demo-01"]


def test_txt_in_utf16(tmp_path):
    p = tmp_path / "slugs.txt"
    p.write_bytes(b"\xff\xfe" + "# mis ofertas\r\nanalista-demo-01\r\nejecutivo-demo-02\r\n".encode("utf-16-le"))
    assert [t.slug for t in load_targets(p)] == ["analista-demo-01", "ejecutivo-demo-02"]


# --- CSV from Excel: ';' in Spanish locales, tabs, and cp1252 ----------------------------------------
@pytest.mark.parametrize("sep", [";", "\t", ","])
def test_csv_delimiter_is_sniffed(tmp_path, sep):
    p = tmp_path / "offers.csv"
    lines = ["url", "title", "company"], \
        ["https://www.takealuk.com/job_offers/analista-demo-01", "Analista, Senior", "Empresa Demo 01 SpA"], \
        ["/job_offers/ejecutivo-demo-02", "Ejecutivo Demo", "Empresa Demo 02 SpA"]
    rows = [sep.join(f'"{c}"' if sep in c or "," in c else c for c in line) for line in lines]
    p.write_text("\r\n".join(rows) + "\r\n", encoding="utf-8")
    targets = load_targets(p)
    assert [t.slug for t in targets] == ["analista-demo-01", "ejecutivo-demo-02"]
    assert targets[0].title == "Analista, Senior" and targets[0].company == "Empresa Demo 01 SpA"


def test_csv_saved_by_excel_in_cp1252(tmp_path):
    p = tmp_path / "offers.csv"
    p.write_bytes("slug;título;empresa\r\nanalista-demo-01;Técnico en Diseño;Compañía Demo 01 SpA\r\n"
                  .encode("cp1252"))
    targets = load_targets(p)
    assert [t.slug for t in targets] == ["analista-demo-01"]
    assert targets[0].company == "Compañía Demo 01 SpA"


def test_csv_utf8_with_bom_and_semicolons(tmp_path):
    p = tmp_path / "offers.csv"
    p.write_text("slug;title;company\nanalista-demo-01;Analista de Diseño;Empresa Demo 01 SpA\n",
                 encoding="utf-8-sig")
    assert load_targets(p) == [Target("analista-demo-01", "Analista de Diseño", "Empresa Demo 01 SpA")]


@pytest.mark.parametrize("name,content", [("missing.jsonl", None), ("bad.json", "{oops"), ("bad.json", '{"data": 3}')])
def test_unreadable_listings_are_an_error(tmp_path, name, content):
    p = tmp_path / name
    if content is not None:
        p.write_text(content, encoding="utf-8")
    with pytest.raises(TargetsError):
        load_targets(p)
