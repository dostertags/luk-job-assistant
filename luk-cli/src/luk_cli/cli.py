"""`luk` command line (spec §5, §5.3).

Commands call only `luk_cli.api` and print exactly one thing on stdout: a JSON document (`--json`),
CSV with a BOM (`--csv`), or a table (plain when stdout is not a terminal). Warnings, progress, the
`Ubicación:` line and every error line go to stderr.

Errors: inside a command, `_reporting` turns any failure into its §5.3 exit code, prints the §5.4
error document on stdout under `--json`, and one redacted line on stderr (plus a hint line without
`--json`; a redacted traceback for unexpected errors under `--verbose`). `main()` reconfigures
stdout/stderr to UTF-8 (piped stdout is cp1252 on the reference machine), runs the app with
`standalone_mode=False` and maps whatever escapes a command the same way; click usage errors exit 1
(as an INVALID_ARGUMENT error document under `--json`). `login` / `session refresh` print nothing of
their own: `luk_cli.auth` reports every step through `notify` (stderr).

Search filters are registered only when their Phase-0 check passed (`config.VERIFIED_PARAMS`, §2.2):
an unverified option is absent from the command, not hidden. `LUK_TEST_FAKE_API=1` (tests only,
§8.2) swaps `luk_cli.api` for a `luk_fake_api` module found on sys.path.
"""

from __future__ import annotations

import importlib
import inspect
import io
import json
import os
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, TypeVar, get_args

import click
import typer
from rich.console import Console

from luk_cli import __version__, api, auth, config, formatters
from luk_cli.config import OptionalParam
from luk_cli.errors import InternalError, Interrupted, InvalidArgument, LukError
from luk_cli.inputs import COUNTRY_PARAM, CURRENCIES, JOB_TYPES, POSTED_WITHIN_PARAM
from luk_cli.models import SCHEMA_VERSION, Envelope, plain_text
from luk_cli.redact import redact

F = TypeVar("F", bound=Callable[..., Any])


def _typer(**options: Any) -> typer.Typer:
    return typer.Typer(pretty_exceptions_show_locals=False, no_args_is_help=True, add_completion=False, **options)


app = _typer(name="luk", help="Read-only personal client for Luk (takealuk.com).")
session_app = _typer(help="The stored Luk session: status and refresh.")
debug_app = _typer(help="Diagnostics for reporting a changed Luk page.")
app.add_typer(session_app, name="session")
app.add_typer(debug_app, name="debug")

JsonFlag = Annotated[bool, typer.Option(
    "--json", help=f"Print one JSON document (schema_version {SCHEMA_VERSION}) on stdout.")]
CsvFlag = Annotated[bool, typer.Option("--csv", help="Print CSV (UTF-8 with BOM, header row) on stdout.")]
PageOpt = Annotated[int, typer.Option("--page", help="Start page.")]
OffsetOpt = Annotated[int, typer.Option(
    "--offset", help="Results to skip on the start page (0-49, within that page only). To continue a list, pass next_page as --page and "
                     "next_offset as --offset (the table caption shows them).")]
BrowserOpt = Annotated[str, typer.Option(
    "--browser", click_type=click.Choice(get_args(auth.BrowserChoice)),
    help="auto = bundled Chromium, else msedge, else chrome.",
)]


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"luk-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option(
        "--version", callback=_print_version, is_eager=True, help="Show the version and exit.")] = False,
    verbose: Annotated[bool, typer.Option(
        "--verbose", help="One stderr line per HTTP hop (never headers, cookies or bodies).")] = False,
) -> None:
    """Read-only personal client for Luk (takealuk.com)."""
    ctx.obj = {"verbose": verbose}


# -- plumbing ------------------------------------------------------------------------------------------

def _api() -> Any:
    """`luk_cli.api`, or the test-only `luk_fake_api` module when LUK_TEST_FAKE_API=1 (spec §8.2)."""
    if os.environ.get("LUK_TEST_FAKE_API") == "1":
        return importlib.import_module("luk_fake_api")
    return api


def _verified_only(**gates: OptionalParam) -> Callable[[F], F]:
    """Unregister the options whose §2.2 param did not pass Phase 0 (`config.VERIFIED_PARAMS`).

    `gates` maps a parameter name to its param; a dropped option keeps its Python default.
    """

    def apply(func: F) -> F:
        signature = inspect.signature(func, eval_str=True)
        kept = [p for p in signature.parameters.values()
                if p.name not in gates or gates[p.name] in config.VERIFIED_PARAMS]
        func.__signature__ = signature.replace(parameters=kept)  # type: ignore[attr-defined]
        return func

    return apply


def _verbose(ctx: typer.Context) -> bool:
    return bool((ctx.obj or {}).get("verbose"))


def _say(message: str) -> None:
    """One stderr line, without terminal control characters (it may quote Luk text, e.g. a name)."""
    click.echo(plain_text(message), err=True)


def _report(err: BaseException, *, as_json: bool, verbose: bool) -> int:
    """Print `err` per §5.3/§5.4 and return its exit code."""
    if isinstance(err, LukError):
        error = err
    elif isinstance(err, (KeyboardInterrupt, click.exceptions.Abort)):
        error = Interrupted()
    else:
        error = InternalError(f"unexpected error: {type(err).__name__}: {' '.join(str(err).split())}")
    if as_json:
        click.echo(formatters.error_document(error))
    _say(redact(f"luk: {error.message}"))
    if error.hint and not as_json:
        _say(redact(f"hint: {error.hint}"))
    if verbose and not isinstance(err, (LukError, KeyboardInterrupt, click.exceptions.Abort)):
        _say(redact("".join(traceback.format_exception(type(err), err, err.__traceback__))).rstrip())
    return error.exit_code


@contextmanager
def _reporting(ctx: typer.Context, *, as_json: bool = False) -> Iterator[None]:
    try:
        yield
    except (click.exceptions.Exit, click.ClickException):
        raise
    except (Exception, KeyboardInterrupt) as err:
        raise typer.Exit(_report(err, as_json=as_json, verbose=_verbose(ctx))) from None


def _call(ctx: typer.Context, call: Callable[[Any], Any]) -> Any:
    """Run `call` with a fresh ApiContext (closed afterwards)."""
    api_ctx = _api().ApiContext.from_env(verbose=_verbose(ctx))
    try:
        return call(api_ctx)
    finally:
        api_ctx.close()


def _emit(ctx: typer.Context, call: Callable[[Any], Envelope[Any]], *, as_json: bool, as_csv: bool = False) -> None:
    """Run a data command and print its warnings (stderr) and its one stdout document."""
    with _reporting(ctx, as_json=as_json):
        if as_json and as_csv:
            raise InvalidArgument("choose one of --json and --csv")
        envelope = _call(ctx, call)
    formatters.render_warnings(envelope.warnings, Console(stderr=True))
    if as_json:
        click.echo(formatters.json_document(envelope))
    elif as_csv:
        click.echo(formatters.csv_document(envelope), nl=False)
    else:
        formatters.render_table(envelope, formatters.stdout_console())


# -- public data commands ------------------------------------------------------------------------------

@app.command()
@_verified_only(country="countries", worldwide="worldwide", posted_within="posted_within", job_type="job_types",
                min_salary="salary", max_salary="salary", currency="salary")
def search(
    ctx: typer.Context,
    roles: Annotated[list[str] | None, typer.Argument(
        help='Job titles, one pill each (OR\'d); quote multi-word roles: "analista financiero".',
        show_default=False)] = None,
    location: Annotated[list[str] | None, typer.Option(
        "--location", "-l", help="Area name, resolved via Luk's area search (repeatable).")] = None,
    location_id: Annotated[list[int] | None, typer.Option(
        "--location-id", help="Luk area id, e.g. 1318 (repeatable).")] = None,
    country: Annotated[list[str] | None, typer.Option(
        "--country", click_type=click.Choice(tuple(COUNTRY_PARAM), case_sensitive=False),
        help="Country (repeatable); searches outside the Chile default.")] = None,
    worldwide: Annotated[bool, typer.Option("--worldwide", help="Search every country.")] = False,
    posted_within: Annotated[str | None, typer.Option(
        "--posted-within", click_type=click.Choice(tuple(POSTED_WITHIN_PARAM)), help="Posted within.")] = None,
    job_type: Annotated[list[str] | None, typer.Option(
        "--type", click_type=click.Choice(JOB_TYPES), help="Job type (repeatable).")] = None,
    min_salary: Annotated[int | None, typer.Option("--min-salary", help="Minimum salary (integer).")] = None,
    max_salary: Annotated[int | None, typer.Option("--max-salary", help="Maximum salary (integer).")] = None,
    currency: Annotated[str | None, typer.Option(
        "--currency", click_type=click.Choice(CURRENCIES, case_sensitive=False),
        help="Salary currency (CLP when a bound is given).")] = None,
    page: PageOpt = 1,
    offset: OffsetOpt = 0,
    limit: Annotated[int, typer.Option("--limit", help="Results to return (1-150).")] = 15,
    as_json: JsonFlag = False,
    as_csv: CsvFlag = False,
) -> None:
    """Search job offers (default location: Chile)."""
    _emit(ctx, lambda c: _api().search_jobs(
        c, roles=list(roles or ()), locations=list(location or ()), location_ids=list(location_id or ()),
        countries=list(country or ()), worldwide=worldwide, posted_within=posted_within,
        job_types=list(job_type or ()), min_salary=min_salary, max_salary=max_salary, currency=currency,
        page=page, offset=offset, limit=limit,
    ), as_json=as_json, as_csv=as_csv)


@app.command()
def show(
    ctx: typer.Context,
    slug_or_url: Annotated[str, typer.Argument(help="Job slug or https://www.takealuk.com/job_offers/<slug>.")],
    as_json: JsonFlag = False,
) -> None:
    """Show one job offer."""
    _emit(ctx, lambda c: _api().get_job(c, slug_or_url), as_json=as_json)


@app.command()
def suggest(
    ctx: typer.Context,
    prefix: str,
    limit: Annotated[int, typer.Option("--limit", help="Suggestions (1-20).")] = 5,
    as_json: JsonFlag = False,
) -> None:
    """Luk's search suggestions for a prefix."""
    _emit(ctx, lambda c: _api().suggest(c, prefix, limit=limit), as_json=as_json)


@app.command()
def areas(
    ctx: typer.Context,
    text: str,
    context: Annotated[str, typer.Option("--for", click_type=click.Choice(
        ("jobs", "companies") if "areas_companies" in config.VERIFIED_PARAMS else ("jobs",)),
        help="Search context.")] = "jobs",
    as_json: JsonFlag = False,
) -> None:
    """Find Luk area ids for a place name."""
    _emit(ctx, lambda c: _api().find_areas(c, text, context=context), as_json=as_json)


@app.command("similar-roles")
def similar_roles(
    ctx: typer.Context,
    role: str,
    limit: Annotated[int, typer.Option("--limit", help="Roles (1-9).")] = 9,
    as_json: JsonFlag = False,
) -> None:
    """Job titles Luk considers similar to ROLE."""
    _emit(ctx, lambda c: _api().similar_roles(c, role, limit=limit), as_json=as_json)


@app.command()
@_verified_only(location="companies_location")
def companies(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Company name text.")] = None,
    location: Annotated[str | None, typer.Option("--location", "-l", help="Area name.")] = None,
    page: PageOpt = 1,
    offset: OffsetOpt = 0,
    limit: Annotated[int, typer.Option("--limit", help="Results to return (1-150).")] = 24,
    as_json: JsonFlag = False,
    as_csv: CsvFlag = False,
) -> None:
    """Search the company directory."""
    _emit(ctx, lambda c: _api().search_companies(c, query=query, location=location, page=page, offset=offset,
                                                 limit=limit), as_json=as_json, as_csv=as_csv)


@app.command()
def company(
    ctx: typer.Context,
    slug_or_url: Annotated[str, typer.Argument(help="Company slug or https://www.takealuk.com/companies/<slug>.")],
    page: Annotated[int, typer.Option("--page", help="Page of the company's offers.")] = 1,
    as_json: JsonFlag = False,
) -> None:
    """Show a company and one page of its offers."""
    _emit(ctx, lambda c: _api().get_company(c, slug_or_url, page=page), as_json=as_json)


# -- private data commands (session required) -------------------------------------------------------------

@app.command()
def saved(
    ctx: typer.Context,
    page: PageOpt = 1,
    offset: OffsetOpt = 0,
    limit: Annotated[int, typer.Option("--limit", help="Results to return (1-150).")] = 15,
    details: Annotated[bool, typer.Option("--details", help="Add the full offer for up to 20 cards.")] = False,
    as_json: JsonFlag = False,
    as_csv: CsvFlag = False,
) -> None:
    """Your saved jobs (needs `luk login`)."""
    _emit(ctx, lambda c: _api().saved_jobs(c, page=page, offset=offset, limit=limit, details=details),
          as_json=as_json, as_csv=as_csv)


@app.command()
def applications(
    ctx: typer.Context,
    page: PageOpt = 1,
    offset: OffsetOpt = 0,
    limit: Annotated[int, typer.Option("--limit", help="Results to return (1-150).")] = 15,
    as_json: JsonFlag = False,
    as_csv: CsvFlag = False,
) -> None:
    """Your job applications (needs `luk login`)."""
    _emit(ctx, lambda c: _api().applications(c, page=page, offset=offset, limit=limit),
          as_json=as_json, as_csv=as_csv)


@app.command()
def cvs(ctx: typer.Context, as_json: JsonFlag = False) -> None:
    """Your CVs on Luk: metadata only, never the files (needs `luk login`)."""
    _emit(ctx, lambda c: _api().cvs(c), as_json=as_json)


@app.command()
def whoami(ctx: typer.Context, as_json: JsonFlag = False) -> None:
    """The Luk account of the stored session."""
    _emit(ctx, lambda c: _api().whoami(c), as_json=as_json)


# -- session -----------------------------------------------------------------------------------------------

@app.command()
def login(
    ctx: typer.Context,
    browser: BrowserOpt = "auto",
    timeout: Annotated[int | None, typer.Option("--timeout", min=1, help="Seconds to wait (default 300).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Log in again even if the session works.")] = False,
    paste_cookie: Annotated[bool, typer.Option(
        "--paste-cookie", help="No browser: paste the session cookie at a hidden prompt (terminal only).")] = False,
) -> None:
    """Log in once in a browser window; you type your own credentials."""
    with _reporting(ctx):
        _call(ctx, lambda c: _api().login(c, browser=browser, timeout_s=timeout, force=force,
                                          paste_cookie=paste_cookie, notify=_say))


@app.command()
def logout(ctx: typer.Context) -> None:
    """Delete the stored session (local only)."""
    with _reporting(ctx):
        existed = _call(ctx, lambda c: _api().logout(c))
    _say("Logged out: the local session was deleted (Luk's server-side session is not revoked)." if existed
         else "No local session to delete.")


@session_app.command("status")
def session_status(
    ctx: typer.Context,
    check: Annotated[bool, typer.Option("--check", help="Also ask Luk whether the session still works.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Where the session is stored and what it holds (never cookie values)."""
    _emit(ctx, lambda c: _api().session_status(c, check=check), as_json=as_json)


@session_app.command("refresh")
def session_refresh(ctx: typer.Context, browser: BrowserOpt = "auto") -> None:
    """Re-run the login window starting from the saved session."""
    with _reporting(ctx):
        _call(ctx, lambda c: _api().refresh_session(c, browser=browser, notify=_say))


# -- tools ---------------------------------------------------------------------------------------------------

@app.command("open")
def open_(
    ctx: typer.Context,
    targets: Annotated[list[str], typer.Argument(help="1-5 job (or, with --company, company) slugs or URLs.")],
    company: Annotated[bool, typer.Option("--company", help="Targets are companies.")] = False,
) -> None:
    """Open offers in your browser (you apply there yourself)."""
    with _reporting(ctx):
        urls = _call(ctx, lambda c: _api().open_targets(c, targets, company=company))
    for url in urls:
        _say(f"Opened {url}")


@app.command()
def doctor(
    ctx: typer.Context,
    live: Annotated[bool, typer.Option("--live", help="Also fetch and parse one page of each public type.")] = False,
    print_mcp_json: Annotated[bool, typer.Option(
        "--print-mcp-json", help="Print an absolute MCP server config for this interpreter.")] = False,
) -> None:
    """Check the installation (never prints cookie values)."""
    if print_mcp_json:
        click.echo(json.dumps(api.mcp_config(), indent=2, ensure_ascii=False))
        return
    with _reporting(ctx):
        checks = _call(ctx, lambda c: _api().doctor(c, live=live))
    for check in checks:
        mark = {True: "ok", False: "FAIL", None: "--"}[check.ok]
        click.echo(plain_text(f"{mark:<5}{check.name:<22}{check.detail}"))  # --live quotes Luk text
    if any(check.ok is False for check in checks):
        raise typer.Exit(1)


@debug_app.command("capture")
def debug_capture(
    ctx: typer.Context,
    path: Annotated[str, typer.Argument(help="Allowlisted Luk path, e.g. / or /saved_jobs (never a URL).")],
    out: Annotated[Path | None, typer.Option("--out", file_okay=False, help="Output dir (default: cache).")] = None,
) -> None:
    """Save a scrubbed copy of one Luk page for fixing a parser."""
    with _reporting(ctx):
        written = _call(ctx, lambda c: _api().debug_capture(c, path, out_dir=out))
    click.echo(str(written))
    _say("Scrubbed capture written; review it before sharing or copying it into tests/fixtures/private_real/.")


@app.command()
def schema(
    ctx: typer.Context,
    kind: Annotated[str | None, typer.Argument(help="One kind (default: all).", show_default=False)] = None,
    out: Annotated[Path | None, typer.Option(
        "--out", file_okay=False, help="Write <kind>.json files here (e.g. docs/schema) instead.")] = None,
) -> None:
    """The JSON Schema of each --json document."""
    with _reporting(ctx):
        found = api.schemas()
        if kind is not None and kind not in found:
            raise InvalidArgument(f"unknown kind '{kind}'; choose one of: {', '.join(found)}")
        selected = found if kind is None else {kind: found[kind]}
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            for name, document in selected.items():
                (out / f"{name}.json").write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode())
                _say(f"Wrote {out / f'{name}.json'}")
            return
    click.echo(json.dumps(selected if kind is None else selected[kind], indent=2, ensure_ascii=False))


@app.command("mcp")
def mcp_command(ctx: typer.Context) -> None:
    """Run the MCP stdio server (requires installing luk-cli with its mcp extra)."""
    with _reporting(ctx):
        from luk_cli.mcp_server import main as serve  # lazy; refuses to start without the [mcp] extra

        serve()


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point (`luk = luk_cli.cli:main`); returns the process exit code."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    try:
        result = app(args=args, prog_name="luk", standalone_mode=False)
    except click.ClickException as err:
        if as_json:  # §5.4: under --json even a usage error is the error document plus one stderr line
            return _report(InvalidArgument(err.format_message()), as_json=True, verbose=False)
        shown = io.StringIO()  # click's usage text, then the §4.6 final redaction pass (it echoes argv)
        err.show(file=shown)
        click.echo(redact(shown.getvalue()), err=True, nl=False)
        return 1
    except (Exception, KeyboardInterrupt) as err:
        return _report(err, as_json=as_json, verbose="--verbose" in args)
    return result if isinstance(result, int) else 0
