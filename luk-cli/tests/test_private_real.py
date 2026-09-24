"""Real private captures (spec §8.1 steps 3-4): runs only when `fixtures/private_real/` holds files.

That directory is git-ignored and stays on your own machine. It holds `luk debug capture` output
(`<name>.html` plus `<name>.meta.json`), reviewed and copied there by the user. Each capture must
parse with the parser of its page; once a parser is marked verified it must no longer warn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luk_cli import parsers
from luk_cli.config import santiago_today

PRIVATE_REAL = Path(__file__).parent / "fixtures" / "private_real"
BASE = "https://www.takealuk.com"
PAGES: dict[str, parsers.PrivatePage] = {path: page for page, path in parsers.PRIVATE_PATHS.items()}
CAPTURES = sorted(PRIVATE_REAL.glob("*.meta.json")) if PRIVATE_REAL.is_dir() else []

pytestmark = pytest.mark.skipif(not CAPTURES, reason="no real private captures in tests/fixtures/private_real/")


def capture(meta_path: Path) -> tuple[str, str]:
    """(requested route, scrubbed html) of one capture."""
    meta = json.loads(meta_path.read_text("utf-8"))
    html_path = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".html")
    return meta["path"].partition("?")[0], html_path.read_text("utf-8")


@pytest.mark.parametrize("meta_path", CAPTURES, ids=[p.name for p in CAPTURES])
def test_real_capture_parses(meta_path: Path):
    route, html = capture(meta_path)
    if route == "/":
        assert parsers.parse_whoami(html).logged_in
        return
    if route not in PAGES:
        pytest.skip(f"{route} is a public page")
    page = PAGES[route]
    parse = {
        "saved_jobs": lambda: parsers.parse_saved_jobs(html, today=santiago_today(), base_url=BASE),
        "application_histories": lambda: parsers.parse_applications(html, base_url=BASE),
        "cvs": lambda: parsers.parse_cvs(html),
    }[page]
    result = parse()
    assert result.verified == parsers.VERIFIED[page]
    assert (result.warnings == []) == result.verified
