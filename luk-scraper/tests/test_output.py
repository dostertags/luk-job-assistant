"""JSONL / CSV writers and readers (round trips, Excel BOM, CSV-injection guard)."""
from __future__ import annotations

import json

import pytest

from luk_scraper.models import FIELDS, Offer
from luk_scraper.output import (format_for, read_csv, read_jsonl, write, write_csv,
                                write_jsonl)


def _offers():
    return [
        Offer(slug="cargo-demo-1", title="Analista Demo", company="Empresa Demo 01 SpA",
              location_text="Las Condes, Santiago, Región Metropolitana, Chile",
              region="Metropolitana", comuna="Las Condes", country="Chile",
              workday="Jornada Completa", modality="Híbrido", posted_relative="Hace 2 horas",
              url="https://www.takealuk.com/job_offers/cargo-demo-1",
              description="Línea uno\n- ítem, con coma y \"comillas\"",
              salary_min=900000, salary_max=1200000, published_at="2026-09-23",
              valid_through="2026-12-22", external_id="9001",
              fetched_at="2026-09-24T12:00:00+00:00"),
        Offer(slug="cargo-demo-2", title="Supervisor Demo",
              url="https://www.takealuk.com/job_offers/cargo-demo-2",
              fetched_at="2026-09-24T12:00:01+00:00"),
    ]


def test_jsonl_round_trip(tmp_path):
    path = tmp_path / "offers.jsonl"
    assert write_jsonl(_offers(), path) == 2
    rows = read_jsonl(path)
    assert rows == [o.to_dict() for o in _offers()]
    assert list(rows[0]) == list(FIELDS)                     # key order
    assert [Offer.from_dict(r) for r in rows] == _offers()
    text = path.read_text(encoding="utf-8")
    assert "Región" in text and "\\u00f3" not in text        # real UTF-8, not escapes
    assert text.count("\n") == 2


def test_csv_round_trip_with_bom_for_excel(tmp_path):
    path = tmp_path / "offers.csv"
    assert write_csv(_offers(), path) == 2
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                   # utf-8-sig
    rows = read_csv(path)
    assert list(rows[0]) == list(FIELDS)
    assert [Offer.from_dict(r) for r in rows] == _offers()   # types restored
    assert rows[1]["salary_min"] == "" and rows[0]["salary_min"] == "900000"


@pytest.mark.parametrize("value", ["=HYPERLINK(\"https://example.com\")", "+1", "-2+3",
                                   "@SUM(A1)", "\tx"])
def test_csv_neutralises_formulas(tmp_path, value):
    path = tmp_path / "o.csv"
    write_csv([Offer(slug="cargo-demo-1", title=value)], path)
    assert read_csv(path)[0]["title"] == "'" + value
    write_jsonl([Offer(slug="cargo-demo-1", title=value)], tmp_path / "o.jsonl")
    assert read_jsonl(tmp_path / "o.jsonl")[0]["title"] == value   # JSONL stays exact


def test_write_picks_the_format_by_extension(tmp_path):
    assert write(_offers(), tmp_path / "a.jsonl") == 2
    assert write(_offers(), tmp_path / "b.NDJSON") == 2
    assert write(_offers(), tmp_path / "c.csv") == 2
    assert json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()[0])["slug"] \
        == "cargo-demo-1"
    assert (tmp_path / "c.csv").read_text(encoding="utf-8-sig").startswith("slug,title,")


@pytest.mark.parametrize("name", ["offers.xlsx", "offers.json", "offers"])
def test_unknown_extension_is_refused(tmp_path, name):
    with pytest.raises(ValueError):
        format_for(name)
    with pytest.raises(ValueError):
        write(_offers(), tmp_path / name)


def test_dicts_are_accepted_and_parent_dirs_created(tmp_path):
    path = tmp_path / "nested" / "dir" / "o.jsonl"
    write([_offers()[0].to_dict()], path)
    assert read_jsonl(path)[0]["slug"] == "cargo-demo-1"


def test_no_temporary_file_is_left_behind(tmp_path):
    write(_offers(), tmp_path / "o.csv")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.csv"]


def test_empty_output_still_has_a_csv_header(tmp_path):
    write([], tmp_path / "o.csv")
    assert read_csv(tmp_path / "o.csv") == []
    assert (tmp_path / "o.csv").read_text(encoding="utf-8-sig").startswith("slug,")


def test_from_dict_requires_a_slug():
    with pytest.raises(ValueError):
        Offer.from_dict({"title": "x"})
