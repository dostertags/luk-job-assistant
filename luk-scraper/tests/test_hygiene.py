"""Guards for the red lines: GET-only, standalone package, fake-only fixtures (static checks)."""
from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "luk_scraper"
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _modules(root: pathlib.Path) -> list[pathlib.Path]:
    """Every ``.py`` file under ``root``, sub-packages included, in a stable order."""
    return sorted(root.rglob("*.py"), key=lambda p: p.relative_to(root).as_posix())


MODULES = _modules(SRC)

WRITE_METHODS = {"post", "put", "patch", "delete", "request", "send"}
ALLOWED_THIRD_PARTY = {"requests", "bs4"}
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "argparse", "ast", "builtins", "csv", "dataclasses", "datetime", "email", "html", "http",
    "importlib", "json", "logging", "math", "os", "pathlib", "re", "sys", "time", "typing",
    "unicodedata", "urllib", "__future__"}
ALLOWED_TOP = STDLIB | ALLOWED_THIRD_PARTY | {"luk_scraper"}
#: Never imported, wherever they appear in a dotted name (``from os import luk_assist`` too): the
#: sibling packages and browser automation.
FORBIDDEN = {"luk_cli", "luk_assist", "playwright", "selenium", "seleniumbase", "pyppeteer",
             "undetected_chromedriver", "nodriver", "splinter", "helium", "mechanize",
             "mechanicalsoup"}
DYNAMIC_IMPORTERS = {"import_module", "__import__"}


def _tree(path: pathlib.Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _bad_name(name: str) -> bool:
    parts = [p for p in name.split(".") if p]
    return (not parts or parts[0] not in ALLOWED_TOP
            or any(p.lower() in FORBIDDEN for p in parts))


def import_violations(tree: ast.AST, depth: int = 1) -> list[str]:
    """Every import in ``tree`` that is not stdlib / requests / bs4 / luk_scraper, or that names
    a sibling package or a browser-automation library anywhere in its dotted name.

    ``depth`` is how deep the module sits below ``luk_scraper`` (1 for ``luk_scraper/x.py``): a
    relative import going further up leaves the package and is always a violation. Dynamic
    imports (``importlib.import_module`` / ``__import__``, aliased or not) are checked like
    static ones; with a computed (non-literal) name they are a violation, since the scan cannot
    tell what they load.
    """
    importers = set(DYNAMIC_IMPORTERS)
    for node in ast.walk(tree):          # from importlib import import_module as im
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "builtins"):
            importers |= {a.asname or a.name for a in node.names if a.name in DYNAMIC_IMPORTERS}

    problems = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > depth:
                problems.append(f"line {node.lineno}: relative import leaves luk_scraper")
                continue
            base = node.module or ""
            if node.level:
                base = "luk_scraper" + (f".{base}" if base else "")
            names = [base] + [f"{base}.{a.name}" for a in node.names if a.name != "*"]
        elif isinstance(node, ast.Call):
            func = node.func
            called = (func.id if isinstance(func, ast.Name)
                      else func.attr if isinstance(func, ast.Attribute) else None)
            if called not in importers:
                continue
            arg = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "name"), None)
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                problems.append(f"line {node.lineno}: dynamic import of a computed name")
                continue
            names = [arg.value]
        for name in names:
            if _bad_name(name):
                problems.append(f"line {node.lineno}: imports {name}")
    return problems


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_write_http_methods(path):
    """luk-scraper only ever sends GET requests."""
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in WRITE_METHODS, (
                f"{path.name}:{node.lineno} calls .{node.func.attr}()")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_imports_are_stdlib_requests_bs4_or_own(path):
    """Standalone: never imports luk_cli / luk_assist (or anything else)."""
    depth = len(path.relative_to(SRC).parts)
    assert import_violations(_tree(path), depth) == [], path.name


# --- the import scan itself must not have blind spots ---------------------------------------

@pytest.mark.parametrize("source", [
    "import luk_cli",
    "import luk_assist.runner",
    "from luk_cli.http import Client",
    "from luk_scraper import luk_cli",                          # ImportFrom: module + name
    "from os import luk_assist",
    "from .. import luk_cli",                                   # relative, leaves the package
    "from ..luk_assist import runner",
    "import importlib\nimportlib.import_module('luk_cli')",     # dynamic imports
    "import importlib\nimportlib.import_module('luk_assist.runner')",
    "from importlib import import_module\nimport_module('luk_cli.auth')",
    "import importlib as il\nil.import_module(name='luk_cli')",
    "__import__('luk_assist')",
    "import builtins\nbuiltins.__import__('luk_cli')",
    "import importlib\nimportlib.import_module('play' + 'wright')",   # computed name
    "import importlib\nname = 'luk_cli'\nimportlib.import_module(name)",
    "import playwright",
    "import importlib\nimportlib.import_module('selenium')",
])
def test_import_scan_catches(source):
    assert import_violations(ast.parse(source)), source


def test_import_scan_accepts_the_allowed_imports():
    source = ("from __future__ import annotations\nimport requests\nfrom bs4 import BeautifulSoup\n"
              "from . import config\nfrom .fetcher import Fetcher\nimport luk_scraper.cli\n"
              "import importlib\nimportlib.import_module('json')\n")
    assert import_violations(ast.parse(source)) == []


def test_module_scan_is_recursive(tmp_path):
    (tmp_path / "top.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("import luk_cli\n", encoding="utf-8")
    found = [p.relative_to(tmp_path).as_posix() for p in _modules(tmp_path)]
    assert found == ["sub/nested.py", "top.py"]


def test_no_browser_or_login_code():
    text = "\n".join(p.read_text(encoding="utf-8") for p in MODULES).lower()
    for word in ("playwright", "selenium", "password", "sign_in(", "set_cookie(",
                 "user-agent\": \"mozilla"):
        assert word not in text


# --- fixtures must be fake ----------------------------------------------------------------

FIXTURE_FILES = sorted(FIXTURES.glob("*.html"))


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_urls_are_luk_or_example(path):
    text = path.read_text(encoding="utf-8")
    for host in re.findall(r"https?://([^/\"'\s<>]+)", text):
        assert host in {"www.takealuk.com", "example.com", "schema.org"}, host


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_has_no_real_identifiers(path):
    text = path.read_text(encoding="utf-8")
    for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        assert email.endswith("@example.com"), email
    for rut in re.findall(r"\b\d{1,2}\.\d{3}\.\d{3}-[\dkK]\b", text):
        assert rut == "11.111.111-0", rut
    for token in re.findall(r'name="authenticity_token"[^>]*value="([^"]*)"', text):
        assert token.startswith("FAKE-"), token
    for company in re.findall(r'<p class="item-title[^"]*">\s*([^<]+?)\s*</p>', text):
        assert re.fullmatch(r"Empresa Demo \d\d SpA", company), company
