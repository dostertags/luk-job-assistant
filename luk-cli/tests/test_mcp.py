"""MCP server (spec §7.1, §7.2, §8.2 "MCP"): tool list, schemas, annotations, direct calls with a
fake api, `CODE: …` / INTERNAL texts without reprs, LUK_MCP_PRIVATE=0, nothing on stdout."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import anyio
import httpx
import pytest

pytest.importorskip("mcp.server.fastmcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from luk_cli import api, config, mcp_server
from luk_cli.config import Settings
from luk_cli.errors import (
    AuthRequired,
    Blocked,
    BudgetExceeded,
    InvalidArgument,
    LukError,
    NetworkError,
    NotFound,
    SiteChanged,
    UnsafeRequest,
)
from luk_cli.mcp_server import PRIVATE_TOOLS, PUBLIC_TOOLS
from luk_cli.models import (
    AccountStatus,
    Application,
    ApplicationList,
    Area,
    AreaList,
    Company,
    CompanyCard,
    CompanyList,
    Cv,
    CvList,
    Envelope,
    JobCard,
    JobList,
    JobPosting,
    Jobs,
    JobSearch,
    RelatedRoles,
    SearchQuery,
    SessionMeta,
)
from luk_cli.ratelimit import RateLimiter
from luk_cli.redact import register_secret
from luk_cli.session import SessionStore

BASE = "https://www.takealuk.com"
CARD = JobCard(slug="analista-contable", url=f"{BASE}/job_offers/analista-contable", title="Analista Contable Ñuñoa")
POSTING = JobPosting(**CARD.model_dump(), status="open", description_text="Funciones: …")
LOGIN_STEP = "Ask the user to run `luk login` (a browser window opens; they type their own credentials), then retry."
STOP_STEP = "Stop and tell the user; never retry or work around it."
INTERNAL_TEXT = "INTERNAL: unexpected error — ask the user to run `luk doctor`"
PERSONAL = (
    "Returns the user's PERSONAL Luk data — call only when the user asks about their own "
    "saved jobs/applications/CVs."
)
SENTINEL_A = "SENTINEL_A_" + "x" * 24
SENTINEL_B = "SENTINEL_B_" + "y" * 24
REPR_RE = re.compile(r"<|object at 0x|[A-Za-z]+(Error|Exception)\(|Traceback")


def page_meta(**overrides: Any) -> dict[str, Any]:
    return {"page": 1, "last_fetched_page": 1, "has_more": False, **overrides}


def job_search(results: list[JobCard], **meta: Any) -> Envelope[Any]:
    data = JobSearch(**page_meta(**meta), total=len(results), query=SearchQuery(), results=results)
    return Envelope(kind="job_search", data=data)


ENVELOPES: dict[str, Envelope[Any]] = {
    "search_jobs": job_search([CARD]),
    "get_jobs": Envelope(kind="jobs", data=Jobs(results=[POSTING])),
    "related_roles": Envelope(kind="related_roles", data=RelatedRoles(role="analista", similar=["contador"])),
    "find_areas": Envelope(
        kind="area_list",
        data=AreaList(
            query="santiago",
            context="companies",
            results=[Area(id=1318, name="Santiago", display_path="Santiago, Región Metropolitana, Chile")],
        ),
    ),
    "search_companies": Envelope(
        kind="company_list",
        data=CompanyList(**page_meta(), results=[CompanyCard(slug="banco-x", url=f"{BASE}/companies/banco-x",
                                                             name="Banco X")]),
    ),
    "get_company": Envelope(
        kind="company",
        data=Company(slug="empresa-demo-54", url=f"{BASE}/companies/empresa-demo-54", name="Empresa Demo 54 SpA", page=2, has_more=False,
                     jobs=[CARD]),
    ),
    "saved_jobs": Envelope(kind="job_list", data=JobList(**page_meta(), results=[CARD])),
    "applications": Envelope(
        kind="application_list", data=ApplicationList(**page_meta(), results=[Application(title="Analista")])
    ),
    "cvs": Envelope(kind="cv_list", data=CvList(results=[Cv(name="cv-1.pdf")])),
    "account_status": Envelope(
        kind="account_status",
        data=AccountStatus(session_present=False, private_tools_enabled=True, login_instructions="Run `luk login`."),
    ),
}


class FakeContext:
    """Stands in for ApiContext: the server only reads `.settings` and calls `.close()`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeApi:
    """Replaces every luk_cli.api function the tools call; records (ctx, args, kwargs, thread)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any], int]] = []
        self.outcomes: dict[str, Envelope[Any] | BaseException] = dict(ENVELOPES)
        for name in ENVELOPES:
            monkeypatch.setattr(api, name, self._fake(name))

    def _fake(self, name: str) -> Callable[..., Envelope[Any]]:
        def fake(ctx: Any, *args: Any, **kwargs: Any) -> Envelope[Any]:
            self.calls.append((name, ctx, args, kwargs, threading.get_ident()))
            outcome = self.outcomes[name]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return fake


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    return FakeApi(monkeypatch)


@pytest.fixture
def contexts() -> list[FakeContext]:
    return []


@pytest.fixture
def make_server(settings: Settings, contexts: list[FakeContext]) -> Callable[..., Any]:
    def make(*, private: bool = True) -> Any:
        configured = dataclasses.replace(settings, mcp_private=private)

        def factory() -> Any:
            ctx = FakeContext(configured)
            contexts.append(ctx)
            return ctx

        return mcp_server.build_server(factory)

    return make


def list_tools(server: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in anyio.run(server.list_tools)}


def call(server: Any, tool: str, /, **arguments: Any) -> dict[str, Any]:
    (block,) = anyio.run(server.call_tool, tool, arguments)
    return json.loads(block.text)


def tool_error(server: Any, tool: str, /, **arguments: Any) -> str:
    """The ToolError text the client sees: exactly `<CODE>: …`, without FastMCP's 'Error executing
    tool X: ' prefix and without a chained exception that could carry a repr."""
    with pytest.raises(ToolError) as caught:
        anyio.run(server.call_tool, tool, arguments)
    error = caught.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)
    return str(error)


# --- tool list, annotations, schemas ------------------------------------------------------------


def test_private_tools_are_registered_only_when_enabled(make_server: Callable[..., Any]) -> None:
    assert list(list_tools(make_server(private=True))) == [*PUBLIC_TOOLS, *PRIVATE_TOOLS]
    assert list(list_tools(make_server(private=False))) == list(PUBLIC_TOOLS)


def test_every_tool_is_async_read_only_and_open_world(make_server: Callable[..., Any]) -> None:
    server = make_server()
    for name, tool in list_tools(server).items():
        assert tool.annotations.readOnlyHint is True, name
        assert tool.annotations.openWorldHint is True, name
        assert tool.outputSchema is None, name  # the envelope travels as JSON text (§5.4)
        assert server._tool_manager.get_tool(name).is_async, name


def test_search_jobs_schema_holds_the_caps_and_vocabularies(make_server: Callable[..., Any]) -> None:
    schema = list_tools(make_server())["search_jobs"].inputSchema
    props = schema["properties"]
    assert list(props) == [
        "roles", "location", "location_id", "countries", "worldwide", "posted_within", "job_types",
        "min_salary", "max_salary", "currency", "page", "offset", "max_results",
    ]
    assert schema.get("required", []) == []
    assert props["roles"]["maxItems"] == 5 and props["roles"]["items"]["maxLength"] == 50
    assert props["roles"]["items"]["pattern"] == "^[^,]*$"  # one pill per item: no commas (§5.1)
    assert props["roles"]["default"] == []
    assert props["max_results"]["default"] == 15 and props["max_results"]["maximum"] == 45
    assert props["page"]["minimum"] == 1 and props["min_salary"]["anyOf"][0]["minimum"] == 1
    assert props["offset"]["minimum"] == 0 and props["offset"]["default"] == 0
    assert "next_offset" in props["offset"]["description"]
    text = json.dumps(schema)
    for value in ("CL", "CO", "MX", "PE", "BR", "24h", "6m", "full_time", "per_diem", "CLP", "BRL"):
        assert f'"{value}"' in text


def test_other_schemas_hold_the_7_2_caps(make_server: Callable[..., Any]) -> None:
    tools = list_tools(make_server())
    props = {name: tool.inputSchema["properties"] for name, tool in tools.items()}
    assert props["get_jobs"]["slugs_or_urls"]["minItems"] == 1
    assert props["get_jobs"]["slugs_or_urls"]["maxItems"] == 10
    assert tools["get_jobs"].inputSchema["required"] == ["slugs_or_urls"]
    assert props["get_jobs"]["full"]["default"] is False
    assert props["related_roles"]["limit"]["maximum"] == 9 and props["related_roles"]["limit"]["default"] == 9
    assert props["find_locations"]["context"]["enum"] == ["jobs", "companies"]
    assert props["search_companies"]["max_results"]["maximum"] == 48
    assert props["search_companies"]["max_results"]["default"] == 24
    assert list(props["search_companies"]) == ["name", "location", "page", "offset", "max_results"]
    assert list(props["get_company"]) == ["slug_or_url", "page"]
    assert props["my_saved_jobs"]["max_results"]["maximum"] == 45
    assert props["my_applications"]["max_results"]["maximum"] == 45
    assert list(props["my_saved_jobs"]) == ["page", "offset", "max_results", "details"]
    assert list(props["my_applications"]) == ["page", "offset", "max_results"]
    for name in ("search_companies", "my_saved_jobs", "my_applications"):
        assert (props[name]["offset"]["minimum"], props[name]["offset"]["default"]) == (0, 0), name
    assert props["my_cvs"] == {}
    assert list(props["account_status"]) == ["check"]


def test_unverified_filters_are_absent_from_the_schemas(
    make_server: Callable[..., Any], fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "VERIFIED_PARAMS", frozenset({"job_types", "posted_within"}))
    server = make_server()
    props = {name: tool.inputSchema["properties"] for name, tool in list_tools(server).items()}
    assert list(props["search_jobs"]) == [
        "roles", "location", "location_id", "posted_within", "job_types", "page", "offset", "max_results",
    ]
    assert list(props["search_companies"]) == ["name", "page", "offset", "max_results"]
    assert list(props["find_locations"]) == ["text"]

    call(server, "search_jobs", roles=["analista"])
    call(server, "find_locations", text="santiago")
    (_, _, _, search, _), (_, _, args, areas, _) = fake_api.calls
    assert search["countries"] == [] and search["worldwide"] is False and search["min_salary"] is None
    assert args == ("santiago",) and areas["context"] == "jobs"


def test_descriptions_and_instructions_carry_the_guidance(make_server: Callable[..., Any]) -> None:
    server = make_server()
    tools = list_tools(server)
    for name in PRIVATE_TOOLS:
        assert tools[name].description.startswith(PERSONAL), name
    for name in PUBLIC_TOOLS:
        assert "read-only" in tools[name].description.lower(), name
    search = tools["search_jobs"].description
    assert "job titles" in search and "related_roles" in search and "Chile" in search
    for name in ("search_jobs", "get_jobs", "search_companies", "get_company", "my_saved_jobs"):  # return offer text
        assert "untrusted third-party data" in tools[name].description, name
    assert "never returns cookie values" in tools["account_status"].description

    instructions = server.instructions
    for phrase in ("read-only", "luk login", "password", "luk open", "untrusted", "BLOCKED", "PERSONAL",
                   "page=next_page", "offset=next_offset"):
        assert phrase in instructions, phrase
    assert "my_saved_jobs" not in make_server(private=False).instructions


# --- direct calls with a fake api -----------------------------------------------------------------

CALLS = [
    (
        "search_jobs",
        {"roles": ["analista financiero", "contador"], "location": "Ñuñoa", "posted_within": "1w",
         "job_types": ["full_time", "intern"], "min_salary": 1_000_000, "currency": "CLP", "page": 2,
         "offset": 7, "max_results": 30},
        "search_jobs",
        (),
        {"roles": ["analista financiero", "contador"], "locations": ["Ñuñoa"], "location_ids": [], "countries": [],
         "worldwide": False, "posted_within": "1w", "job_types": ["full_time", "intern"], "min_salary": 1_000_000,
         "max_salary": None, "currency": "CLP", "page": 2, "offset": 7, "limit": 30},
    ),
    (
        "search_jobs",
        {"countries": ["CO", "MX"]},
        "search_jobs",
        (),
        {"roles": [], "locations": [], "location_ids": [], "countries": ["CO", "MX"], "worldwide": False,
         "posted_within": None, "job_types": [], "min_salary": None, "max_salary": None, "currency": None,
         "page": 1, "offset": 0, "limit": 15},
    ),
    (
        "search_jobs",
        {"location_id": 1318, "worldwide": False, "max_salary": 900_000},
        "search_jobs",
        (),
        {"roles": [], "locations": [], "location_ids": [1318], "countries": [], "worldwide": False,
         "posted_within": None, "job_types": [], "min_salary": None, "max_salary": 900_000, "currency": None,
         "page": 1, "offset": 0, "limit": 15},
    ),
    (
        "get_jobs",
        {"slugs_or_urls": ["analista-contable", f"{BASE}/job_offers/contador"], "full": True},
        "get_jobs",
        (["analista-contable", f"{BASE}/job_offers/contador"],),
        {"full": True},
    ),
    ("related_roles", {"role": "analista"}, "related_roles", ("analista",), {"limit": 9}),
    ("find_locations", {"text": "santiago", "context": "companies"}, "find_areas", ("santiago",),
     {"context": "companies", "limit": 10}),
    (
        "search_companies",
        {"name": "banco", "location": "Santiago", "page": 3, "offset": 5, "max_results": 48},
        "search_companies",
        (),
        {"query": "banco", "location": "Santiago", "page": 3, "offset": 5, "limit": 48},
    ),
    ("get_company", {"slug_or_url": "empresa-demo-54", "page": 2}, "get_company", ("empresa-demo-54",), {"page": 2}),
    (
        "my_saved_jobs",
        {"max_results": 45, "details": True},
        "saved_jobs",
        (),
        {"page": 1, "offset": 0, "limit": 45, "details": True, "details_limit": 10},
    ),
    ("my_applications", {"page": 3, "offset": 2}, "applications", (), {"page": 3, "offset": 2, "limit": 15}),
    ("my_cvs", {}, "cvs", (), {}),
    ("account_status", {"check": True}, "account_status", (), {"check": True}),
]


@pytest.mark.parametrize(("tool", "arguments", "api_name", "args", "kwargs"), CALLS)
def test_tools_call_the_api_in_a_worker_thread_with_a_fresh_context(
    make_server: Callable[..., Any],
    fake_api: FakeApi,
    contexts: list[FakeContext],
    tool: str,
    arguments: dict[str, Any],
    api_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    server = make_server()
    assert [ctx.closed for ctx in contexts] == [1]  # the build-time context only reads settings

    document = call(server, tool, **arguments)

    ((name, ctx, got_args, got_kwargs, thread),) = fake_api.calls
    assert (name, got_args, got_kwargs) == (api_name, args, kwargs)
    assert thread != threading.get_ident()
    assert ctx is contexts[-1] and len(contexts) == 2 and ctx.closed == 1
    assert document == ENVELOPES[api_name].model_dump(mode="json")


def test_result_is_the_compact_utf8_envelope(make_server: Callable[..., Any], fake_api: FakeApi) -> None:
    (block,) = anyio.run(make_server().call_tool, "search_jobs", {})
    assert block.text.startswith('{"schema_version":2,"kind":"job_search","data":{')
    assert "Ñuñoa" in block.text and "\\u" not in block.text


def test_an_empty_first_page_is_a_success_with_a_hint(make_server: Callable[..., Any], fake_api: FakeApi) -> None:
    server = make_server()
    fake_api.outcomes["search_jobs"] = job_search([])
    empty = call(server, "search_jobs", roles=["zzqxwvkj"])
    assert empty["data"]["results"] == [] and len(empty["warnings"]) == 1
    assert "not an error" in empty["warnings"][0] and "related_roles" in empty["warnings"][0]

    past_end = job_search([], page=20, last_fetched_page=20)
    past_end.warnings.append("page 20 is past the last page (19)")
    fake_api.outcomes["search_jobs"] = past_end
    assert call(server, "search_jobs", page=20)["warnings"] == ["page 20 is past the last page (19)"]

    shrunk = job_search([], offset=4)  # what is left of page 1 after a cursor: a continuation, not "no match"
    shrunk.warnings.append("page 1 has only 4 results; offset 4 skipped them all")
    fake_api.outcomes["search_jobs"] = shrunk
    assert call(server, "search_jobs", offset=4)["warnings"] == ["page 1 has only 4 results; offset 4 skipped them all"]

    fake_api.outcomes["cvs"] = Envelope(kind="cv_list", data=CvList())
    assert "not an error" in call(server, "my_cvs")["warnings"][0]


def test_empty_related_roles_and_jobs_carry_a_hint(make_server: Callable[..., Any], fake_api: FakeApi) -> None:
    """§7.1 "empty results are a success with a hint in warnings" also covers related_roles (the tool Claude
    is sent to after a thin search) and get_jobs when no offer is left."""
    server = make_server()
    fake_api.outcomes["related_roles"] = Envelope(kind="related_roles", data=RelatedRoles(role="zzqxwvkj"))
    (hint,) = call(server, "related_roles", role="zzqxwvkj")["warnings"]
    assert "not an error" in hint and "broader" in hint

    fake_api.outcomes["related_roles"] = Envelope(kind="related_roles", data=RelatedRoles(role="x", similar=["y"]))
    assert call(server, "related_roles", role="x")["warnings"] == []
    fake_api.outcomes["related_roles"] = Envelope(
        kind="related_roles", data=RelatedRoles(role="x", suggestions=[{"query": "y", "popularity": 1}]))
    assert call(server, "related_roles", role="x")["warnings"] == []

    fake_api.outcomes["get_jobs"] = Envelope(kind="jobs", data=Jobs(not_found=["gone-1", "gone-2"]))
    (hint,) = call(server, "get_jobs", slugs_or_urls=["gone-1", "gone-2"])["warnings"]
    assert "not an error" in hint and "not_found" in hint


# --- errors ---------------------------------------------------------------------------------------

ERROR_TEXTS = [
    (AuthRequired(), f"AUTH_REQUIRED: Not logged in to Luk. {LOGIN_STEP}"),  # the §7.1 example, verbatim
    (
        AuthRequired("Luk redirected to /onboarding — finish your profile in the browser, then retry"),
        (
            "AUTH_REQUIRED: Luk redirected to /onboarding — finish your profile in the browser, then retry. "
            "Tell the user."
        ),
    ),
    (
        Blocked.for_status(403),
        f"BLOCKED: Blocked by Luk (HTTP 403) — stopping; not retrying (repo red line: no evasion). {STOP_STEP}",
    ),
    (BudgetExceeded(), f"BUDGET_EXCEEDED: hourly budget reached. {STOP_STEP}"),
    (NotFound("No Luk area matches 'zzqx'"), "NOT_FOUND: No Luk area matches 'zzqx'."),
    (InvalidArgument("page must be at least 1"), "INVALID_ARGUMENT: page must be at least 1."),
    (NetworkError(), "NETWORK: network error talking to Luk. Check your connection, then retry."),
    (
        SiteChanged.missing_anchor("search", "turbo-frame#job_offers_results", "/job_offers?job_positions=x"),
        (
            "SITE_CHANGED: Luk search page changed: missing turbo-frame#job_offers_results. Run `luk debug "
            'capture "/job_offers?job_positions=x"` and report it. Tell the user; run the capture only if they ask.'
        ),
    ),
    (UnsafeRequest(), "INTERNAL: refused an unsafe request. Ask the user to run `luk doctor`."),
]


@pytest.mark.parametrize(("error", "expected"), ERROR_TEXTS, ids=[type(error).__name__ for error, _ in ERROR_TEXTS])
def test_luk_errors_become_code_message_next_step(
    make_server: Callable[..., Any], fake_api: FakeApi, error: LukError, expected: str
) -> None:
    fake_api.outcomes["search_jobs"] = error
    text = tool_error(make_server(), "search_jobs")
    assert text == expected
    assert not REPR_RE.search(text)


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("search_jobs", {"max_results": 46},
         "INVALID_ARGUMENT: max_results: Input should be less than or equal to 45."),
        ("search_jobs", {"roles": ["a", "b", "c", "d", "e", "f"]},
         "INVALID_ARGUMENT: roles: Value should have at most 5 items after validation, not 6."),
        ("search_jobs", {"countries": ["AR"]},
         "INVALID_ARGUMENT: countries.0: Input should be 'CL', 'CO', 'MX', 'PE' or 'BR'."),
        ("get_jobs", {}, "INVALID_ARGUMENT: slugs_or_urls: Field required."),
        ("get_jobs", {"slugs_or_urls": [f"s{i}" for i in range(11)]},
         "INVALID_ARGUMENT: slugs_or_urls: List should have at most 10 items after validation, not 11."),
        ("related_roles", {"role": "analista", "limit": 10},
         "INVALID_ARGUMENT: limit: Input should be less than or equal to 9."),
        ("search_jobs", {"roles": ["analista, contador"]},
         "INVALID_ARGUMENT: roles.0: String should match pattern '^[^,]*$'."),
        ("search_jobs", {"location_ids": [1318]},
         ("INVALID_ARGUMENT: unknown argument location_ids; search_jobs takes roles, location, location_id, "
          "countries, worldwide, posted_within, job_types, min_salary, max_salary, currency, page, offset, "
          "max_results.")),
        ("my_cvs", {"page": 2, "all": True}, "INVALID_ARGUMENT: unknown argument all, page; my_cvs takes no arguments."),
    ],
)
def test_schema_violations_are_invalid_argument_without_echoing_input(
    make_server: Callable[..., Any], fake_api: FakeApi, tool: str, arguments: dict[str, Any], expected: str
) -> None:
    text = tool_error(make_server(), tool, **arguments)
    assert text == expected
    assert not REPR_RE.search(text) and "input_value" not in text
    assert fake_api.calls == []


def test_an_unregistered_filter_is_refused_not_silently_dropped(
    make_server: Callable[..., Any], fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filter Phase 0 did not verify is absent from the schema; sending it anyway must not return
    unfiltered results that look filtered (FastMCP's argument models ignore unknown keys)."""
    monkeypatch.setattr(config, "VERIFIED_PARAMS", frozenset({"job_types", "posted_within", "salary"}))
    text = tool_error(make_server(), "search_jobs", roles=["analista"], countries=["CO"])
    assert text.startswith("INVALID_ARGUMENT: unknown argument countries; search_jobs takes roles, location, ")
    assert fake_api.calls == []


def test_empty_filter_lists_mean_no_filter(make_server: Callable[..., Any], fake_api: FakeApi) -> None:
    call(make_server(), "search_jobs", roles=[], countries=[], job_types=[])
    ((_, _, _, kwargs, _),) = fake_api.calls
    assert kwargs["roles"] == [] and kwargs["countries"] == [] and kwargs["job_types"] == []


def test_unexpected_errors_are_internal_with_a_redacted_traceback_on_stderr(
    make_server: Callable[..., Any], fake_api: FakeApi, capfd: pytest.CaptureFixture[str]
) -> None:
    register_secret(SENTINEL_B)
    server = make_server()
    fake_api.outcomes["get_company"] = RuntimeError(f"boom _portal_de_empleos_session={SENTINEL_A}; jar {SENTINEL_B}")
    assert tool_error(server, "get_company", slug_or_url="empresa-demo-54") == INTERNAL_TEXT

    fake_api.outcomes["saved_jobs"] = NetworkError(f"remember_user_token={SENTINEL_A} and {SENTINEL_B}")
    leaked = tool_error(server, "my_saved_jobs")
    assert leaked == "NETWORK: remember_user_token=*** and ***. Check your connection, then retry."

    out, err = capfd.readouterr()
    assert out == ""
    assert "Traceback" in err and "RuntimeError: boom _portal_de_empleos_session=***; jar ***" in err
    assert SENTINEL_A not in err and SENTINEL_B not in err


def test_protocol_round_trip_and_nothing_on_stdout(
    make_server: Callable[..., Any], fake_api: FakeApi, capfd: pytest.CaptureFixture[str]
) -> None:
    server = make_server()
    fake_api.outcomes["account_status"] = AuthRequired()

    async def session() -> tuple[Any, ...]:
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            found = await client.call_tool("search_jobs", {"roles": ["analista"], "max_results": 3})
            too_many = await client.call_tool("search_jobs", {"max_results": 46})
            denied = await client.call_tool("account_status", {})
            return listed, found, too_many, denied

    listed, found, too_many, denied = anyio.run(session)
    assert [tool.name for tool in listed.tools] == [*PUBLIC_TOOLS, *PRIVATE_TOOLS]
    assert not found.isError and json.loads(found.content[0].text)["kind"] == "job_search"
    assert found.structuredContent is None
    assert too_many.isError
    assert too_many.content[0].text == "INVALID_ARGUMENT: max_results: Input should be less than or equal to 45."
    assert denied.isError
    assert denied.content[0].text == f"AUTH_REQUIRED: Not logged in to Luk. {LOGIN_STEP}"
    assert capfd.readouterr().out == ""


# --- through the real api, session and http layers (no fake api; offline) ---------------------------


def test_luk_mcp_private_0_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, store: SessionStore, make_state: Callable[..., dict[str, Any]]
) -> None:
    """§7.1: LUK_MCP_PRIVATE=0 → the private tools are not registered and account_status omits the
    name and email; the default context factory reads the environment (no request is sent)."""
    store.save(make_state(), SessionMeta(name="Paz Soto", email="paz@example.com", logged_in_at=datetime.now(timezone.utc),
                                         browser="chromium", luk_cli_version="0.1.0"))
    shown = call(mcp_server.build_server(), "account_status")["data"]
    assert (shown["session_present"], shown["name"], shown["email"]) == (True, "Paz Soto", "paz@example.com")

    monkeypatch.setenv("LUK_MCP_PRIVATE", "0")
    server = mcp_server.build_server()
    assert list(list_tools(server)) == list(PUBLIC_TOOLS)
    hidden = call(server, "account_status")["data"]
    assert (hidden["session_present"], hidden["private_tools_enabled"]) == (True, False)
    assert hidden["name"] is None and hidden["email"] is None


@pytest.fixture
def real_server(settings: Settings, store: SessionStore, limiter: RateLimiter) -> Callable[..., Any]:
    """build_server over real ApiContexts whose takealuk traffic goes to an httpx.MockTransport."""

    def make(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
        transport = httpx.MockTransport(handler)
        return mcp_server.build_server(lambda: api.ApiContext(settings, store, limiter, luk_transport=transport))

    return make


def test_secrets_sentinel_through_an_mcp_tool_error(
    real_server: Callable[..., Any], saved_session: Callable[..., Any], capfd: pytest.CaptureFixture[str]
) -> None:
    """§8.2: Luk sends Set-Cookie SENTINEL_A while the jar holds SENTINEL_B; neither may reach stdout,
    stderr or the ToolError text — for a mapped LukError and for an unexpected exception."""
    saved_session(SENTINEL_B)
    blocked = real_server(lambda request: httpx.Response(
        403, headers={"Set-Cookie": f"_portal_de_empleos_session={SENTINEL_A}; path=/; secure; HttpOnly"}, text="no"))
    assert tool_error(blocked, "my_cvs").startswith("BLOCKED: Blocked by Luk (HTTP 403)")

    def explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"transport broke: Cookie: {request.headers['cookie']}; "
                           f"Set-Cookie: _portal_de_empleos_session={SENTINEL_A}")

    assert tool_error(real_server(explode), "my_saved_jobs") == INTERNAL_TEXT

    out, err = capfd.readouterr()
    assert out == ""
    assert "RuntimeError: transport broke: Cookie: _portal_de_empleos_session=***" in err
    for sentinel in (SENTINEL_A, SENTINEL_B):
        assert sentinel not in out and sentinel not in err


# --- packaging hygiene ----------------------------------------------------------------------------


def test_missing_mcp_extra_is_a_clear_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("mcp", "mcp.server", "mcp.server.fastmcp", "mcp.server.fastmcp.exceptions", "mcp.types"):
        monkeypatch.setitem(sys.modules, name, None)

    def factory() -> Any:
        raise AssertionError("no context may be built without the extra")

    with pytest.raises(LukError) as caught:
        mcp_server.build_server(factory)
    assert caught.value.exit_code == 1
    message = caught.value.message
    # repo-based hint: a bare `pip install "luk-cli[mcp]"` would resolve against PyPI (dependency confusion)
    assert 'pip install -e "' in message and 'luk-cli[mcp]"' in message and "not published on PyPI" in message


def test_importing_the_server_module_loads_neither_mcp_nor_playwright() -> None:
    code = "import sys, luk_cli.mcp_server; print([m for m in ('mcp', 'playwright') if m in sys.modules])"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


STDIO_SERVER = """
import types
from luk_cli import api, mcp_server
from luk_cli.models import AccountStatus, Envelope

ctx = types.SimpleNamespace(settings=types.SimpleNamespace(mcp_private=False), close=lambda: None)
api.ApiContext.from_env = staticmethod(lambda: ctx)
api.account_status = lambda ctx, check: Envelope(kind="account_status", data=AccountStatus(
    session_present=False, private_tools_enabled=False, login_instructions="Run `luk login`."))
mcp_server.main()
"""


def test_main_serves_stdio_and_stdout_carries_only_the_protocol() -> None:
    stray: list[Exception] = []

    async def collect(message: Any) -> None:
        if isinstance(message, Exception):  # a stdout line that is not JSON-RPC
            stray.append(message)

    async def session() -> tuple[Any, Any]:
        params = StdioServerParameters(command=sys.executable, args=["-c", STDIO_SERVER], env=dict(os.environ))
        with tempfile.TemporaryFile("w+") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, message_handler=collect) as client:
                    await client.initialize()
                    return await client.list_tools(), await client.call_tool("account_status", {})

    listed, status = anyio.run(session)
    assert [tool.name for tool in listed.tools] == list(PUBLIC_TOOLS)
    assert not status.isError and json.loads(status.content[0].text)["kind"] == "account_status"
    assert stray == []
