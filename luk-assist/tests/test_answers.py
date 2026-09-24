"""The answer engine: keyword rules, the (profile, per_question, fixed) model, honesty rules, the file."""

from __future__ import annotations

import json

import pytest
from conftest import EXPERIENCE, PACKAGE_ROOT, REPO_ROOT, RESIDENCE, SALARY

from luk_assist import answers as A
from luk_assist.answers import Answers, answer_for, answers_from_dict, load_answers, make_answer_fn, match_rule
from luk_assist.browser import Field


# --- the three real Luk questions -----------------------------------------------------------
@pytest.mark.parametrize("label,target", [(RESIDENCE, "comuna"), (SALARY, A.SALARY), (EXPERIENCE, "experiencia")])
def test_luk_questions_hit_the_right_rule(label, target):
    assert match_rule(label) == target


def test_luk_questions_get_the_users_answers(paz_answers):
    assert answer_for(RESIDENCE, paz_answers) == "Calle Falsa 123, Comuna Demo, Santiago"
    assert answer_for(SALARY, paz_answers) == "1500000"  # digits only
    assert answer_for(EXPERIENCE, paz_answers).startswith("6 años en análisis comercial")


def test_residencia_alone_is_a_location_question():
    assert match_rule("Lugar de residencia") == "comuna"


def test_empty_profile_leaves_everything_blank():
    for label in (RESIDENCE, SALARY, EXPERIENCE, "Carta de presentación", "¿Tienes mascotas?"):
        assert answer_for(label, Answers()) == ""


def test_matched_rule_with_empty_value_stays_blank(paz_answers):
    # a licence question matches a rule, but Paz never said anything about a licence
    assert answer_for("¿Tienes licencia de conducir?", paz_answers) == ""


def test_salary_is_digits_only_or_blank():
    assert answer_for(SALARY, Answers(fixed={"salary": "$ 1.800.000 líquidos"})) == "1800000"
    assert answer_for(SALARY, Answers(fixed={"salary": "a convenir"})) == ""


# --- per-question overrides ---------------------------------------------------------------
def test_per_question_beats_the_rules_and_ignores_case_and_accents(paz_answers):
    ans = answers_from_dict({
        "profile": dict(paz_answers.profile),
        "per_question": {"EXPERIENCIA relacionada": "Respuesta a medida de Paz."},
    })
    assert answer_for(EXPERIENCE, ans) == "Respuesta a medida de Paz."
    assert answer_for(RESIDENCE, ans) == "Calle Falsa 123, Comuna Demo, Santiago"


def test_per_question_can_answer_a_contact_field_rules_never_do():
    rules_only = Answers(profile={"comuna": "Comuna Demo"})
    assert answer_for("Dirección de correo electrónico", rules_only) == ""  # 'direccion' is a location keyword
    explicit = answers_from_dict({"per_question": {"correo": "paz.prueba@example.com"}})
    assert answer_for("Correo electrónico", explicit) == "paz.prueba@example.com"


@pytest.mark.parametrize("label", ["Contraseña", "Password", "Ingresa tu clave", "PIN", "Código 2FA",
                                   "Código de verificación", "Ingresa el código de 6 dígitos",
                                   "Código enviado a tu correo", "Token", "Security code", "One-time password",
                                   "Enter your one time code"])
def test_credentials_are_never_answered_even_with_an_explicit_entry(label):
    ans = answers_from_dict({"per_question": {label: "secreto"}, "fixed": {"generic": "x"}})
    assert A.is_credential(label)
    assert answer_for(label, ans, fill_generic=True) == ""


@pytest.mark.parametrize("label", [RESIDENCE, SALARY, EXPERIENCE, "Código postal", "¿Tienes licencia de conducir?"])
def test_ordinary_questions_are_not_credentials(label):
    assert not A.is_credential(label)


# --- salary: only YOUR EXPECTED salary, only when the question asks for an expectation -------------
SALARY_SET = Answers(profile={"experiencia": "6 años en análisis comercial."}, fixed={"salary": "1.500.000"})


@pytest.mark.parametrize("label", [
    SALARY,
    "Indique su pretensión de renta líquida",
    "Pretensiones de renta",
    "¿Cuánto quieres ganar?",
    "Renta esperada",
    "¿Cuál es tu aspiración salarial?",
    "¿Qué sueldo líquido esperas?",
])
def test_expected_salary_questions_get_the_digits(label):
    assert match_rule(label) == A.SALARY
    assert answer_for(label, SALARY_SET) == "1500000"


@pytest.mark.parametrize("label", [
    "Comenta tu experiencia en remuneraciones y liquidación de sueldos",
    "Describe tu experiencia en el área de remuneraciones",
    "¿Tienes experiencia en cálculo de sueldo bruto a líquido?",
    "¿Tienes conocimiento en cálculo de remuneraciones?",
    "Manejo de liquidaciones de sueldo",
])
def test_questions_about_payroll_experience_never_get_salary_digits(label):
    assert match_rule(label) != A.SALARY
    for fill_generic in (False, True):
        assert "1500000" not in answer_for(label, SALARY_SET, fill_generic=fill_generic)


@pytest.mark.parametrize("label", [
    "¿Cuál es tu sueldo actual?",
    "¿Cuál fue tu último sueldo líquido?",
    "Indica tu renta bruta esperada",
    "Pretensión de renta bruta",
    "Renta",
    "Sueldo",
])
def test_current_gross_or_unqualified_salary_is_left_blank(label):
    assert match_rule(label) != A.SALARY
    assert answer_for(label, SALARY_SET) == ""
    assert answer_for(label, SALARY_SET, fill_generic=True) == ""  # never the generic sentence either


def test_an_explicit_per_question_entry_answers_a_current_salary_question():
    ans = answers_from_dict({"per_question": {"sueldo actual": "1.200.000"}, "fixed": {"salary": "1500000"}})
    assert answer_for("¿Cuál es tu sueldo actual?", ans) == "1.200.000"


def test_rentabilidad_is_not_about_your_pay():
    assert match_rule("¿Cómo mejorarías la rentabilidad de un producto?") is None


# --- --fill-generic is opt-in ---------------------------------------------------------------
def test_generic_sentence_is_opt_in():
    assert match_rule("¿Tienes mascotas?") is None
    assert answer_for("¿Tienes mascotas?", Answers()) == ""
    assert answer_for("¿Tienes mascotas?", Answers(), fill_generic=True) == A.DEFAULT_GENERIC
    own = Answers(fixed={"generic": "Lo converso con gusto en una entrevista."})
    assert answer_for("¿Tienes mascotas?", own, fill_generic=True) == "Lo converso con gusto en una entrevista."
    # your real answer always wins over the generic sentence
    assert answer_for(EXPERIENCE, Answers(profile={"experiencia": "Algo real."}), fill_generic=True) == "Algo real."


def test_generic_never_goes_into_salary_or_contact_fields():
    assert answer_for(SALARY, Answers(), fill_generic=True) == ""
    assert answer_for("Teléfono celular", Answers(), fill_generic=True) == ""


def test_answer_fn_uses_generic_only_in_textareas():
    fn = make_answer_fn(Answers(), fill_generic=True)
    assert fn(Field(name="q1", label="¿Tienes mascotas?", kind="textarea")) == A.DEFAULT_GENERIC
    assert fn(Field(name="q2", label="¿Tienes mascotas?", kind="text")) == ""
    assert make_answer_fn(Answers())(Field(name="q1", label="¿Tienes mascotas?", kind="textarea")) == ""


def test_default_generic_states_no_fact():
    assert "cumplo" not in A.normalize(A.DEFAULT_GENERIC)
    assert not any(ch.isdigit() for ch in A.DEFAULT_GENERIC)


# --- matching details ---------------------------------------------------------------------
@pytest.mark.parametrize("label,target", [
    ("Describe tus tareas habituales", None),        # 'area' must start a word
    ("Detalle de la propiedad", None),               # 'edad' must start a word
    ("¿Cuál es tu área de interés?", "experiencia"),
    ("Indique su pretensión de renta líquida", A.SALARY),
    ("¿Cuál es tu disponibilidad?", A.AVAILABILITY),
    ("Carta de presentación", A.COVER_LETTER),
    ("Señale su título o profesión", "titulo"),
    ("Nivel de inglés", "idioma"),
    ("¿Tienes movilización propia?", "movilizacion"),
])
def test_rules(label, target):
    assert match_rule(label) == target


def test_normalize_and_digits_only():
    assert A.normalize("  ¿Cuál  es tu PRETENSIÓN? ") == "¿cual es tu pretension?"
    assert A.digits_only("1.200.000") == "1200000"
    assert A.digits_only(None) == ""


# --- the answers file -----------------------------------------------------------------------
def test_template_placeholders_count_as_empty():
    ans = answers_from_dict(A.TEMPLATE)
    assert ans == Answers()
    for label in (RESIDENCE, SALARY, EXPERIENCE):
        assert answer_for(label, ans) == ""


def test_example_file_matches_the_template():
    example = json.loads((PACKAGE_ROOT / "answers.example.json").read_text(encoding="utf-8"))
    assert example == A.TEMPLATE


def test_load_missing_file_gives_empty_answers(tmp_path):
    assert load_answers(tmp_path / "nope.json") == Answers()


def test_load_answers_file(tmp_path):
    p = tmp_path / "answers.json"
    p.write_text(json.dumps({
        "_help": "ignored",
        "profile": {"comuna": " Comuna Demo ", "edad": "", "titulo": "<placeholder>"},
        "fixed": {"salary": 1500000},
        "per_question": {"Años de experiencia en Excel": "3 años", "": "ignored"},
    }), encoding="utf-8")
    ans = load_answers(p)
    assert ans.profile == {"comuna": "Comuna Demo"}
    assert ans.fixed == {"salary": "1500000"}
    assert ans.per_question == {"anos de experiencia en excel": "3 años"}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"profile": ["x"]}'])
def test_bad_answers_file_is_an_error(tmp_path, content):
    p = tmp_path / "answers.json"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(A.AnswersError):
        load_answers(p)


def test_write_template_never_overwrites(tmp_path):
    p = tmp_path / "cfg" / "answers.json"
    assert A.write_template(p) is True
    assert json.loads(p.read_text(encoding="utf-8")) == A.TEMPLATE
    p.write_text('{"profile": {"comuna": "Comuna Demo"}}', encoding="utf-8")
    assert A.write_template(p) is False
    assert json.loads(p.read_text(encoding="utf-8")) == {"profile": {"comuna": "Comuna Demo"}}


def test_default_answers_path_is_outside_the_repository():
    path = A.default_answers_path().resolve()
    assert path.name == "answers.json"
    assert "luk-assist" in path.parts
    assert REPO_ROOT not in path.parents and PACKAGE_ROOT not in path.parents
