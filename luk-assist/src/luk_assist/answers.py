"""Answer engine: maps a form question (its label) to YOUR answer, offline, by keyword rules.

The answers file (JSON, default ``<user config dir>/luk-assist/answers.json``, outside any repository)
has three parts, all optional:

    {
      "profile":      {"titulo": "...", "experiencia": "...", "comuna": "...", "idioma": "...",
                       "edad": "...", "licencia": "...", "movilizacion": "..."},
      "fixed":        {"salary": "1500000", "availability": "...", "cover_letter": "...",
                       "generic": "..."},
      "per_question": {"part of a question label": "your exact answer"}
    }

How a label is answered (first hit wins):
  1. ``per_question``: its key appears in the label (case- and accent-insensitive substring).
  2. Salary: ``fixed.salary`` (digits only) goes ONLY into a question about your EXPECTED pay: one that
     names pay (renta, sueldo, salario, remuneración, pretensión, líquido...) AND an expectation
     (expectativa, pretensión, aspiración, esperas / esperada, "cuánto quieres ganar"), and that is not
     about your experience with payroll (experiencia, manejo, conocimiento, cálculo, "área de",
     describe, comenta) nor about your current, last or gross pay (actual, último, bruta / bruto).
     Current, last, gross or unqualified pay questions stay blank unless a per_question entry answers them.
  3. ``RULES``: the first rule with a keyword that starts a word of the label names the target
     (a profile key, or availability / cover letter).
  4. Otherwise nothing, unless the caller opts in to the generic sentence (``--fill-generic``).

Honesty: nothing is ever fabricated. Defaults are empty; a field is filled only when a rule or a
per-question entry matches AND your value for it is non-empty. Values written as ``<...>`` are
placeholders (the template uses them) and count as empty. Password, PIN and one-time-code fields are
never answered; identity and contact fields (RUT, e-mail, phone) only from an explicit per_question
entry. This module only decides text; it never touches a browser.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

log = logging.getLogger("luk_assist.answers")

APP_NAME = "luk-assist"

# Target tokens for the "fixed" answers; every other rule target is a profile key.
SALARY = "salary"
AVAILABILITY = "availability"
COVER_LETTER = "cover_letter"
GENERIC = "generic"
# A pay question luk-assist never answers by rule (current, last, gross or unqualified pay, or a
# payroll question no other rule answers): always blank, never the generic sentence.
SALARY_OTHER = "salary_other"

PROFILE_KEYS = ("titulo", "experiencia", "comuna", "idioma", "edad", "licencia", "movilizacion")
FIXED_KEYS = (SALARY, AVAILABILITY, COVER_LETTER, GENERIC)

# Used only with --fill-generic and only for free-text areas; it states no fact about you.
DEFAULT_GENERIC = "Puedo ampliar esta información en una entrevista."

# Pay questions (normalized text). Whole words for the topic, so "rentabilidad" is not about your pay;
# word prefixes for the cues that make luk-assist leave a pay question blank.
_W = r"(?<![a-z0-9])"
_PAY_TOPIC = re.compile(_W + r"(?:rentas?|sueldos?|salari(?:o|os|al|ales)|remuneracion(?:es)?|"
                        r"pretension(?:es)?|liquid[oa]s?|brut[oa]s?|cuanto quieres ganar|"
                        r"expectativas? economicas?)(?![a-z0-9])")
_PAY_EXPECTATION = re.compile(_W + r"(?:expectativa|pretension|aspiracion|esperas|esperad[oa]|"
                              r"cuanto quieres ganar|renta esperada)")
_PAY_EXPERIENCE = re.compile(_W + r"(?:experiencia|manejo|conocimiento|calculo|area de|describ|coment)")
_PAY_NOT_EXPECTED = re.compile(_W + r"(?:actual|ultim|brut)")

# (keywords, target). Keywords are normalized (lowercase, no accents) and must START a word of the
# normalized label ("area" does not match "tareas"). Evaluated in order: the first rule that matches wins.
# Pay questions are decided before these rules (see `match_rule`).
RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("disponibilidad", "cuando podrias", "cuando puede", "fecha de inicio", "incorporacion",
      "ingreso inmediato"), AVAILABILITY),
    (("carta", "presentacion", "motivacion", "por que te interesa", "por que deberiamos",
      "cuentanos sobre ti", "mensaje"), COVER_LETTER),
    (("titulo", "profesion", "carrera", "estudios", "formacion", "grado", "tecnico o profesional",
      "nivel educacional"), "titulo"),
    (("experiencia", "anos en el", "trayectoria", "desempenado", "te has desempenado", "area"),
     "experiencia"),
    (("edad", "cuantos anos tienes", "que edad"), "edad"),
    (("licencia",), "licencia"),
    (("ingles", "idioma", "portugues"), "idioma"),
    (("comuna", "residencia", "donde vives", "vivienda", "resides", "sector", "ubicacion",
      "direccion"), "comuna"),
    (("movilizacion", "vehiculo", "auto propio", "locomocion"), "movilizacion"),
)

# Credential fields are never filled, whatever the answers file says: passwords, PINs, one-time and
# verification codes, tokens and card security codes.
_CREDENTIAL = re.compile(r"(password|passwd|\bpwd\b|contrasena|\bclave\b|\bpin\b|\botp\b|2fa|"
                         r"codigo de verificacion|codigo de \d|codigo enviado|\btoken|security code|"
                         r"one[- ]time|\bcvv\b|\bcvc\b)")
# Identity and contact fields are filled only from an explicit per_question entry, never by a rule
# or the generic sentence (Luk usually fills them from your account).
_CONTACT = re.compile(r"(\brut\b|\bdni\b|pasaporte|correo|e-?mail|telefono|celular|whatsapp)")

_PLACEHOLDER = re.compile(r"^<[^<>]*>$", re.S)

TEMPLATE: dict[str, Any] = {
    "_help": (
        "luk-assist answers. Fill in only what is true for you. Empty values and <placeholders> are "
        "never typed into a form. per_question keys match (ignoring case and accents) any question "
        "label that contains them. Keep this file out of any repository."
    ),
    "profile": {
        "titulo": "<your degree or profession, e.g. Ingeniería Comercial>",
        "experiencia": "<two to four sentences about your relevant experience>",
        "comuna": "<where you live: street (optional), comuna, city>",
        "idioma": "<languages and level, e.g. Español nativo; inglés intermedio>",
        "edad": "<your age, only if you want to share it>",
        "licencia": "<driving licence, e.g. Sí, clase B>",
        "movilizacion": "<own transport, e.g. Sí, movilización propia>",
    },
    "fixed": {
        "salary": "<expected monthly net salary in CLP, digits only>",
        "availability": "<e.g. Inmediata, or 30 días>",
        "cover_letter": "<a short cover letter>",
        "generic": "<optional neutral sentence, used only with --fill-generic>",
    },
    "per_question": {
        "<part of a question you see often>": "<your exact answer>",
    },
}


class AnswersError(Exception):
    """The answers file exists but cannot be used (bad JSON or bad shape)."""


@dataclass(frozen=True)
class Answers:
    """The (profile, per_question, fixed) model. Values are non-empty strings; per_question keys are normalized."""

    profile: Mapping[str, str] = field(default_factory=dict)
    per_question: Mapping[str, str] = field(default_factory=dict)
    fixed: Mapping[str, str] = field(default_factory=dict)


def default_answers_path() -> Path:
    """``<user config dir>/luk-assist/answers.json`` (Windows: ``%LOCALAPPDATA%\\luk-assist``), outside any repo."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "answers.json"


def normalize(text: object) -> str:
    """Lowercase, accents removed, whitespace collapsed (ñ becomes n)."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def digits_only(value: object) -> str:
    """'1.500.000' -> '1500000'; 'a convenir' -> ''."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _clean(value: object) -> str:
    """A usable answer or '' (None, blanks, containers and <placeholders> are empty)."""
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    text = str(value).strip()
    return "" if _PLACEHOLDER.match(text) else text


def _section(data: Mapping[str, Any], key: str, path: Path) -> dict[str, str]:
    raw = data.get(key, {})
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AnswersError(f"{path}: '{key}' must be an object of text answers")
    out: dict[str, str] = {}
    for k, v in raw.items():
        name, value = str(k).strip(), _clean(v)
        if name and not name.startswith("_") and not _PLACEHOLDER.match(name) and value:
            out[name] = value
    return out


def answers_from_dict(data: Mapping[str, Any], path: Path | str = "<answers>") -> Answers:
    """Build `Answers` from parsed JSON; drops empty values, placeholders and `_comment` keys."""
    path = Path(path)
    if not isinstance(data, Mapping):
        raise AnswersError(f"{path}: the answers file must be a JSON object")
    per_question = {normalize(k): v for k, v in _section(data, "per_question", path).items() if normalize(k)}
    return Answers(
        profile=_section(data, "profile", path),
        per_question=per_question,
        fixed=_section(data, "fixed", path),
    )


def load_answers(path: Path | str | None = None) -> Answers:
    """Load the answers file; a missing file gives empty answers (every field is left blank)."""
    p = Path(path) if path is not None else default_answers_path()
    if not p.is_file():
        return Answers()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AnswersError(f"{p}: not valid JSON (line {exc.lineno}, column {exc.colno})") from exc
    except OSError as exc:
        raise AnswersError(f"{p}: cannot be read ({exc.strerror})") from exc
    return answers_from_dict(data, p)


def write_template(path: Path | str | None = None) -> bool:
    """Create the answers template (placeholders only). Never overwrites: False if the file exists."""
    p = Path(path) if path is not None else default_answers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, "x", encoding="utf-8") as fh:  # "x": fails if it exists, even in a race
            json.dump(TEMPLATE, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except FileExistsError:
        return False
    if os.name != "nt":
        os.chmod(p, 0o600)
    log.info("answers template written to %s", p)
    return True


def _starts_word(keyword: str, label: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword), label) is not None


def _pay_target(n: str) -> str | None:
    """SALARY for your expected pay, SALARY_OTHER for a pay question left blank, '' for payroll
    experience (decided by the other rules), None when the label is not about pay."""
    if not _PAY_TOPIC.search(n):
        return None
    if _PAY_EXPERIENCE.search(n):
        return ""  # about your experience with pay, not about your pay
    if _PAY_NOT_EXPECTED.search(n) or not _PAY_EXPECTATION.search(n):
        return SALARY_OTHER  # current, last, gross or unqualified pay: only a per_question entry answers it
    return SALARY


def match_rule(label: str) -> str | None:
    """The target for a label: a profile key, SALARY / SALARY_OTHER, AVAILABILITY or COVER_LETTER, or None."""
    n = normalize(label)
    pay = _pay_target(n)
    if pay:
        return pay
    for keywords, target in RULES:
        if any(_starts_word(k, n) for k in keywords):
            return target
    return SALARY_OTHER if pay == "" else None


def is_credential(text: str) -> bool:
    """True for password / PIN / one-time-code fields: luk-assist never types into them."""
    return _CREDENTIAL.search(normalize(text)) is not None


def is_contact(text: str) -> bool:
    """True for identity and contact questions (RUT, e-mail, phone...)."""
    return _CONTACT.search(normalize(text)) is not None


def answer_for(label: str, answers: Answers, *, fill_generic: bool = False) -> str:
    """Your answer for a question label, or '' to leave the field blank for you.

    ``fill_generic`` (opt-in) fills an otherwise-blank question with ``fixed.generic`` (or a neutral
    default), except salary questions (digits only, never a sentence), contact and credential fields.
    """
    n = normalize(label)
    if not n or is_credential(n):
        return ""
    for key, value in answers.per_question.items():
        if key and key in n:
            return value
    if is_contact(n):
        return ""
    target = match_rule(n)
    if target == SALARY:
        return digits_only(answers.fixed.get(SALARY, ""))
    if target == SALARY_OTHER:
        return ""
    if target in (AVAILABILITY, COVER_LETTER):
        value = answers.fixed.get(target, "")
    elif target is not None:
        value = answers.profile.get(target, "")
    else:
        value = ""
    if value:
        return value
    if fill_generic:
        return answers.fixed.get(GENERIC) or DEFAULT_GENERIC
    return ""


def make_answer_fn(answers: Answers, *, fill_generic: bool = False) -> Callable[[Any], str]:
    """An ``answer_fn(field) -> str`` for `browser.prefill`.

    The generic sentence (opt-in) only ever goes into free-text areas (``field.kind == "textarea"``),
    never into a one-line input such as a phone, an ID or a number.
    """

    def answer_fn(fld: Any) -> str:
        generic_ok = fill_generic and getattr(fld, "kind", "text") == "textarea"
        return answer_for(getattr(fld, "label", "") or "", answers, fill_generic=generic_ok)

    return answer_fn
