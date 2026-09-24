"""cli.py — stdout/stderr discipline, the JSON envelope and error document, CSV with BOM, tables,
exit codes (§5.3, §5.4), verified-only filters (§2.2), redaction (§4.6) and a UTF-8 round-trip
through a real pipe (§8.2). The api is faked: CliRunner tests patch `luk_cli.api`; subprocess tests
use the test-only LUK_TEST_FAKE_API=1 hook with a `luk_fake_api` module written to tmp_path.
"""

import io
import json
import logging
import os
import subprocess
import sys
import textwrap
from datetime import date
from typing import Annotated

import click
import httpx
import pytest
import typer
from typer.testing import CliRunner

from luk_cli import __version__, api, cli, config, parsers
from luk_cli.api import DoctorCheck
from luk_cli.auth import LoginResult
from luk_cli.errors import (
    AuthRequired, Blocked, BudgetExceeded, InternalError, InvalidArgument, NetworkError, NotFound, RateLimited,
    SiteChanged,
)
from luk_cli.models import (
    Application, ApplicationList, Area, AreaList, Company, CompanyCard, CompanyList, CookieInfo, CvList, Envelope,
    JobCard, JobList, JobPosting, JobSearch, Salary, SearchQuery, SessionStatus, SimilarRoles, SuggestionList, WhoAmI,
)
from luk_cli.parsers import Pagination, PrivateList
from luk_cli.redact import register_secret

BASE = "https://www.takealuk.com"
UNICODE = "Ñuñoa ✓ 📍"
runner = CliRunner()


def job_search(*cards, warnings=()):
    data = JobSearch(total=len(cards), page=1, last_fetched_page=1, has_more=False, results=list(cards),
                     query=SearchQuery(roles=["analista"], location_ids=[1021]))
    return Envelope(kind="job_search", data=data, warnings=list(warnings))


def card(slug="analista-x", **kw):
    return JobCard(slug=slug, url=f"{BASE}/job_offers/{slug}", title=kw.pop("title", "Analista [Remoto]"), **kw)


class FakeContext:
    def __init__(self):
        self.verbose = None
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeApi:
    """Replaces api functions; `calls` holds (name, args after ctx, kwargs)."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.ctx = FakeContext()
        self.calls = []

        def from_env(*, verbose=False):
            self.ctx.verbose = verbose
            return self.ctx

        monkeypatch.setattr(api.ApiContext, "from_env", from_env)

    def set(self, name, result):
        def fake(*args, **kwargs):
            self.calls.append((name, args[1:], kwargs))
            if isinstance(result, BaseException):
                raise result
            return result(*args, **kwargs) if callable(result) else result

        self.monkeypatch.setattr(api, name, fake)


@pytest.fixture
def fake(monkeypatch):
    return FakeApi(monkeypatch)


def invoke(*args):
    return runner.invoke(cli.app, list(args))


# -- output discipline (§5.3, §5.4) ----------------------------------------------------------------

def test_json_stdout_is_exactly_the_envelope_and_notes_go_to_stderr(fake):
    envelope = job_search(card(location=UNICODE), warnings=[f"Ubicación: {UNICODE} [Comuna] (1349)"])
    fake.set("search_jobs", envelope)
    result = invoke("search", "analista", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == envelope.model_dump(mode="json")
    assert UNICODE in result.stdout  # ensure_ascii=False
    assert result.stderr.strip() == f"Ubicación: {UNICODE} [Comuna] (1349)"
    assert fake.ctx.closed == 1


def test_search_maps_every_option_to_the_api(fake):
    fake.set("search_jobs", job_search())
    result = invoke(
        "--verbose", "search", "analista financiero", "contador", "--location", "santiago", "-l", "ñuñoa",
        "--location-id", "1318", "--country", "co", "--posted-within", "1w", "--type", "intern", "--type",
        "part_time", "--min-salary", "500000", "--max-salary", "900000", "--currency", "clp", "--page", "2",
        "--offset", "7", "--limit", "30", "--json",
    )
    assert result.exit_code == 0, result.stderr
    assert fake.ctx.verbose is True
    assert fake.calls == [("search_jobs", (), {
        "roles": ["analista financiero", "contador"], "locations": ["santiago", "ñuñoa"], "location_ids": [1318],
        "countries": ["CO"], "worldwide": False, "posted_within": "1w", "job_types": ["intern", "part_time"],
        "min_salary": 500000, "max_salary": 900000, "currency": "CLP", "page": 2, "offset": 7, "limit": 30,
    })]


def test_search_defaults(fake):
    fake.set("search_jobs", job_search())
    assert invoke("search", "--worldwide", "--json").exit_code == 0
    assert fake.calls[0][2] == {
        "roles": [], "locations": [], "location_ids": [], "countries": [], "worldwide": True, "posted_within": None,
        "job_types": [], "min_salary": None, "max_salary": None, "currency": None, "page": 1, "offset": 0,
        "limit": 15,
    }


def test_csv_has_a_bom_a_header_and_one_row_per_result(fake):
    salaried = card("b", title="Contador, senior", salary=Salary(raw="CLP $650.000 - $850.000", currency="CLP",
                                                                  min=650000, max=850000),
                    labels=["Jornada Completa", "Presencial"], posted_at_approx=date(2026, 9, 20))
    fake.set("search_jobs", job_search(card("a", location=UNICODE), salaried))
    result = invoke("search", "analista", "--csv")
    assert result.exit_code == 0
    assert result.stdout.startswith("\ufeffslug,url,title,company,location,salary,salary_currency,salary_min,")
    lines = result.stdout.lstrip("\ufeff").splitlines()
    assert len(lines) == 3 and UNICODE in lines[1]
    assert lines[2].startswith(
        'b,https://www.takealuk.com/job_offers/b,"Contador, senior",,,CLP $650.000 - $850.000,CLP,650000,850000,')
    assert "Jornada Completa; Presencial" in lines[2] and "2026-09-20" in lines[2]


def test_csv_of_an_empty_list_is_just_the_header(fake):
    fake.set("search_companies", Envelope(kind="company_list", data=CompanyList(
        total=0, page=1, last_fetched_page=1, has_more=False)))
    result = invoke("companies", "--csv")
    assert result.stdout == "\ufeffslug,url,name,location,sector,size,active_offers,offer_locations\n"


def test_json_and_csv_together_is_a_usage_error(fake):
    fake.set("search_jobs", job_search())
    result = invoke("search", "x", "--json", "--csv")
    assert result.exit_code == 1 and fake.calls == []
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"


def test_table_is_plain_text_when_not_a_tty(fake):
    fake.set("search_jobs", job_search(card(location=UNICODE)))
    result = invoke("search", "analista")
    assert result.exit_code == 0
    assert "Analista [Remoto]" in result.stdout and UNICODE in result.stdout  # markup-looking text stays literal
    assert "\x1b[" not in result.stdout and result.stderr == ""


def test_table_caption_names_the_continuation(fake):
    cut = job_search(card())
    cut.data = cut.data.model_copy(update={"total": 40, "has_more": True, "next_page": 1, "next_offset": 1})
    fake.set("search_jobs", cut)
    assert "40 results; page 1; more: --page 1 --offset 1" in invoke("search", "analista", "--limit", "1").stdout


@pytest.mark.parametrize(("name", "args", "envelope"), [
    ("get_job", ("show", "analista-x"), Envelope(kind="job", data=JobPosting(
        slug="analista-x", url=f"{BASE}/job_offers/analista-x", title="Analista", status="open",
        description_text="Línea 1\n- punto", requirements_text=None))),
    ("similar_roles", ("similar-roles", "analista"), Envelope(kind="similar_roles", data=SimilarRoles(
        role="analista", resolved_name="Analista", items=["Contador", "Auditor"]))),
    ("whoami", ("whoami",), Envelope(kind="whoami", data=WhoAmI(logged_in=True, name="Ana"))),
    ("session_status", ("session", "status"), Envelope(kind="session_status", data=SessionStatus(
        path="C:/x/session.json", exists=True, cookies=[CookieInfo(name="_portal_de_empleos_session",
                                                                   domain="www.takealuk.com", expires="session")]))),
    ("applications", ("applications",), Envelope(kind="application_list", data=ApplicationList(
        total=None, page=1, last_fetched_page=1, has_more=False,
        results=[Application(title="Analista", status_text="Vista")]))),
])
def test_every_kind_renders_as_a_table(fake, name, args, envelope):
    fake.set(name, envelope)
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() and "\x1b[" not in result.stdout


# -- errors and exit codes (§5.3, §5.4) ------------------------------------------------------------

@pytest.mark.parametrize(("error", "code"), [
    (InvalidArgument("bad"), 1), (AuthRequired(), 2), (NotFound("No Luk area matches 'x'"), 3),
    (NetworkError(), 4), (RateLimited(), 4), (Blocked(), 4), (BudgetExceeded(), 4), (SiteChanged("moved"), 5),
    (InternalError("boom"), 1),
])
def test_luk_errors_map_to_exit_codes(fake, error, code):
    fake.set("search_jobs", error)
    result = invoke("search", "x")
    assert result.exit_code == code
    assert result.stdout == "" and result.stderr.startswith(f"luk: {error.message}")


def test_error_json_goes_to_stdout_with_one_line_on_stderr(fake):
    fake.set("saved_jobs", AuthRequired())
    result = invoke("saved", "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"schema_version": 2, "kind": "error", "error": {
        "code": "AUTH_REQUIRED", "exit_code": 2, "message": AuthRequired.default_message,
        "hint": AuthRequired.default_hint}}
    assert result.stderr.count("\n") == 1 and "luk login" in result.stderr


def test_keyboard_interrupt_exits_130(fake):
    fake.set("search_jobs", KeyboardInterrupt())
    assert invoke("search", "x").exit_code == 130
    result = invoke("search", "x", "--json")
    assert result.exit_code == 130 and json.loads(result.stdout)["error"]["code"] == "INTERRUPTED"


def test_unexpected_errors_are_one_redacted_line_and_a_traceback_only_when_verbose(fake):
    secret = "SENTINEL_B_" + "z" * 30
    register_secret(secret)
    fake.set("search_jobs", RuntimeError(f"boom _portal_de_empleos_session=SENTINEL_A_abc\nvalue {secret}"))

    quiet = invoke("search", "x")
    assert quiet.exit_code == 1 and quiet.stdout == ""
    assert quiet.stderr.splitlines()[0] == (
        "luk: unexpected error: RuntimeError: boom _portal_de_empleos_session=*** value ***")
    assert "Traceback" not in quiet.stderr

    loud = invoke("--verbose", "search", "x", "--json")
    assert loud.exit_code == 1 and json.loads(loud.stdout)["error"]["code"] == "INTERNAL"
    assert "Traceback" in loud.stderr
    for out in (quiet.output, loud.output):
        assert "SENTINEL_A" not in out and secret not in out


def test_secrets_sentinel_never_reaches_output_end_to_end(monkeypatch, settings, store, limiter, saved_session):
    """§4.6/§8.2 through the real api + http layers: the server rotates the cookie to SENTINEL_A, the jar
    holds SENTINEL_B, the second page's transport raises with the Cookie header in its message; run with
    --verbose, --json and the root logger at DEBUG."""
    sentinel_a = "SENTINEL_A_" + "x" * 30
    sentinel_b = "SENTINEL_B_" + "y" * 30
    saved_session(value=sentinel_b)

    def site(request):
        if request.url.params.get("page") == "2":
            raise RuntimeError(f"transport failed after sending {request.headers['cookie']}")
        return httpx.Response(200, text="<html lang='es-CL'><main>ok</main></html>", headers={
            "content-type": "text/html; charset=utf-8",
            "set-cookie": f"_portal_de_empleos_session={sentinel_a}; path=/; secure; HttpOnly"})

    def from_env(*, verbose=False):
        return api.ApiContext(settings, store, limiter, verbose=verbose, luk_transport=httpx.MockTransport(site))

    monkeypatch.setattr(api.ApiContext, "from_env", from_env)
    monkeypatch.setattr(parsers, "parse_saved_jobs", lambda text, *, today, base_url: PrivateList(
        [card()], Pagination(None, True), verified=False))
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", logging.DEBUG)
    root.addHandler(handler)
    try:
        result = invoke("--verbose", "saved", "--limit", "30", "--json")
    finally:
        root.removeHandler(handler)

    assert result.exit_code == 1 and json.loads(result.stdout)["error"]["code"] == "INTERNAL"
    assert "GET https://www.takealuk.com/saved_jobs -> 200" in result.stderr  # verbose ran
    assert "Traceback" in result.stderr and "transport failed" in result.stderr
    for text in (result.stdout, result.stderr, log.getvalue()):
        assert "SENTINEL_A" not in text and "SENTINEL_B" not in text


@pytest.mark.parametrize(("name", "value"), [("LUK_BASE_URL", "http://www.takealuk.com"),
                                             ("LUK_USER_AGENT", "luk-cli/0.1 (José Ñuñoa)")])
@pytest.mark.parametrize("command", [["whoami", "--json"], ["search", "analista", "--json"], ["doctor"]])
def test_bad_environment_is_reported_as_invalid_argument(monkeypatch, name, value, command):
    monkeypatch.setenv(name, value)
    result = invoke(*command)
    assert result.exit_code == 1 and name in result.stderr and "INTERNAL" not in result.stdout + result.stderr
    if "--json" in command:
        assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"


# -- main() (§5.3) ---------------------------------------------------------------------------------

def test_main_returns_exit_codes(fake, capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"luk-cli {__version__}"
    assert cli.main(["search", "--no-such-option"]) == 1
    assert "No such option" in capsys.readouterr().err
    fake.set("cvs", NotFound("x"))
    assert cli.main(["cvs"]) == 3
    assert cli.main(["--help"]) == 0


def test_usage_errors_under_json_print_the_error_document(capsys):
    assert cli.main(["search", "x", "--limit", "abc", "--json"]) == 1
    out = capsys.readouterr()
    error = json.loads(out.out)["error"]
    assert (error["code"], error["exit_code"]) == ("INVALID_ARGUMENT", 1) and "--limit" in error["message"]
    assert out.err.startswith("luk: ") and out.err.count("\n") == 1


@pytest.mark.parametrize("args", [
    ["whoami", "_portal_de_empleos_session=SENTINELCOOKIEVALUE"],
    ["login", "--paste-cookie", "_portal_de_empleos_session=SENTINELCOOKIEVALUE"],
    ["doctor", "remember_user_token=SENTINELCOOKIEVALUE"],
])
def test_usage_errors_are_redacted_with_their_usage_line(capsys, args):
    """§4.6: the top-level handler's final redaction pass also covers click's usage errors (a cookie pasted
    as an argument is echoed as 'unexpected extra argument'), which keep their usage line (§5.3)."""
    assert cli.main(args) == 1
    out = capsys.readouterr()
    assert "SENTINELCOOKIEVALUE" not in out.out + out.err
    assert out.err.startswith(f"Usage: luk {args[0]} ") and "Got unexpected extra argument (" in out.err
    assert "=***" in out.err and out.out == ""


@pytest.mark.parametrize(("raised", "code"), [(AuthRequired(), 2), (KeyboardInterrupt(), 130),
                                              (click.exceptions.Abort(), 130), (RuntimeError("x"), 1)])
def test_main_maps_errors_raised_outside_commands(monkeypatch, capsys, raised, code):
    def app(**kwargs):
        raise raised

    monkeypatch.setattr(cli, "app", app)
    assert cli.main(["whoami", "--json"]) == code
    out = capsys.readouterr()
    assert json.loads(out.out)["error"]["exit_code"] == code and out.err.startswith("luk: ")


# -- other commands --------------------------------------------------------------------------------

def test_list_commands_pass_their_options(fake):
    empty_jobs = Envelope(kind="job_list", data=JobList(page=1, last_fetched_page=1, has_more=False))
    fake.set("saved_jobs", empty_jobs)
    fake.set("applications", Envelope(kind="application_list", data=ApplicationList(
        page=1, last_fetched_page=1, has_more=False)))
    fake.set("search_companies", Envelope(kind="company_list", data=CompanyList(
        total=1, page=1, last_fetched_page=1, has_more=False,
        results=[CompanyCard(slug="empresa-demo-54", url=f"{BASE}/companies/empresa-demo-54", name="Empresa Demo 54 SpA")])))
    fake.set("get_company", lambda ctx, slug, **kw: Envelope(kind="company", data=Company(
        slug="empresa-demo-54", url=f"{BASE}/companies/empresa-demo-54", name="Empresa Demo 54 SpA", page=kw["page"], has_more=False, jobs=[card()])))
    fake.set("find_areas", Envelope(kind="area_list", data=AreaList(query="stgo", context="companies", results=[
        Area(id=1318, name="Santiago", display_path="Santiago, Chile")])))
    fake.set("suggest", Envelope(kind="suggestion_list", data=SuggestionList(query="ana")))
    fake.set("similar_roles", Envelope(kind="similar_roles", data=SimilarRoles(role="x")))
    fake.set("cvs", Envelope(kind="cv_list", data=CvList()))

    for args in (("saved", "--page", "2", "--offset", "3", "--limit", "5", "--details", "--json"),
                 ("applications", "--offset", "4", "--csv"),
                 ("companies", "--query", "banco", "--location", "santiago", "--limit", "48"),
                 ("company", "empresa-demo-54", "--page", "2"), ("areas", "stgo", "--for", "companies"),
                 ("suggest", "ana", "--limit", "7"), ("similar-roles", "x", "--limit", "3"), ("cvs", "--json")):
        assert invoke(*args).exit_code == 0, args

    assert [(name, args, kw) for name, args, kw in fake.calls] == [
        ("saved_jobs", (), {"page": 2, "offset": 3, "limit": 5, "details": True}),
        ("applications", (), {"page": 1, "offset": 4, "limit": 15}),
        ("search_companies", (), {"query": "banco", "location": "santiago", "page": 1, "offset": 0, "limit": 48}),
        ("get_company", ("empresa-demo-54",), {"page": 2}),
        ("find_areas", ("stgo",), {"context": "companies"}),
        ("suggest", ("ana",), {"limit": 7}),
        ("similar_roles", ("x",), {"limit": 3}),
        ("cvs", (), {}),
    ]


def test_session_status_check_and_json(fake):
    fake.set("session_status", Envelope(kind="session_status", data=SessionStatus(path="p", exists=False)))
    result = invoke("session", "status", "--check", "--json")
    assert result.exit_code == 0 and fake.calls == [("session_status", (), {"check": True})]
    assert json.loads(result.stdout)["kind"] == "session_status"


def test_login_passes_its_options(fake):
    fake.set("login", LoginResult(outcome="success", name="Ana", email=None, browser="msedge"))
    assert invoke("login", "--browser", "msedge", "--timeout", "60", "--force").exit_code == 0
    (_, _, kwargs), = fake.calls
    assert {k: kwargs[k] for k in ("browser", "timeout_s", "force", "paste_cookie")} == {
        "browser": "msedge", "timeout_s": 60, "force": True, "paste_cookie": False}


@pytest.mark.parametrize(("name", "args"), [("login", ("login",)), ("refresh_session", ("session", "refresh"))])
def test_login_and_refresh_print_only_what_auth_notifies(fake, name, args):
    incomplete = "profile incomplete — finish onboarding in the browser"

    def run(ctx, **kwargs):
        kwargs["notify"](incomplete)
        kwargs["notify"]("Logged in as Ana")
        return LoginResult(outcome="success_incomplete", name="Ana", email=None, browser="msedge",
                           warnings=[incomplete])

    fake.set(name, run)
    result = invoke(*args)
    assert result.exit_code == 0 and result.stdout == ""
    assert result.stderr == f"{incomplete}\nLogged in as Ana\n"  # auth's lines, never repeated


def test_stderr_lines_never_carry_terminal_control_characters(fake):
    """Lines built from Luk text outside a model (auth's "Logged in as <name>" from the page header, an
    error message, a warning appended after validation) lose their C0/C1 controls on the way out."""
    evil = "\x1b]0;PWNED\x07\x1b[2J\x9b2J\r"

    def login(ctx, **kwargs):
        kwargs["notify"](f"Logged in as Ana{evil}")
        return LoginResult(outcome="success", name="Ana", email=None, browser="chromium")

    fake.set("login", login)
    assert invoke("login").stderr == "Logged in as Ana]0;PWNED[2J2J\n"
    fake.set("cvs", NotFound(f"gone{evil}"))
    assert invoke("cvs").stderr.splitlines()[0] == "luk: gone]0;PWNED[2J2J"
    envelope = Envelope(kind="cv_list", data=CvList())
    envelope.warnings.append(f"note{evil}")  # appended after validation
    fake.set("cvs", envelope)
    assert invoke("cvs").stderr == "note]0;PWNED[2J2J\n"


def test_login_notify_and_paste_cookie(fake):
    def login(ctx, **kwargs):
        kwargs["notify"]("Opening a browser window.")
        return LoginResult(outcome="success", name="Ana", email=None, browser="paste-cookie")

    fake.set("login", login)
    result = invoke("login", "--paste-cookie")
    assert result.exit_code == 0 and result.stdout == ""
    assert "Opening a browser window." in result.stderr and fake.calls[0][2]["paste_cookie"] is True


def test_refresh_logout_and_open(fake):
    fake.set("refresh_session", LoginResult(outcome="success", name="Ana", email=None, browser="chrome"))
    fake.set("logout", True)
    fake.set("open_targets", [f"{BASE}/companies/empresa-demo-54"])
    assert invoke("session", "refresh", "--browser", "chrome").exit_code == 0
    logout = invoke("logout")
    assert logout.exit_code == 0 and "local" in logout.stderr and logout.stdout == ""
    opened = invoke("open", "empresa-demo-54", "--company")
    assert opened.exit_code == 0 and f"{BASE}/companies/empresa-demo-54" in opened.stderr
    assert [c[0] for c in fake.calls] == ["refresh_session", "logout", "open_targets"]
    assert fake.calls[0][2]["browser"] == "chrome" and "notify" in fake.calls[0][2]
    assert fake.calls[2] == ("open_targets", (["empresa-demo-54"],), {"company": True})


def test_doctor_prints_checks_and_fails_on_a_failed_check(fake):
    fake.set("doctor", [DoctorCheck("python", None, sys.executable), DoctorCheck("httpx", True, "0.28.1")])
    ok = invoke("doctor", "--live")
    assert ok.exit_code == 0 and "httpx" in ok.stdout and fake.calls[0][2] == {"live": True}
    fake.set("doctor", [DoctorCheck("chromium", False, "missing")])
    assert invoke("doctor").exit_code == 1


def test_doctor_print_mcp_json(fake):
    result = invoke("doctor", "--print-mcp-json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == api.mcp_config()


def test_debug_capture_prints_the_written_path(fake, tmp_path):
    written = tmp_path / "saved_jobs.html"
    fake.set("debug_capture", written)
    result = invoke("debug", "capture", "/saved_jobs", "--out", str(tmp_path))
    assert result.exit_code == 0 and result.stdout.strip() == str(written)
    assert fake.calls == [("debug_capture", ("/saved_jobs",), {"out_dir": tmp_path})]


def test_schema_prints_or_writes_json_schemas(tmp_path):
    one = invoke("schema", "job_search")
    assert one.exit_code == 0 and json.loads(one.stdout) == api.schemas()["job_search"]
    everything = json.loads(invoke("schema").stdout)
    assert set(everything) == set(api.schemas())
    written = invoke("schema", "--out", str(tmp_path / "schema"))
    assert written.exit_code == 0 and written.stdout == ""
    files = sorted(p.name for p in (tmp_path / "schema").iterdir())
    assert files == sorted(f"{kind}.json" for kind in api.schemas())
    assert b"\r\n" not in (tmp_path / "schema" / "job.json").read_bytes()
    assert invoke("schema", "nope").exit_code == 1


def test_mcp_without_the_extra_exits_1(monkeypatch):
    for name in ("mcp", "mcp.server.fastmcp", "mcp.server.fastmcp.exceptions", "mcp.types"):
        monkeypatch.setitem(sys.modules, name, None)  # `import` now raises ImportError
    result = invoke("mcp")
    assert result.exit_code == 1 and 'luk-cli[mcp]' in result.stderr and result.stdout == ""


def test_mcp_runs_the_server_lazily(monkeypatch):
    import luk_cli.mcp_server as server

    served = []
    monkeypatch.setattr(server, "main", lambda: served.append(True))
    assert invoke("mcp").exit_code == 0 and served == [True]


# -- verified-only registration (§2.2) --------------------------------------------------------------

def test_unverified_options_are_not_registered(monkeypatch):
    monkeypatch.setattr(config, "VERIFIED_PARAMS", frozenset({"job_types"}))
    probe = typer.Typer()

    @probe.command()
    @cli._verified_only(kinds="job_types", country="countries")
    def search(kinds: Annotated[bool, typer.Option("--kinds")] = False,
               country: Annotated[str, typer.Option("--country")] = "") -> None:
        typer.echo(f"{kinds} {country!r}")

    assert "--country" not in runner.invoke(probe, ["--help"]).stdout
    assert runner.invoke(probe, ["--kinds"]).stdout.strip() == "True ''"
    assert runner.invoke(probe, ["--country", "CO"]).exit_code == 2


def test_every_phase0_filter_is_registered_today():
    help_text = runner.invoke(cli.app, ["search", "--help"], env={"COLUMNS": "200"}).stdout
    for option in ("--country", "--worldwide", "--posted-within", "--type", "--min-salary", "--max-salary",
                   "--currency", "--location", "--location-id", "--page", "--limit", "--json", "--csv"):
        assert option in help_text


# -- real processes (§5.3, §8.2) -------------------------------------------------------------------

FAKE_API = textwrap.dedent(f"""
    from luk_cli.api import DoctorCheck
    from luk_cli.auth import LoginResult
    from luk_cli.models import (
        ApplicationList, Area, AreaList, Company, CompanyList, CvList, Envelope, JobCard, JobList, JobPosting,
        JobSearch, SearchQuery, SessionStatus, SimilarRoles, SuggestionList, WhoAmI,
    )

    TEXT = {UNICODE!r}
    URL = "https://www.takealuk.com/job_offers/x"
    META = dict(page=1, last_fetched_page=1, has_more=False)


    class ApiContext:
        @classmethod
        def from_env(cls, *, verbose=False):
            return cls()

        def close(self):
            pass


    def find_areas(ctx, text, *, context="jobs"):
        data = AreaList(query=text, context=context, results=[Area(id=1349, name=TEXT, display_path=TEXT)])
        return Envelope(kind="area_list", data=data, warnings=["Ubicación: " + TEXT])


    def search_jobs(ctx, **kwargs):
        job = JobCard(slug="x", url=URL, title=TEXT, location=TEXT)
        return Envelope(kind="job_search", data=JobSearch(total=1, results=[job], query=SearchQuery(), **META))


    def get_job(ctx, slug_or_url):
        job = JobPosting(slug="x", url=URL, title=TEXT, status="open", description_text=TEXT)
        return Envelope(kind="job", data=job)


    def suggest(ctx, prefix, *, limit):
        return Envelope(kind="suggestion_list", data=SuggestionList(query=prefix))


    def similar_roles(ctx, role, *, limit):
        return Envelope(kind="similar_roles", data=SimilarRoles(role=role, items=[TEXT]))


    def search_companies(ctx, **kwargs):
        return Envelope(kind="company_list", data=CompanyList(total=0, **META))


    def get_company(ctx, slug_or_url, *, page):
        company = Company(slug="c", url="https://www.takealuk.com/companies/c", name=TEXT, page=page, has_more=False)
        return Envelope(kind="company", data=company)


    def saved_jobs(ctx, **kwargs):
        return Envelope(kind="job_list", data=JobList(**META))


    def applications(ctx, **kwargs):
        return Envelope(kind="application_list", data=ApplicationList(**META))


    def cvs(ctx):
        return Envelope(kind="cv_list", data=CvList())


    def whoami(ctx):
        return Envelope(kind="whoami", data=WhoAmI(logged_in=True, name=TEXT))


    def session_status(ctx, *, check):
        return Envelope(kind="session_status", data=SessionStatus(path="session.json", exists=False))


    def login(ctx, *, notify, **kwargs):
        notify("Logged in as " + TEXT)
        return LoginResult("success", TEXT, None, "chromium")


    refresh_session = login


    def logout(ctx):
        return False


    def open_targets(ctx, targets, *, company):
        return []  # never starts a real browser


    def doctor(ctx, *, live):
        return [DoctorCheck("python", None, TEXT)]


    def debug_capture(ctx, path, *, out_dir):
        return "capture.html"
""")

EVERY_COMMAND = (
    ["areas", "x", "--json"], ["search", "x", "--csv"], ["show", "x"], ["suggest", "x"], ["similar-roles", "x"],
    ["companies"], ["company", "c"], ["saved"], ["applications", "--csv"], ["cvs"], ["whoami", "--json"], ["login"],
    ["logout"], ["session", "status"], ["session", "refresh"], ["open", "x"], ["doctor"],
    ["doctor", "--print-mcp-json"], ["debug", "capture", "/"], ["schema", "job"],
)


@pytest.fixture
def fake_env(tmp_path):
    (tmp_path / "luk_fake_api.py").write_text(FAKE_API, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    paths = [str(tmp_path), *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))]
    env.update(LUK_TEST_FAKE_API="1", PYTHONPATH=os.pathsep.join(paths))
    return env


def run_luk(env, *args):
    return subprocess.run([sys.executable, "-m", "luk_cli", *args], capture_output=True, env=env, timeout=120)


def test_utf8_round_trips_through_a_pipe(fake_env):
    as_json = run_luk(fake_env, "areas", "nunoa", "--json")
    assert as_json.returncode == 0, as_json.stderr.decode("utf-8", "replace")
    assert json.loads(as_json.stdout.decode("utf-8"))["data"]["results"][0]["name"] == UNICODE
    assert f"Ubicación: {UNICODE}" in as_json.stderr.decode("utf-8")

    as_csv = run_luk(fake_env, "search", "x", "--csv")
    assert as_csv.stdout.startswith(b"\xef\xbb\xbfslug,") and UNICODE.encode("utf-8") in as_csv.stdout

    table = run_luk(fake_env, "search", "x")
    assert table.returncode == 0 and UNICODE in table.stdout.decode("utf-8")


def test_cli_never_imports_playwright(fake_env):
    """§8.2: importing cli, api and mcp_server and running every command (fake api) never loads Playwright."""
    code = ("import sys; from luk_cli import cli, api, mcp_server; "
            f"codes = [cli.main(args) for args in {list(EVERY_COMMAND)!r}]; "
            "sys.stdout.flush(); print('RESULT', codes, 'playwright' in sys.modules)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=fake_env, timeout=120)
    last = proc.stdout.decode("utf-8").strip().splitlines()[-1]
    assert last == f"RESULT {[0] * len(EVERY_COMMAND)} False", proc.stderr.decode("utf-8", "replace")


def test_fake_api_hook_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LUK_TEST_FAKE_API", raising=False)
    assert cli._api() is api
