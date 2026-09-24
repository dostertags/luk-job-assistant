"""Text helpers: regions (incl. the fixed "R" quirk), HTML, dates, integers (offline)."""
from __future__ import annotations

import pytest

from luk_scraper.text import (REGIONES, html_to_text, key, norm_region, normalize_space,
                              parse_date_any, strip_region_word, to_int)


def test_sixteen_regions():
    assert len(REGIONES) == 16 and len({key(r) for r in REGIONES}) == 16


@pytest.mark.parametrize("region", REGIONES)
def test_canonical_names_map_to_themselves(region):
    assert norm_region(region) == region
    assert norm_region("Región " + region) == region
    assert norm_region(region.upper()) == region


@pytest.mark.parametrize("raw,expected", [
    # The quirk being fixed: the old regex ^R\.?\s* ate the leading "R" of "Región ...",
    # leaving "egión X". Now "Región [de [la]]" is stripped first and an "R" prefix only when
    # a dot or whitespace follows.
    ("Región Metropolitana", "Metropolitana"),
    ("Region Metropolitana", "Metropolitana"),
    ("REGIÓN DE VALPARAÍSO", "Valparaíso"),
    ("Región de Valparaíso", "Valparaíso"),
    ("Región del Maule", "Maule"),
    ("Región de la Araucanía", "La Araucanía"),
    ("Región de Los Ríos", "Los Ríos"),
    ("Región de Ñuble", "Ñuble"),
    ("Región del Biobío", "Bío Bío"),
    ("Región del Libertador General Bernardo O'Higgins", "O'Higgins"),
    ("Región de Aysén del General Carlos Ibáñez del Campo", "Aysén"),
    ("Región de Magallanes y de la Antártica Chilena", "Magallanes y Antártica Chilena"),
    ("Región Metropolitana de Santiago", "Metropolitana"),
    ("R.Metropolitana", "Metropolitana"),
    ("R. Metropolitana", "Metropolitana"),
    ("R Maule", "Maule"),
    ("R.M.", "Metropolitana"),
    ("RM", "Metropolitana"),
    ("Bíobío", "Bío Bío"),
    ("Biobio", "Bío Bío"),
    ("Araucanía", "La Araucanía"),
    ("Aisén", "Aysén"),
    ("Magallanes", "Magallanes y Antártica Chilena"),
    ("Tarapaca", "Tarapacá"),
    ("  Los   Lagos ", "Los Lagos"),
    ("Región de Coquimbo", "Coquimbo"),        # decomposed (NFD) accent
])
def test_norm_region(raw, expected):
    assert norm_region(raw) == expected


@pytest.mark.parametrize("raw", [
    "Región",            # the word alone, not a region
    "Rancagua",          # a comuna starting with R: never stripped to "ancagua"
    "Santiago",          # a province/comuna, not a region name
    "Chile",             # a country (used to fuzzy-match "...antarticaCHILEna")
    "Lima",
    "",
    None,
])
def test_norm_region_unknown_is_none(raw):
    assert norm_region(raw) is None


def test_the_old_quirk_never_returns_a_mangled_name():
    for raw in ("Región Desconocida", "Region X", "Región de Ninguna Parte"):
        result = norm_region(raw)
        assert result is None or result in REGIONES
        assert not (result or "").lower().startswith("egi")


@pytest.mark.parametrize("raw,expected", [
    ("Región Metropolitana", "Metropolitana"),
    ("Region de Valparaíso", "Valparaíso"),
    ("Región del Maule", "Maule"),
    ("Región de la Araucanía", "Araucanía"),
    ("Rancagua", "Rancagua"),
    ("Regional Demo", "Regional Demo"),     # "Regional" is not the word "Región"
    (None, ""),
])
def test_strip_region_word(raw, expected):
    assert strip_region_word(raw) == expected


def test_key_and_normalize_space():
    assert key("Bío Bío") == key("Biobío") == "biobio"
    assert key("O'Higgins") == "ohiggins"
    assert key("Ñuble") == "nuble"
    assert key(None) == ""
    assert normalize_space("  a\n\t b\xa0c  ") == "a b c"


def test_html_to_text():
    html = ("<p>Cargo: Demo\n<br />Empresa: Empresa Demo 01 SpA</p><p>Texto &amp; más"
            "<br>\nTareas:</p>\n<ul>\n<li>Uno.</li>\n<li>Dos.</li>\n</ul>")
    text = html_to_text(html)
    assert text.splitlines() == ["Cargo: Demo", "Empresa: Empresa Demo 01 SpA",
                                 "Texto & más", "Tareas:", "- Uno.", "- Dos."]
    assert "<" not in text


@pytest.mark.parametrize("value", [None, "", "   ", 42, {"a": 1}])
def test_html_to_text_non_text(value):
    assert html_to_text(value) == ""


@pytest.mark.parametrize("value,expected", [
    ("2026-09-23", "2026-09-23"),
    ("2026-09-23T10:00:00-03:00", "2026-09-23"),
    ("23/09/26", "2026-09-23"),
    ("23/09/2026", "2026-09-23"),
    ("23/09/26 12:00", "2026-09-23"),
    ("2026-02-30", None),
    ("hace 2 días", None),
    ("", None),
    (None, None),
    (20260923, None),
])
def test_parse_date_any(value, expected):
    assert parse_date_any(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("1.200.000", 1200000),
    ("$1.200.000", 1200000),
    ("1,200,000", 1200000),
    ("1.200.000,50", 1200000),
    ("1200000.0", 1200000),
    (1200000.0, 1200000),
    (1200000, 1200000),
    ("1828", 1828),
    ("Entre 800.000 y 1.000.000", 800000),
    ("sin dato", None),
    ("", None),
    (None, None),
    (True, None),
    ({"value": 1}, None),
    (float("nan"), None),
])
def test_to_int(value, expected):
    assert to_int(value) == expected
