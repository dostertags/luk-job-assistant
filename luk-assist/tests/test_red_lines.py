"""The red line, enforced on the source: luk-assist pre-fills and STOPS.

An AST scan of every module in src/luk_assist (subpackages included) fails on any way to submit or
interact beyond typing text: clicks, key presses, focus, drag and drop, checking / selecting, file
uploads, form submission, synthetic events, page scripts, request routing, raw HTTP through the logged-in
browser (``.request``), reloading or navigating a tab you are reviewing, and the keyboard / mouse /
touchscreen objects. Navigation (``.goto``) is allowed only in ``_goto_new_tab``, on a page that method got
from ``self._new_page()``. Browser launches must pass the literal ``headless=False`` and no extra Chromium
``args=`` / ``ignore_default_args=`` / ``**kwargs``. Network libraries, luk-cli and luk-scraper cannot be
imported, statically or dynamically. It also fails on any function, method or class whose name suggests
sending an application. The scanner is itself tested, so it cannot silently pass by scanning nothing.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from conftest import SRC_DIR

from luk_assist import browser as B

FORBIDDEN_METHODS = frozenset({
    # acting on the page
    "click", "dblclick", "tap", "press", "press_sequentially", "type", "check", "uncheck", "set_checked",
    "select_option", "set_input_files", "submit", "request_submit", "dispatch_event", "drag_to",
    "drag_and_drop", "focus",
    # page scripts
    "evaluate", "evaluate_all", "evaluate_handle", "eval_on_selector", "eval_on_selector_all",
    "wait_for_function", "add_script_tag", "add_init_script", "expose_function", "expose_binding",
    # rewriting requests or the page, or leaving a form you are reviewing
    "route", "unroute", "unroute_all", "route_from_har", "route_web_socket", "set_content", "reload",
    "go_back", "go_forward",
    # other browsers / raw HTTP
    "connect_over_cdp", "launch_server", "urlopen",
})
# `.request` is Playwright's APIRequestContext (ctx.request.post, page.request.fetch): raw HTTP that
# carries your logged-in cookies. It is also urllib.request.
FORBIDDEN_OBJECTS = frozenset({"keyboard", "mouse", "touchscreen", "request"})
FORBIDDEN_NAME_PARTS = ("submit", "send", "enviar", "postular", "apply_now")
FORBIDDEN_IMPORTS = ("luk_cli", "luk_scraper", "requests", "httpx", "urllib.request", "urllib3", "http.client",
                     "http.server", "httplib2", "socket", "aiohttp", "ftplib", "smtplib", "telnetlib",
                     "websocket", "websockets", "xmlrpc")
LAUNCHERS = frozenset({"launch", "launch_persistent_context"})
LAUNCH_FORBIDDEN_KEYWORDS = frozenset({"args", "ignore_default_args"})
GOTO_ONLY_IN = "_goto_new_tab"
DYNAMIC_IMPORTERS = frozenset({"__import__", "import_module"})


def _forbidden_module(name: str) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS)


def _enclosing_functions(tree: ast.AST) -> dict[int, ast.AST | None]:
    """id(node) -> the innermost def that contains it (None at module or class level)."""
    owner: dict[int, ast.AST | None] = {}

    def visit(node: ast.AST, function: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            owner[id(child)] = function
            visit(child, child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function)

    visit(tree, None)
    return owner


def _is_new_tab_goto(call: ast.Call, function: ast.AST | None) -> bool:
    """`page.goto(...)` inside _goto_new_tab, where `page` is assigned only from `self._new_page()`."""
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name != GOTO_ONLY_IN:
        return False
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    if not isinstance(receiver, ast.Name):
        return False
    assigned = [n.value for n in ast.walk(function)
                if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr))
                and any(isinstance(t, ast.Name) and t.id == receiver.id
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    return bool(assigned) and all(
        isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "_new_page"
        and isinstance(v.func.value, ast.Name) and v.func.value.id == "self" and not v.args and not v.keywords
        for v in assigned
    )


def violations(source: str, filename: str = "<source>") -> list[str]:
    found: list[str] = []
    tree = ast.parse(source, filename=filename)
    owner = _enclosing_functions(tree)
    importers = set(DYNAMIC_IMPORTERS)
    for node in ast.walk(tree):  # `from importlib import import_module as load` -> `load` is an importer too
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            importers.update(a.asname for a in node.names if a.name in DYNAMIC_IMPORTERS and a.asname)
    allowed_goto = {id(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute) and c.func.attr == "goto"
                    and _is_new_tab_goto(c, owner.get(id(c)))}
    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_METHODS or node.attr in FORBIDDEN_OBJECTS:
                found.append(f"{where}: .{node.attr}")
            if node.attr == "goto" and id(node) not in allowed_goto:
                found.append(f"{where}: .goto outside {GOTO_ONLY_IN}() on a page from self._new_page()")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(part in node.name.lower() for part in FORBIDDEN_NAME_PARTS):
                found.append(f"{where}: name {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if isinstance(func, ast.Name) and func.id in ("getattr", "setattr", "hasattr"):
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and \
                        str(node.args[1].value) in FORBIDDEN_METHODS | FORBIDDEN_OBJECTS | DYNAMIC_IMPORTERS | {"goto"}:
                    found.append(f"{where}: getattr {node.args[1].value!r}")
            if isinstance(func, ast.Name) and func.id in ("exec", "eval"):
                found.append(f"{where}: {func.id}()")
            if name in importers:
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    found.append(f"{where}: dynamic import of a computed name")
                elif _forbidden_module(first.value):
                    found.append(f"{where}: dynamic import {first.value}")
            if name in LAUNCHERS:
                keywords = {k.arg for k in node.keywords}
                if None in keywords:
                    found.append(f"{where}: **kwargs in {name}()")
                for bad in sorted(k for k in keywords if k in LAUNCH_FORBIDDEN_KEYWORDS):
                    found.append(f"{where}: {bad}= in {name}()")
                if "headless" not in keywords:
                    found.append(f"{where}: {name}() must pass headless=False (Playwright defaults to headless)")
                if node.args:
                    found.append(f"{where}: positional arguments in {name}()")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_module(alias.name):
                    found.append(f"{where}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for candidate in [module] + [f"{module}.{a.name}" if module else a.name for a in node.names]:
                if candidate and _forbidden_module(candidate):
                    found.append(f"{where}: import {candidate}")
        if isinstance(node, ast.keyword) and node.arg == "headless":
            if not (isinstance(node.value, ast.Constant) and node.value.value is False):
                found.append(f"{where}: headless must be the literal False")
    return found


def package_sources(root: Path = SRC_DIR) -> list[Path]:
    """Every .py file under `root`, subpackages included."""
    return sorted(root.rglob("*.py"))


# --- the scanner catches what it must --------------------------------------------------------------
@pytest.mark.parametrize("snippet", [
    "page.click('text=Enviar postulación')",
    "page.get_by_role('button').click()",
    "locator.dblclick()",
    "page.keyboard.press('Enter')",
    "el.press('Enter')",
    "el.type('secret')",
    "page.mouse.click(10, 10)",
    "box.check()",
    "box.set_checked(True)",
    "sel.select_option('1')",
    "inp.set_input_files('cv.pdf')",
    "form.submit()",
    "el.dispatch_event('click')",
    "page.evaluate('document.forms[0].submit()')",
    "el.tap()",
    "f = page.click",
    "getattr(page, 'click')()",
    "def submit_application(): pass",
    "def enviar(): pass",
    "class AutoPostular: pass",
    "def send_form(): pass",
    "def apply_now(): pass",
    "import luk_cli",
    "from luk_scraper.export import write",
    "import requests",
    "chromium.launch(headless=True)",
    "chromium.launch(headless=flag)",
    # page scripts, focus, drag and drop
    "page.locator('form').evaluate_all('fs => fs[0].requestSubmit()')",
    "page.wait_for_function('document.forms[0].submit() || true')",
    "el.focus()",
    "page.drag_and_drop('#a', '#b')",
    # raw HTTP with the logged-in cookies (APIRequestContext)
    "ctx.request.post('https://www.takealuk.com/job_offers/x/applications', data={})",
    "page.request.fetch('https://www.takealuk.com/')",
    "api = ctx.request",
    # rewriting requests or the page; leaving a tab you are reviewing
    "page.route('**/*', handler)",
    "ctx.unroute('**/*')",
    "ctx.route_from_har('x.har')",
    "page.set_content('<form></form>')",
    "page.reload()",
    "page.go_back()",
    "page.go_forward()",
    # .goto anywhere but on a new tab in _goto_new_tab
    "page.goto(url)",
    "def fill(self, url):\n    self._page.goto(url)",
    "def open(self, url):\n    page = self._new_page()\n    page.goto(url)",
    "def _goto_new_tab(self, url):\n    self._page.goto(url)",
    "def _goto_new_tab(self, url):\n    page = self._page\n    page.goto(url)",
    "def _goto_new_tab(self, url):\n    page = self._new_page()\n    page = self._page\n    page.goto(url)",
    "def _goto_new_tab(self, url):\n    self._ctx.new_page().goto(url)",
    "g = page.goto",
    "getattr(page, 'goto')(url)",
    # launching: extra Chromium args, **kwargs, or relying on the headless default
    "chromium.launch(headless=False, args=['--headless=new'])",
    "chromium.launch(**{'headless': True})",
    "chromium.launch(headless=False, **opts)",
    "chromium.launch_persistent_context(user_data_dir='p', headless=False, ignore_default_args=True)",
    "chromium.launch_persistent_context(user_data_dir='p', headless=False, args=['--x'])",
    "chromium.launch()",
    "chromium.launch_persistent_context(user_data_dir='p')",
    "chromium.connect_over_cdp('http://localhost:9222')",
    # network modules, however imported
    "from urllib import request",
    "from http import client",
    "import urllib.request",
    "urllib.request.urlopen('https://example.com')",
    "import urllib3",
    "import importlib\nimportlib.import_module('requests')",
    "import importlib\nimportlib.import_module('luk_cli.client')",
    "__import__('socket')",
    "__import__('luk_scraper')",
    "from importlib import import_module\nimport_module('httpx')",
    "from importlib import import_module as load\nload('requests')",
    "import importlib\nimportlib.import_module(name)",
    "getattr(importlib, 'import_module')('requests')",
    "exec('import requests')",
    "eval('__import__(\"socket\")')",
])
def test_scanner_catches(snippet):
    assert violations(snippet), snippet


@pytest.mark.parametrize("snippet", [
    "el.fill('Comuna Demo')",
    "def _goto_new_tab(self, url):\n    page = self._new_page()\n    page.goto(url, wait_until='domcontentloaded')\n"
    "    return page",
    "page.get_by_role('button', name='Enviar postulación')",
    "chromium.launch(headless=False)",
    "chromium.launch_persistent_context(user_data_dir='p', headless=False, viewport={'width': 1})",
    "def apply_url(slug): pass",
    "from urllib.parse import unquote",
    "import importlib\nimportlib.import_module('pypdf')",
    "page.wait_for_timeout(500)",
    "self.storage_state = None",
])
def test_scanner_allows(snippet):
    assert violations(snippet) == []


def test_package_sources_include_subpackages(tmp_path):
    (tmp_path / "sub" / "deeper").mkdir(parents=True)
    for rel in ("a.py", "sub/b.py", "sub/deeper/c.py"):
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")
    assert [p.relative_to(tmp_path).as_posix() for p in package_sources(tmp_path)] == [
        "a.py", "sub/b.py", "sub/deeper/c.py"]


# --- the package passes ------------------------------------------------------------------------
def test_package_has_no_way_to_submit_click_press_or_tick():
    files = package_sources()
    assert len(files) >= 8, f"expected the whole package under {SRC_DIR}, found {len(files)} files"
    problems = [v for f in files for v in violations(f.read_text(encoding="utf-8"), f.name)]
    assert problems == []


def test_the_package_navigates_only_through_goto_new_tab():
    source = (SRC_DIR / "browser.py").read_text(encoding="utf-8")
    assert source.count(".goto(") == 1 and f"def {GOTO_ONLY_IN}(" in source


def test_formbrowser_protocol_has_only_open_detect_fill():
    members = {name for name, _ in inspect.getmembers(B.FormBrowser, inspect.isfunction) if not name.startswith("_")}
    assert members == {"open", "detect_fields", "fill"}


def test_playwright_browser_cannot_be_headless():
    params = inspect.signature(B.PlaywrightBrowser.__init__).parameters
    assert "headless" not in params
    assert not any("headless" in name for name in params)


def test_no_public_method_of_the_driver_suggests_sending():
    for name in dir(B.PlaywrightBrowser):
        assert not any(part in name.lower() for part in FORBIDDEN_NAME_PARTS), name
