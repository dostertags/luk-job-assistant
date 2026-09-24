"""The README and every user-facing hint install from your checkout, and work in Windows shells."""

from __future__ import annotations

import re

import pytest
from conftest import PACKAGE_ROOT, SRC_DIR

README = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
# `pip install "luk-assist[...]"` without -e would fetch whatever package of that name PyPI has
# (luk-assist is not published there: dependency confusion).
BARE_INSTALL = re.compile(r"pip3?\s+install\s+(?:(?:-U|--upgrade)\s+)?(?!-e\b|--editable\b)[\"']?luk-assist\b")


@pytest.mark.parametrize("path", [PACKAGE_ROOT / "README.md", *sorted(SRC_DIR.rglob("*.py"))],
                         ids=lambda p: p.name)
def test_no_bare_install_of_the_package_name(path):
    assert not BARE_INSTALL.search(path.read_text(encoding="utf-8"))


def test_bare_install_pattern_is_itself_tested():
    assert BARE_INSTALL.search('pip install "luk-assist[browser]"')
    assert BARE_INSTALL.search("python -m pip install -U luk-assist")
    assert not BARE_INSTALL.search('python -m pip install -e "<path to luk-job-assistant>/luk-assist[browser]"')


def test_readme_installs_from_the_checkout_with_a_recent_pip():
    assert 'python -m pip install -U "pip>=21.3"' in README
    assert "python -m pip install -e" in README
    assert not re.search(r"(?m)^\s*pip3?\s+install", README)  # always `python -m pip`


def test_readme_shows_the_session_path_for_cmd_and_powershell():
    assert r'"%LOCALAPPDATA%\luk-cli\session.json"' in README
    assert r'"$env:LOCALAPPDATA\luk-cli\session.json"' in README
    assert "PowerShell" in README and "cmd" in README


def test_readme_saves_luk_cli_json_safely_in_powershell():
    assert "Out-File -Encoding utf8 offers.json" in README


def test_readme_runs_the_tests_without_posix_only_syntax():
    assert "PYTHONPATH=src" not in README
    assert "python -m pytest -q" in README
