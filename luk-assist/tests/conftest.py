"""Shared fakes. Offline only: no network, no Playwright, no real PDF, fictional people and companies."""

from __future__ import annotations

from pathlib import Path

import pytest

from luk_assist.answers import Answers
from luk_assist.browser import Field

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
SRC_DIR = PACKAGE_ROOT / "src" / "luk_assist"

# The three free-text questions every Luk application form asks (labels as shown on takealuk.com).
RESIDENCE = "Indica tu lugar de residencia (calle, comuna, ciudad)"
SALARY = "¿Cuál es tu expectativa de renta mensual líquida?"
EXPERIENCE = "Comenta tu experiencia relacionada al cargo"


def luk_fields() -> list[Field]:
    return [
        Field(name="residence", label=RESIDENCE, kind="text"),
        Field(name="salary", label=SALARY, kind="text"),
        Field(name="experience", label=EXPERIENCE, kind="textarea"),
    ]


class FakeBrowser:
    """Implements FormBrowser (open / detect_fields / fill) and records every call. No submit exists."""

    def __init__(self, fields: list[Field] | None = None, fail_on: set[str] | None = None,
                 no_form_on: set[str] | None = None) -> None:
        self._fields = list(fields) if fields is not None else luk_fields()
        self._fail_on = fail_on or set()
        self._no_form_on = no_form_on or set()  # URL parts whose page shows no application form
        self.opened: list[str] = []
        self.filled: list[tuple[str, str]] = []

    def open(self, url: str) -> bool:
        self.opened.append(url)
        return not any(part in url for part in self._no_form_on)

    def detect_fields(self) -> list[Field]:
        return list(self._fields)

    def fill(self, field: Field, value: str) -> None:
        if field.name in self._fail_on:
            raise RuntimeError("element detached")
        self.filled.append((field.name, value))


class StrictBrowser(FakeBrowser):
    """A FakeBrowser that fails the test if anything but the three FormBrowser methods is touched."""

    _ALLOWED = {"open", "detect_fields", "fill"}

    def __getattribute__(self, name: str):
        if not name.startswith("_") and name not in StrictBrowser._ALLOWED and name not in ("opened", "filled"):
            raise AssertionError(f"orchestration used a non-FormBrowser attribute: {name}")
        return object.__getattribute__(self, name)


@pytest.fixture
def paz_answers() -> Answers:
    """Answers of a fictional candidate (Paz Prueba)."""
    return Answers(
        profile={
            "comuna": "Calle Falsa 123, Comuna Demo, Santiago",
            "experiencia": "6 años en análisis comercial en Empresa Demo 01 SpA.",
            "titulo": "Ingeniería Comercial",
        },
        per_question={},
        fixed={"salary": "1.500.000", "availability": "Inmediata"},
    )


@pytest.fixture
def fake_browser() -> FakeBrowser:
    return FakeBrowser()
