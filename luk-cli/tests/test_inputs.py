"""inputs.py — slug-or-url (§4.7), pills and location modes (§5.1), capture allow/deny (§4.7)."""

import pytest

from luk_cli import inputs
from luk_cli.errors import InvalidArgument

VALID = [
    ("analista-demo-01-empresa-demo-01", "analista-demo-01-empresa-demo-01"),
    ("  abc-1  ", "abc-1"),
    ("https://www.takealuk.com/job_offers/abc-1", "abc-1"),
    ("https://takealuk.com/job_offers/abc?utm=x#frag", "abc"),
    ("https://luk.cl/job_offers/abc/", "abc"),
    ("HTTPS://WWW.LUK.CL/job_offers/abc", "abc"),
]

INVALID = [
    "http://www.takealuk.com/job_offers/abc",
    "https://evil.com/job_offers/abc",
    "https://www.takealuk.com.evil.com/job_offers/abc",
    "https://user@www.takealuk.com/job_offers/abc",
    "https://www.takealuk.com:444/job_offers/abc",
    "https://api.takealuk.com/job_offers/abc",
    "https://www.takealuk.com/companies/abc",
    "https://www.takealuk.com/saved_jobs",
    "https://www.takealuk.com/job_offers/abc/save_later",
    "https://www.takealuk.com/job_offers/%2e%2e",
    "C:\\x.bat",
    "\\\\server\\share\\x.bat",
    "../saved_jobs",
    "x/save_later",
    "file:///C:/x.bat",
    "javascript:alert(1)",
    "Analista",
    "abc--def",
    "-abc",
    "",
    "a" * 201,
]


@pytest.mark.parametrize(("value", "slug"), VALID)
def test_job_slug_or_url_accepted(value, slug):
    assert inputs.parse_slug_or_url(value, "job") == slug


@pytest.mark.parametrize("value", INVALID)
def test_job_slug_or_url_rejected(value):
    with pytest.raises(InvalidArgument):
        inputs.parse_slug_or_url(value, "job")


def test_company_slug_or_url():
    url = "https://www.takealuk.com/companies/empresa-demo-86"
    assert inputs.parse_slug_or_url(url, "company") == "empresa-demo-86"
    with pytest.raises(InvalidArgument):
        inputs.parse_slug_or_url("https://www.takealuk.com/job_offers/abc", "company")


@pytest.mark.parametrize("slug", sorted(inputs.RESERVED_COMPANY_SLUGS))
def test_reserved_company_slugs_rejected(slug):
    with pytest.raises(InvalidArgument):
        inputs.parse_slug_or_url(slug, "company")
    with pytest.raises(InvalidArgument):
        inputs.parse_slug_or_url(f"https://www.takealuk.com/companies/{slug}", "company")


def test_max_slug_length_is_200():
    assert inputs.parse_slug_or_url("a" * 200, "job") == "a" * 200


def test_rebuilt_paths_and_urls_only():
    assert inputs.target_path("job", "abc") == "/job_offers/abc"
    assert inputs.target_path("company", "x-1") == "/companies/x-1"
    assert inputs.target_url("https://www.takealuk.com", "job", "abc") == "https://www.takealuk.com/job_offers/abc"


def test_roles_are_nfc_stripped_pills():
    decomposed = "Contado\u0301n"  # "Contadón" with a combining accent
    assert inputs.normalize_roles([" analista financiero ", decomposed]) == ["analista financiero", "Contadón"]
    assert inputs.normalize_roles([]) == []


@pytest.mark.parametrize(
    ("roles", "limit"),
    [(["a,b"], "','"), (["x" * 51], "50 characters"), (["   "], "empty"), (["é" * 50] * 6, "512 bytes")],
)
def test_role_limits_name_the_limit(roles, limit):
    with pytest.raises(InvalidArgument, match=limit):
        inputs.normalize_roles(roles)


def test_role_limits_boundaries():
    assert inputs.normalize_roles(["x" * 50])
    assert inputs.normalize_roles(["x" * 50] * 10 + ["y" * 2])  # 10×50 + 2 + 10 commas = 512 bytes
    with pytest.raises(InvalidArgument, match="512 bytes"):
        inputs.normalize_roles(["x" * 50] * 10 + ["y" * 3])


def test_fold_matches_accent_and_case_insensitively():
    assert inputs.fold("nunoa") == inputs.fold("Ñuñoa") == "nunoa"
    assert inputs.fold("  Región   Metropolitana ") == "region metropolitana"


@pytest.mark.parametrize(
    ("kwargs", "mode"),
    [
        ({}, "default"),
        ({"locations": ["Santiago"]}, "areas"),
        ({"location_ids": [1318], "locations": ["x"]}, "areas"),
        ({"countries": ["CL"]}, "countries"),
        ({"worldwide": True}, "worldwide"),
    ],
)
def test_location_mode(kwargs, mode):
    assert inputs.location_mode(**kwargs) == mode


@pytest.mark.parametrize(
    "kwargs",
    [{"locations": ["x"], "countries": ["CL"]}, {"location_ids": [1], "worldwide": True},
     {"countries": ["CL"], "worldwide": True}],
)
def test_location_modes_cannot_be_combined(kwargs):
    with pytest.raises(InvalidArgument) as info:
        inputs.location_mode(**kwargs)
    assert "--" not in info.value.message  # MCP users see it verbatim: no CLI flag names


def test_param_vocabularies_follow_the_form():
    assert inputs.POSTED_WITHIN_PARAM == {
        "24h": "last_day", "3d": "last_3_days", "1w": "last_week",
        "1m": "last_month", "3m": "last_3_months", "6m": "last_6_months",
    }
    assert inputs.COUNTRY_PARAM == {"CL": "Chile", "CO": "Colombia", "MX": "México", "PE": "Perú", "BR": "Brasil"}
    assert inputs.JOB_TYPES == ("full_time", "part_time", "contractor", "intern", "per_diem", "other")
    assert inputs.CURRENCIES == ("CLP", "COP", "PEN", "MXN", "BRL")


def test_bounded_int():
    assert inputs.bounded_int("--limit", 150, 1, 150) == 150
    assert inputs.bounded_int("--min-salary", 10**9, 1) == 10**9
    with pytest.raises(InvalidArgument, match="--limit"):
        inputs.bounded_int("--limit", 0, 1, 150)
    with pytest.raises(InvalidArgument, match="--min-salary"):
        inputs.bounded_int("--min-salary", 0, 1)


ALLOWED_CAPTURES = [
    "/", "/job_offers", "/job_offers?job_positions=analista&page=2", "/job_offers/abc-1", "/companies",
    "/companies/empresa-demo-86", "/companies?q=banco", "/saved_jobs", "/saved_jobs?page=2",
    "/profile/application_histories", "/profile/cvs",
]
DENIED_CAPTURES = [
    "https://www.takealuk.com/", "job_offers", "", "/users/sign_in", "/users/sign_out", "/profile",
    "/job_offers/x/save_later", "/users/auth/google_oauth2", "/job_offers/apply-now", "/companies/postulaciones",
    "/profile/cvs/1/destroy", "/saved_jobs/../users", "/job_offers?x=delete", "/saved_jobs?confirm=1",
    "/job_offers?unsubscribe=1", "/Job_offers", "/job_offers#x", "/job_offers?one_tap=1",
]


@pytest.mark.parametrize("path", ALLOWED_CAPTURES)
def test_capture_path_allowed(path):
    assert inputs.validate_capture_path(path) == path


@pytest.mark.parametrize("path", DENIED_CAPTURES)
def test_capture_path_denied(path):
    with pytest.raises(InvalidArgument):
        inputs.validate_capture_path(path)
