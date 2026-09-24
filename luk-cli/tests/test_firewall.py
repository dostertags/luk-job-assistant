"""Package firewall (ADR-0001 (docs/adr/0001-luk-cli-read-only-personal-client.md at the repo root), spec §1.7,
§8.2), by AST scan.

The repository holds three independent sibling packages: `luk-cli/` (this read-only client), `luk-scraper/`
(package `luk_scraper`, the public crawler) and `luk-assist/` (package `luk_assist`, which pre-fills an
application form in a visible browser and stops). `luk_cli` imports only the standard library, its
declared dependencies and itself, so the read-only client never imports the form-filling tool or the
crawler under any name. In the other direction, nothing under `luk-scraper/src` or `luk-assist/src`, and no
other Python file of the repository outside `luk-cli/`, imports `luk_cli`. And the two siblings never
import each other: the crawler cannot fill forms and the form filler cannot crawl. This is the one
repo-level firewall; it sees the dynamic import forms too.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

LUK_CLI = Path(__file__).resolve().parents[1]
SOURCE = LUK_CLI / "src" / "luk_cli"
REPO = LUK_CLI.parent
# The declared dependencies (pyproject §3) plus anyio, which httpx itself depends on. `luk_fake_api` is
# the test-only LUK_TEST_FAKE_API module (spec §8.2).
ALLOWED = frozenset({
    "luk_cli", "typer", "click", "rich", "httpx", "pydantic", "selectolax", "platformdirs", "filelock",
    "playwright", "mcp", "anyio", "luk_fake_api",
})
# Imported but guaranteed by another declared dependency (httpx requires anyio).
TRANSITIVE_OK = frozenset({"anyio"})
PYPROJECT = LUK_CLI / "pyproject.toml"
# Sibling package directory -> the import name of its package.
SIBLINGS = {"luk-scraper": "luk_scraper", "luk-assist": "luk_assist"}
SKIPPED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "luk-cli"})


DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


def _dynamic_import(node: ast.Call) -> bool:
    """`importlib.import_module(…)`, a bare `import_module(…)`, or `__import__(…)` (also `builtins.__import__`)."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    return name in DYNAMIC_IMPORTS


def imported_modules(path: Path) -> Iterator[str]:
    """Top-level names of every absolute import in `path`: `import x`, `from x import y`, and the dynamic
    forms `importlib.import_module("x")`, `import_module("x")` and `__import__("x")`. An aliased
    `import_module` (`from importlib import import_module as load`) is resolved through its alias."""
    tree = ast.parse(path.read_bytes(), filename=str(path))
    aliases = {alias.asname for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "importlib"
               for alias in node.names if alias.name == "import_module" and alias.asname}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]
        elif (isinstance(node, ast.Call) and (_dynamic_import(node) or (isinstance(node.func, ast.Name)
                                                                         and node.func.id in aliases))
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            yield node.args[0].value.split(".")[0]


@pytest.mark.parametrize("source", [
    "import luk_assist.fill",
    "from luk_assist import fill",
    "import importlib\ndef f():\n    return importlib.import_module('luk_assist.fill')",
    "from importlib import import_module\ndef f():\n    return import_module('luk_assist.fill')",
    "from importlib import import_module as load\ndef f():\n    return load('luk_assist.fill')",
    "def f():\n    return __import__('luk_assist')",
    "import builtins\ndef f():\n    return builtins.__import__('luk_assist')",
], ids=["import", "from", "importlib.import_module", "import_module", "aliased", "__import__", "builtins"])
def test_the_scan_sees_every_import_form(tmp_path, source):
    """The scan is not blind to the dynamic forms: each one names the package it loads."""
    module = tmp_path / "probe.py"
    module.write_text(source, "utf-8")
    assert "luk_assist" in set(imported_modules(module))


def luk_cli_sources() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def test_luk_cli_imports_only_stdlib_and_its_dependencies():
    foreign = {
        f"{path.name}: {name}"
        for path in luk_cli_sources()
        for name in imported_modules(path)
        if name not in ALLOWED and name not in sys.stdlib_module_names
    }
    assert luk_cli_sources() and not foreign


def declared_distributions() -> set[str]:
    """Distribution names in pyproject's dependencies and optional-dependencies (no tomllib on 3.10)."""
    text = PYPROJECT.read_text("utf-8")
    names: set[str] = set()
    for block in re.findall(r"(?ms)^(?:dependencies|[A-Za-z0-9_-]+)\s*=\s*\[(.*?)\]", text):
        for spec in re.findall(r"\"([^\"]+)\"", block):
            name = re.match(r"[A-Za-z0-9_.-]+", spec)
            if name:
                names.add(name.group(0).lower().replace("-", "_"))
    return names


def test_every_third_party_import_is_a_declared_dependency():
    """A dependency that merely arrives transitively can vanish: typer >= 0.27 stopped installing click,
    which luk_cli imports directly. Every third-party import must be declared in pyproject.toml."""
    declared = declared_distributions()
    undeclared = {
        f"{path.name}: {name}"
        for path in luk_cli_sources()
        for name in imported_modules(path)
        if name not in sys.stdlib_module_names and name not in {"luk_cli", "luk_fake_api"}
        and name not in declared and name not in TRANSITIVE_OK
    }
    assert "click" in declared and not undeclared


def test_luk_cli_never_imports_a_sibling_package():
    """The read-only client never imports the form-filling tool (`luk_assist`) or the crawler (`luk_scraper`)."""
    for path in luk_cli_sources():
        assert not set(imported_modules(path)) & set(SIBLINGS.values()), path.name


@pytest.mark.parametrize("sibling", sorted(SIBLINGS))
def test_a_sibling_package_never_imports_luk_cli(sibling):
    files = sorted((REPO / sibling / "src").rglob("*.py"))
    if not files:
        pytest.skip(f"no {sibling}/src next to luk-cli/")
    importers = [
        str(path.relative_to(REPO))
        for path in files
        if b"luk_cli" in path.read_bytes() and "luk_cli" in imported_modules(path)
    ]
    assert importers == []


@pytest.mark.parametrize(("sibling", "other"), [("luk-scraper", "luk_assist"), ("luk-assist", "luk_scraper")])
def test_the_siblings_never_import_each_other(sibling, other):
    """luk_scraper never imports luk_assist and vice versa (dynamic forms included, every sub-module)."""
    files = sorted((REPO / sibling / "src").rglob("*.py"))
    if not files:
        pytest.skip(f"no {sibling}/src next to luk-cli/")
    importers = [str(path.relative_to(REPO)) for path in files if other in set(imported_modules(path))]
    assert importers == []


def repo_python_files() -> Iterator[Path]:
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS and not d.startswith(".")]
        yield from (Path(root, name) for name in files if name.endswith(".py"))


def test_nothing_outside_luk_cli_imports_it():
    if not any((REPO / sibling).is_dir() for sibling in SIBLINGS):
        pytest.skip("luk-cli is not inside the multi-package repository")
    importers = [
        str(path.relative_to(REPO))
        for path in repo_python_files()
        if b"luk_cli" in path.read_bytes() and "luk_cli" in imported_modules(path)
    ]
    assert importers == []
