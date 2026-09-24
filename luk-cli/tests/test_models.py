"""models.py — the JSON contract of spec §5.4 and the stored-session shapes (§4.1)."""

import json
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from luk_cli import models as m
from luk_cli.errors import AuthRequired

SPEC_KINDS = {
    "job_search", "job", "jobs", "suggestion_list", "area_list", "similar_roles", "related_roles",
    "company_list", "company", "job_list", "application_list", "cv_list", "whoami",
    "session_status", "account_status",
}


def card(**kw):
    return m.JobCard(slug="analista-x", url="https://www.takealuk.com/job_offers/analista-x", title="Analista", **kw)


def meta_page(**kw):
    return {"page": 1, "last_fetched_page": 1, "has_more": False, **kw}


def test_every_kind_has_a_model():
    assert set(m.KIND_MODELS) == SPEC_KINDS


def test_every_key_is_always_present_and_null_means_unknown():
    assert card().model_dump(mode="json") == {
        "slug": "analista-x", "url": "https://www.takealuk.com/job_offers/analista-x", "title": "Analista",
        "company": None, "location": None, "salary": None, "employment_type": None, "modality": None,
        "labels": [], "posted_ago": None, "posted_at_approx": None,
    }


@pytest.mark.parametrize("kind", sorted(SPEC_KINDS))
def test_serialization_schema_marks_every_key_required(kind):
    schema = m.KIND_MODELS[kind].model_json_schema(mode="serialization")
    assert set(schema["required"]) == set(schema["properties"])


def test_contract_models_reject_unknown_keys():
    with pytest.raises(ValidationError):
        m.JobCard(slug="a", url="u", title="t", logo="x")


def test_enums_are_typed():
    assert card(employment_type="full_time", modality="on_site").employment_type == "full_time"
    with pytest.raises(ValidationError):
        card(employment_type="FULL_TIME")
    with pytest.raises(ValidationError):
        card(modality="presencial")


def test_job_posting_extends_job_card():
    posting = m.JobPosting(
        **card(posted_at_approx=date(2026, 9, 1)).model_dump(),
        status="open", description_text="Hola", offer_id=10001,
        address=m.Address(locality="Santiago", country="CL"), date_posted=date(2026, 9, 1),
    )
    doc = posting.model_dump(mode="json")
    assert doc["posted_at_approx"] == "2026-09-01"
    assert doc["address"] == {"locality": "Santiago", "region": None, "country": "CL"}
    assert doc["text_truncated"] is False and doc["requirements_text"] is None
    with pytest.raises(ValidationError):
        m.JobPosting(**card().model_dump(), status="gone", description_text="")


def test_list_hierarchy():
    search = m.JobSearch(**meta_page(total=273, per_page=15), results=[card()], query=m.SearchQuery(roles=["analista"]))
    assert isinstance(search, m.JobList) and isinstance(search, m.ListMeta)
    doc = search.model_dump(mode="json")
    assert doc["details"] is None and doc["not_found"] == [] and doc["effective_location"] is None
    assert doc["query"] == {
        "roles": ["analista"], "location_ids": [], "countries": [], "worldwide": False,
        "filters": {"job_types": [], "posted_within": None, "min_salary": None, "max_salary": None, "currency": None},
    }
    assert m.CompanyList(**meta_page(), results=[]).results == []
    assert m.ApplicationList(**meta_page(), results=[m.Application(title="Analista")]).results[0].status_text is None


def test_envelope_shape_and_kind_check():
    env = m.Envelope(kind="whoami", data=m.WhoAmI(logged_in=True, name="Ana"), warnings=["w"])
    assert env.model_dump(mode="json") == {
        "schema_version": 2, "kind": "whoami",
        "data": {"logged_in": True, "name": "Ana", "email": None}, "warnings": ["w"],
    }
    with pytest.raises(ValidationError):
        m.Envelope(kind="job", data=m.WhoAmI(logged_in=False))
    with pytest.raises(ValidationError):  # a JobSearch is a JobList, but the kind must name it exactly
        m.Envelope(kind="job_list", data=m.JobSearch(**meta_page(), query=m.SearchQuery()))


def test_envelope_round_trips_through_json():
    env = m.Envelope[m.WhoAmI](kind="whoami", data=m.WhoAmI(logged_in=False))
    again = m.Envelope[m.WhoAmI].model_validate_json(env.model_dump_json())
    assert again == env


def test_error_envelope_from_error():
    doc = m.ErrorEnvelope.from_error(AuthRequired()).model_dump(mode="json")
    assert doc == {
        "schema_version": 2, "kind": "error",
        "error": {"code": "AUTH_REQUIRED", "exit_code": 2,
                  "message": "Session expired or missing. Run `luk login`.", "hint": AuthRequired().hint},
    }


def test_account_status_fields_follow_the_tool_table():
    status = m.AccountStatus(session_present=False, private_tools_enabled=True, login_instructions="Run `luk login`.")
    assert set(status.model_dump()) == {
        "session_present", "valid", "name", "email", "logged_in_at", "last_auth_ok_at",
        "private_tools_enabled", "login_instructions",
    }


def test_session_status_never_carries_cookie_values():
    assert set(m.CookieInfo.model_fields) == {"name", "domain", "expires"}
    status = m.SessionStatus(path="p", exists=True, cookies=[m.CookieInfo(name="n", domain="d", expires="session")])
    assert status.expiry == "server-side, unknown"
    assert status.model_dump(mode="json")["valid"] is None


CONTROL_CHARACTERS = [chr(c) for c in (*range(0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0))]


def strings(value):
    """Every str inside a model_dump() (keys included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def test_contract_text_never_carries_terminal_control_characters():
    """Luk text is untrusted third-party data (§7.1): C0 controls except tab and newline, DEL and C1 controls
    are dropped from every contract string, so ESC/CSI/OSC sequences never reach a terminal, a CSV or a JSON
    reader; the printable text around them stays."""
    evil = "".join(CONTROL_CHARACTERS)
    job = m.JobPosting(
        slug="analista-x", url="https://www.takealuk.com/job_offers/analista-x",
        title="Analista \x1b]0;PWNED\x1b\\ \x1b[2J\x9b2J\x07Tributario", company="\x1b[31mEvil",
        labels=["Presencial\x1b[0m", evil], status="open", description_text="Línea 1\n\tLínea 2\r\x1b[H",
        salary=m.Salary(raw="CLP \x9b$1"), address=m.Address(locality="Ñuñoa\x7f"),
    )
    assert job.title == "Analista ]0;PWNED\\ [2J2JTributario" and job.company == "[31mEvil"
    assert job.labels == ["Presencial[0m", ""] and job.description_text == "Línea 1\n\tLínea 2[H"
    assert (job.salary.raw, job.address.locality) == ("CLP $1", "Ñuñoa")
    env = m.Envelope(kind="job_search", warnings=[f"Ubicación: Santiago{evil}"], data=m.JobSearch(
        **meta_page(), query=m.SearchQuery(roles=[f"x{evil}"]), results=[job], related_roles=[evil],
        effective_location=m.AreaRef(id=1318, display_path="Santiago\x1b]0;AREA\x1b\\"),
    ))
    assert env.warnings == ["Ubicación: Santiago"] and env.data.effective_location.display_path == "Santiago]0;AREA\\"
    for text in strings(env.model_dump(mode="json")):
        assert not set(text) & set(CONTROL_CHARACTERS), text
    error = m.ErrorEnvelope.from_error(AuthRequired(f"Luk redirected to /x{evil}")).error
    assert error.message == "Luk redirected to /x"


def test_session_meta_tolerates_future_keys():
    meta = m.SessionMeta.model_validate(
        {"logged_in_at": "2026-09-23T12:00:00+00:00", "browser": "chromium", "luk_cli_version": "0.1.0", "new": 1}
    )
    assert meta.logged_in_at == datetime(2026, 9, 23, 12, tzinfo=timezone.utc) and meta.name is None


PLAYWRIGHT_STATE = {
    "cookies": [{
        "name": "_portal_de_empleos_session", "value": "SENTINEL_VALUE", "domain": "www.takealuk.com",
        "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax", "partitionKey": "x",
    }],
    "origins": [{"origin": "https://www.takealuk.com", "localStorage": [{"name": "k", "value": "SENTINEL_LS"}]}],
}


def test_storage_state_keeps_values_secret():
    state = m.StorageState.model_validate(PLAYWRIGHT_STATE)
    cookie = state.cookies[0]
    assert cookie.http_only is True and cookie.same_site == "Lax" and cookie.expires == -1
    for text in (repr(state), str(state), state.model_dump_json(), json.dumps(state.model_dump(mode="json"))):
        assert "SENTINEL_VALUE" not in text and "SENTINEL_LS" not in text


def test_storage_state_reveals_only_through_to_playwright():
    state = m.StorageState.model_validate(PLAYWRIGHT_STATE)
    revealed = state.to_playwright()
    expected_cookie = {k: v for k, v in PLAYWRIGHT_STATE["cookies"][0].items() if k != "partitionKey"}
    assert revealed == {"cookies": [expected_cookie], "origins": PLAYWRIGHT_STATE["origins"]}
