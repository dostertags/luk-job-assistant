"""CV seeding: pure text parsing, lazy PDF readers, and 'your answers file always wins'."""

from __future__ import annotations

import logging
import re

import pytest

from luk_assist import cv
from luk_assist.answers import Answers

FAKE_CV = """Paz Prueba
paz.prueba@example.com | +56 900000000

Resumen:
Analista comercial con 6 años de experiencia en Empresa Demo 01 SpA.
Foco en pricing y reportería.
Experiencia
Empresa Demo 01 SpA - Analista (2020 - hoy)

Título profesional: Ingeniería Comercial
Comuna: Comuna Demo
Idiomas: Español nativo, Inglés avanzado
"""


def test_profile_from_text_reads_only_what_is_there():
    profile = cv.profile_from_text(FAKE_CV)
    assert profile["experiencia"] == ("Analista comercial con 6 años de experiencia en Empresa Demo 01 SpA. "
                                      "Foco en pricing y reportería.")  # stops at the next heading
    assert profile["titulo"] == "Ingeniería Comercial"
    assert profile["comuna"] == "Comuna Demo"
    assert profile["idioma"] == "Español nativo, Inglés avanzado"
    assert set(profile) <= set(cv.PROFILE_KEYS)


def test_summary_heading_on_its_own_line_and_perfil_profesional():
    text = "Perfil profesional\n\nIngeniera en control de gestión.\n\nEducación\nUniversidad Demo"
    assert cv.profile_from_text(text) == {"experiencia": "Ingeniera en control de gestión."}


def test_an_empty_summary_section_takes_nothing_from_the_next_one():
    assert "experiencia" not in cv.profile_from_text("Resumen\nComuna: Comuna Demo\n")


def test_summary_is_capped_at_a_word_boundary():
    text = "Summary: " + "palabra " * 200
    summary = cv.profile_from_text(text)["experiencia"]
    assert len(summary) <= cv.MAX_SUMMARY and summary.endswith("palabra")


@pytest.mark.parametrize("text", [
    # Replaces the old "a mention of English means Inglés <level>" guess: that fabricated a language
    # level (a school name, a failed course or an explicit "No" all became "Inglés").
    "Inglés: No",
    "Colegio Inglés Saint George",
    "English for Business (no aprobado)",
    "Nivel de inglés avanzado en lectura",
    "English (C1)",
    "Cursos de inglés",
    "Idiomas: No",
    "Idiomas: ninguno",
    "Languages: none",
    "Idioma: sin inglés",
    "Languages: not applicable",
])
def test_idioma_only_from_an_explicit_languages_line_and_never_a_negation(text):
    assert "idioma" not in cv.profile_from_text(text)


@pytest.mark.parametrize("text,expected", [
    ("Idiomas: Español nativo, Inglés avanzado", "Español nativo, Inglés avanzado"),
    ("Idioma(s): Español nativo; Inglés intermedio", "Español nativo; Inglés intermedio"),
    ("Idioma: Portugués básico", "Portugués básico"),
    ("Languages: Spanish (native), English (C1)", "Spanish (native), English (C1)"),
    ("Idiomas: Español nativo, Inglés: no", "Español nativo"),
    ("Idiomas: Español nativo; sin inglés", "Español nativo"),
])
def test_explicit_languages_line(text, expected):
    assert cv.profile_from_text(text)["idioma"] == expected


@pytest.mark.parametrize("text", ["", None, "   \n\n", "Paz Prueba\npaz.prueba@example.com\n+56 900000000"])
def test_nothing_found_means_nothing_invented(text):
    assert cv.profile_from_text(text) == {}


def test_a_perfil_mention_is_not_a_heading():
    assert "experiencia" not in cv.profile_from_text("Perfil de LinkedIn: https://example.com/paz")


def test_merge_never_overrides_the_user():
    user = Answers(profile={"comuna": "Comuna Elegida"}, fixed={"salary": "1500000"})
    derived = {"comuna": "Comuna Demo", "experiencia": "Del CV.", "desconocida": "x", "titulo": ""}
    merged = cv.merge(user, derived)
    assert merged.profile == {"comuna": "Comuna Elegida", "experiencia": "Del CV."}
    assert merged.fixed == user.fixed
    assert user.profile == {"comuna": "Comuna Elegida"}  # the original is untouched
    assert cv.seeded_keys(user, derived) == ["experiencia"]


# --- text_from_pdf: lazy readers, never raises --------------------------------------------------
def _missing(_path):
    raise ImportError("not installed")


def test_text_from_pdf_missing_file_or_none():
    assert cv.text_from_pdf(None) == ""
    assert cv.text_from_pdf("does-not-exist.pdf") == ""


def test_text_from_pdf_without_any_reader_warns(tmp_path, monkeypatch, caplog):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(cv, "_READERS", (_missing, _missing, _missing))
    with caplog.at_level(logging.WARNING, logger="luk_assist.cv"):
        assert cv.text_from_pdf(pdf) == ""
    assert "luk-assist[pdf]" in caplog.text
    # luk-assist is not on PyPI: the hint installs from your checkout, never a bare package name
    assert 'python -m pip install -e "<path to luk-job-assistant>/luk-assist[pdf]"' in caplog.text
    assert not re.search(r"pip install\s+(?!-e\b|-U\b)[\"']?luk-assist", caplog.text)


def test_text_from_pdf_falls_through_to_the_next_reader(tmp_path, monkeypatch):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    def broken(_path):
        raise ValueError("corrupt")

    monkeypatch.setattr(cv, "_READERS", (_missing, broken, lambda _p: "Resumen: texto"))
    assert cv.text_from_pdf(pdf) == "Resumen: texto"
