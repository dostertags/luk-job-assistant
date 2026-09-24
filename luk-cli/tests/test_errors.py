"""errors.py — codes and exit codes of spec §5.3/§5.4."""

import os
import re
import shlex
import shutil
import subprocess
import sys

import pytest

from luk_cli import errors

TABLE = [
    (errors.InvalidArgument, "INVALID_ARGUMENT", 1),
    (errors.AuthRequired, "AUTH_REQUIRED", 2),
    (errors.NotFound, "NOT_FOUND", 3),
    (errors.NetworkError, "NETWORK", 4),
    (errors.RateLimited, "RATE_LIMITED", 4),
    (errors.Blocked, "BLOCKED", 4),
    (errors.BudgetExceeded, "BUDGET_EXCEEDED", 4),
    (errors.SiteChanged, "SITE_CHANGED", 5),
    (errors.Interrupted, "INTERRUPTED", 130),
    (errors.InternalError, "INTERNAL", 1),
]


@pytest.mark.parametrize(("cls", "code", "exit_code"), TABLE)
def test_codes_and_exit_codes(cls, code, exit_code):
    err = cls("boom")
    assert isinstance(err, errors.LukError)
    assert (err.code, err.exit_code, err.message, str(err)) == (code, exit_code, "boom", "boom")


def test_every_spec_code_has_an_exit_code():
    assert set(errors.EXIT_CODES) == {code for _, code, _ in TABLE}


def test_default_messages_follow_the_spec():
    assert errors.AuthRequired().message == "Session expired or missing. Run `luk login`."
    assert errors.Blocked().message == (
        "Blocked by Luk (HTTP 403) — stopping; not retrying (repo red line: no evasion)"
    )
    assert errors.Blocked.for_status(503).message.startswith("Blocked by Luk (HTTP 503)")
    assert errors.BudgetExceeded().message == "hourly budget reached"


def test_hint_defaults_and_override():
    assert errors.AuthRequired().hint
    assert errors.NotFound("x", hint="try y").hint == "try y"


def test_specialised_errors_keep_their_family():
    assert errors.SessionBusy("login already in progress").exit_code == 1
    assert errors.UnsafeRequest("POST refused").code == "INTERNAL"
    assert errors.ScrubFailed("identity found in <title>").exit_code == 1
    rejected = errors.AlgoliaRejected("suggest is disabled")
    assert isinstance(rejected, errors.Blocked) and rejected.exit_code == 4


def test_site_changed_names_page_selector_and_capture_command():
    err = errors.SiteChanged.missing_anchor("search", "turbo-frame#job_offers_results", "/job_offers?page=2")
    assert "search" in err.message
    assert "turbo-frame#job_offers_results" in err.message
    assert 'luk debug capture "/job_offers?page=2"' in err.message


SEARCH_CAPTURE = "/job_offers?job_positions=analista+financiero&locations=1021&job_types%5B%5D=intern"


def suggested_command(capture_path: str) -> str:
    message = errors.SiteChanged.missing_anchor("search", "x", capture_path).message
    return re.search(r"`(luk debug capture [^`]*)`", message)[1]


@pytest.mark.parametrize("capture_path", ["/", "/companies/empresa-demo-54?page=2", SEARCH_CAPTURE])
def test_the_suggested_capture_command_is_one_shell_command(capture_path):
    """§5.5 names `luk debug capture <path>` for the user to paste: with 2+ params the path holds `&`, which
    bash and cmd treat as a command separator, so the path is quoted and stays one argument."""
    lexer = shlex.shlex(suggested_command(capture_path), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    assert list(lexer) == ["luk", "debug", "capture", capture_path]


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"), reason="Windows PowerShell only")
def test_the_suggested_capture_command_parses_in_powershell():
    """§3 Windows 11 first: PowerShell refuses a bare `&` (AmpersandNotAllowed); the quoted path parses."""
    script = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($env:LUK_CMD, "
              "[ref]$null, [ref]$errors); $errors.Count")
    env = {**os.environ, "LUK_CMD": suggested_command(SEARCH_CAPTURE)}
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.stdout.strip() == "0", proc.stdout + proc.stderr
