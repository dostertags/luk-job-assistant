"""prefill (pure, via a fake FormBrowser) and PlaywrightBrowser (via a fake playwright module, offline)."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest
from conftest import EXPERIENCE, REPO_ROOT, RESIDENCE, SALARY, FakeBrowser, luk_fields

from luk_assist import browser as B
from luk_assist.answers import Answers, make_answer_fn
from luk_assist.browser import Field, PlaywrightBrowser, clip, prefill


# ============================== prefill ==============================
def test_prefill_fills_the_three_luk_questions(paz_answers):
    fake = FakeBrowser()
    result = prefill(fake, make_answer_fn(paz_answers))
    assert fake.filled == [
        ("residence", "Calle Falsa 123, Comuna Demo, Santiago"),
        ("salary", "1500000"),
        ("experience", "6 años en análisis comercial en Empresa Demo 01 SpA."),
    ]
    assert [f.label for f in result.filled] == [RESIDENCE, SALARY, EXPERIENCE]
    assert result.left_blank == [] and result.detected == 3


def test_prefill_with_no_answers_types_nothing():
    fake = FakeBrowser()
    result = prefill(fake, make_answer_fn(Answers()))
    assert fake.filled == []
    assert [f.label for f in result.left_blank] == [RESIDENCE, SALARY, EXPERIENCE]


def test_prefill_never_types_into_credential_or_unsupported_fields():
    fields = [
        Field(name="user[password]", label="Ingresa", kind="text"),
        Field(name="q", label="Contraseña", kind="text"),
        Field(name="consent", label="Acepto los términos", kind="checkbox"),
        Field(name="cv", label="Adjunta tu CV", kind="file"),
    ]
    fake = FakeBrowser(fields)
    result = prefill(fake, lambda _f: "anything")
    assert fake.filled == [] and result.filled == [] and result.left_blank == []


def test_prefill_number_fields_take_digits_only():
    fields = [Field(name="n1", label="Renta", kind="number"), Field(name="n2", label="Edad", kind="number")]
    fake = FakeBrowser(fields)
    result = prefill(fake, lambda f: "1500000" if f.name == "n1" else "treinta")
    assert fake.filled == [("n1", "1500000")]
    assert [f.name for f in result.left_blank] == ["n2"]


def test_prefill_truncates_long_values():
    fields = [Field(name="t", label="Carta", kind="text", max_length=12),
              Field(name="a", label="Experiencia", kind="textarea")]
    fake = FakeBrowser(fields)
    prefill(fake, lambda f: "uno dos tres cuatro" if f.name == "t" else "x" * 5000)
    assert fake.filled[0] == ("t", "uno dos tres")
    assert len(fake.filled[1][1]) == B.DEFAULT_MAX_LENGTH["textarea"]


def test_a_failed_fill_is_reported_as_left_blank():
    fake = FakeBrowser(fail_on={"salary"})
    result = prefill(fake, make_answer_fn(Answers(profile={"comuna": "Comuna Demo"}, fixed={"salary": "1"})))
    assert [f.name for f in result.filled] == ["residence"]
    assert [f.name for f in result.left_blank] == ["salary", "experience"]


@pytest.mark.parametrize("text,limit,expected", [
    ("abc def", 10, "abc def"),
    ("abc def", 4, "abc"),
    ("ab cdef", 4, "ab"),
    ("abcdef", 3, "abc"),
    ("abc", 0, ""),
])
def test_clip(text, limit, expected):
    assert clip(text, limit) == expected


def test_supported_kinds_are_text_only():
    assert set(B.FILLABLE_KINDS) == {"text", "number", "textarea"}


# ======================= PlaywrightBrowser, faked =======================
class El:
    """A fake DOM element with the locator methods the driver uses."""

    def __init__(self, tag, *, name=None, type=None, id=None, visible=True, editable=True, value="",
                 aria_label=None, placeholder=None, maxlength=None, in_form=True, in_header=False,
                 autocomplete=None, inputmode=None):
        self.tag = tag
        self.attrs = {"name": name, "type": type, "id": id, "aria-label": aria_label,
                      "placeholder": placeholder, "maxlength": maxlength, "autocomplete": autocomplete,
                      "inputmode": inputmode}
        self.visible, self.editable, self.value = visible, editable, value
        self.in_form, self.in_header = in_form, in_header
        self.fills: list[str] = []

    def element_handle(self, timeout=None):  # a pinned handle to this very node
        return self

    def get_attribute(self, attr):
        return self.attrs.get(attr)

    def is_visible(self):
        return self.visible

    def is_editable(self):
        return self.editable

    def input_value(self):
        return self.value

    def fill(self, value):
        self.fills.append(value)
        self.value = value


class Label:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class Loc:
    def __init__(self, items, page=None):
        self.items, self.page = list(items), page

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]

    @property
    def first(self):
        return Loc(self.items[:1], self.page)

    def or_(self, other):
        return Loc(self.items + other.items, self.page)

    def wait_for(self, state=None, timeout=None):
        assert state == "visible"
        if not self.items:
            raise TimeoutError("form never appeared")

    def inner_text(self):
        return self.items[0].inner_text()

    def locator(self, tag):  # used on the form root
        return Loc([e for e in self.page.elements if e.tag == tag and e.in_form], self.page)


class Page:
    def __init__(self, ctx):
        self.ctx, self.url = ctx, "about:blank"
        self.elements: list[El] = []
        self.labels: dict[str, str] = {}
        self.form_loaded = False
        self.form_container = True  # False: the button is visible but no <form> / frame holds it
        self.closed = False
        self.gotos: list[tuple[str, str]] = []

    def is_closed(self):
        return self.closed

    def goto(self, url, wait_until=None):
        self.url = url
        self.gotos.append((url, wait_until))
        if self.ctx.site is not None:
            self.ctx.site(self)

    def get_by_role(self, role, name=None):
        assert role == "button" and name == B.SUBMIT_BUTTON_TEXT
        return Loc(["the-submit-button"] if self.form_loaded else [], self)

    def locator(self, css, has=None):
        if css == "form":
            return Loc([self] if (self.form_loaded and self.form_container and has is not None and has.count())
                       else [], self)
        if css == "turbo-frame textarea":
            return Loc([e for e in self.elements if e.tag == "textarea" and e.in_form], self)
        if css.startswith("turbo-frame"):
            return Loc([], self)
        m = re.fullmatch(r'label\[for="(.*)"\]', css)
        if m:
            return Loc([Label(self.labels[m.group(1)])] if m.group(1) in self.labels else [], self)
        if css in ("textarea", "input"):
            return Loc([e for e in self.elements if e.tag == css], self)
        raise AssertionError(f"unexpected selector: {css}")

    def inner_text(self):  # never used on a page
        raise AssertionError

    def wait_for_timeout(self, ms):
        if self.ctx.on_wait is not None:
            self.ctx.on_wait(self, ms)
            return
        self.ctx.pages.clear()  # the user closes the window


class Ctx:
    def __init__(self, blank_tab, site=None, on_wait=None):
        self.pages: list[Page] = []
        if blank_tab:
            self.pages.append(Page(self))
        self.site = site
        self.on_wait = on_wait
        self.timeout = None
        self.closed = False

    def new_page(self):
        page = Page(self)
        self.pages.append(page)
        return page

    def set_default_timeout(self, ms):
        self.timeout = ms

    def close(self):
        self.closed = True


class Chromium:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.ctx: Ctx | None = None
        self.site = None  # passed to the next context (for runs where the test cannot reach it in time)
        self.on_wait = None

    def launch_persistent_context(self, **kwargs):
        self.calls.append(("launch_persistent_context", kwargs))
        self.ctx = Ctx(blank_tab=True, site=self.site, on_wait=self.on_wait)
        return self.ctx

    def launch(self, **kwargs):
        self.calls.append(("launch", kwargs))
        chromium = self

        class App:
            def new_context(self, **kw):
                chromium.calls.append(("new_context", kw))
                chromium.ctx = Ctx(blank_tab=False)
                return chromium.ctx

            def close(self):
                chromium.calls.append(("browser.close", {}))

        return App()


@pytest.fixture
def fake_playwright(monkeypatch):
    chromium = Chromium()
    pw = types.SimpleNamespace(chromium=chromium, stopped=False)
    pw.stop = lambda: setattr(pw, "stopped", True)
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: pw)
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return pw


def luk_form(page: Page) -> None:
    """The Luk apply tab once the Turbo Frame has loaded (fictional ids, real question labels)."""
    page.form_loaded = True
    page.labels = {"q_res": RESIDENCE, "q_sal": SALARY, "q_exp": EXPERIENCE}
    page.elements = [
        El("input", name="search", type="text", in_form=False, in_header=True, placeholder="Busca por comuna"),
        El("input", name="answers[residence]", type="text", id="q_res"),
        El("input", name="answers[salary]", type="text", id="q_sal", maxlength="9"),
        El("textarea", name="answers[experience]", id="q_exp"),
        El("input", name="already", type="text", value="typed by you", aria_label="Nombre"),
        El("input", name="hidden_q", type="text", visible=False, aria_label="Oculta"),
        El("input", name="readonly_q", type="text", editable=False, aria_label="Solo lectura"),
        El("input", name="user[password]", type="password", aria_label="Contraseña"),
        El("input", name="consent", type="checkbox", aria_label="Acepto"),
        El("input", name="cv_choice", type="radio", aria_label="CV"),
        El("input", name="attachment", type="file", aria_label="Adjunto"),
        El("input", name="commit", type="submit", aria_label=B.SUBMIT_BUTTON_TEXT),
        El("input", type="text", aria_label="Sin nombre"),
        El("input", name="age", type="number", placeholder="Edad"),
    ]


def test_persistent_profile_is_always_headed(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "profile").start()
    name, kwargs = fake_playwright.chromium.calls[0]
    assert name == "launch_persistent_context"
    assert kwargs["headless"] is False
    assert kwargs["user_data_dir"] == str(tmp_path / "profile")
    assert (tmp_path / "profile").is_dir()
    br.close()
    assert fake_playwright.chromium.ctx.closed and fake_playwright.stopped


def test_storage_state_uses_a_fresh_headed_context(fake_playwright, tmp_path):
    state = tmp_path / "session.json"
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    PlaywrightBrowser(storage_state=state).start()
    calls = fake_playwright.chromium.calls
    assert calls[0] == ("launch", {"headless": False})
    assert calls[1][0] == "new_context" and calls[1][1]["storage_state"] == str(state)


def test_profile_and_storage_state_are_exclusive(tmp_path):
    with pytest.raises(ValueError):
        PlaywrightBrowser(profile_dir=tmp_path, storage_state=tmp_path / "s.json")


def test_missing_playwright_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(B.BrowserUnavailable, match="luk-assist\\[browser\\]"):
        PlaywrightBrowser(profile_dir=Path("unused")).start()


def test_install_hint_is_repo_based_never_a_bare_package_name():
    """luk-assist is not on PyPI: a bare `pip install "luk-assist[...]"` could fetch someone else's package."""
    hint = B.INSTALL_HINT
    assert 'python -m pip install -U "pip>=21.3"' in hint
    assert 'python -m pip install -e "<path to luk-job-assistant>/luk-assist[browser]"' in hint
    assert "python -m playwright install chromium" in hint
    assert not re.search(r"pip install\s+(?!-e\b|-U\b)[\"']?luk-assist", hint)


def test_default_profile_dir_is_outside_the_repository():
    path = B.default_profile_dir().resolve()
    assert path.name == "browser-profile" and "luk-assist" in path.parts
    assert REPO_ROOT not in path.parents


def test_open_uses_a_new_tab_each_time_and_waits_for_the_form(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    ctx = fake_playwright.chromium.ctx
    ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    br.open("https://www.takealuk.com/job_offers/demo-2?tab=apply")
    assert len(ctx.pages) == 2  # the first open reused the blank tab; the second got a new one
    assert [p.gotos[0][0] for p in ctx.pages] == ["https://www.takealuk.com/job_offers/demo-1?tab=apply",
                                                  "https://www.takealuk.com/job_offers/demo-2?tab=apply"]
    assert br.form_ready is True


def test_open_survives_a_form_that_never_loads(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = lambda page: None  # e.g. not logged in
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    assert br.form_ready is False
    assert br.detect_fields() == []


def test_detect_fields_only_returns_empty_visible_named_text_fields(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    fields = br.detect_fields()
    assert [(f.name, f.label, f.kind, f.max_length) for f in fields] == [
        ("answers[experience]", EXPERIENCE, "textarea", None),
        ("answers[residence]", RESIDENCE, "text", None),
        ("answers[salary]", SALARY, "text", 9),
        ("age", "Edad", "number", None),
    ]


def test_no_form_means_no_fields_never_the_whole_page(fake_playwright, tmp_path):
    """Replaces the old page-wide fallback: without the application form nothing on the page is filled."""
    def no_form_element(page):
        luk_form(page)
        page.form_loaded = False  # no "Enviar postulación" button, no application frame
        for element in page.elements:
            element.in_form = False

    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = no_form_element
    assert br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply") is False
    assert br.detect_fields() == []


def test_a_visible_button_outside_any_form_or_frame_is_not_a_form(fake_playwright, tmp_path):
    def button_without_form(page):
        luk_form(page)
        page.form_container = False  # the button is there, but no <form> or application frame holds it

    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = button_without_form
    assert br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply") is True
    assert br.detect_fields() == []  # no page-wide fallback, even with form_ready


def test_detect_fields_is_empty_unless_the_form_is_ready(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = luk_form
    br.show("https://www.takealuk.com/job_offers/demo-1?tab=apply")  # a page, but not waited for as a form
    assert br.form_ready is False and br.detect_fields() == []


def test_open_returns_whether_the_form_was_found(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = luk_form
    assert br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply") is True


# --- fill re-verifies the field it detected ---------------------------------------------------
def _detected(fake_playwright, tmp_path, label=RESIDENCE):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    fld = next(f for f in br.detect_fields() if f.label == label)
    page = fake_playwright.chromium.ctx.pages[0]
    element = next(e for e in page.elements if e.attrs["name"] == fld.name)
    return br, fld, page, element


@pytest.mark.parametrize("change", [
    lambda page, el: el.attrs.update(name="answers[other_question]"),         # another element now
    lambda page, el: page.labels.update(q_res="¿Tienes licencia de conducir?"),  # another question
    lambda page, el: el.attrs.update(type="password"),
    lambda page, el: el.attrs.update(autocomplete="one-time-code"),
    lambda page, el: page.labels.update(q_res="Código de verificación"),
    lambda page, el: setattr(el, "visible", False),
])
def test_fill_skips_a_field_whose_identity_changed_since_detection(fake_playwright, tmp_path, change):
    br, fld, page, element = _detected(fake_playwright, tmp_path)
    change(page, element)
    with pytest.raises(B.FillSkipped):
        br.fill(fld, "Comuna Demo")
    assert element.fills == []


def test_fill_still_types_into_an_unchanged_field(fake_playwright, tmp_path):
    br, fld, _page, element = _detected(fake_playwright, tmp_path)
    br.fill(fld, "Comuna Demo")
    assert element.fills == ["Comuna Demo"]


# --- one-time codes, card numbers and new passwords are never detected ---------------------------
@pytest.mark.parametrize("attrs", [
    {"autocomplete": "one-time-code"},
    {"autocomplete": "current-password"},
    {"autocomplete": "new-password"},
    {"autocomplete": "cc-number"},
    {"autocomplete": "section-pago cc-csc"},
    {"inputmode": "numeric", "maxlength": "6", "placeholder": "Código"},
    {"inputmode": "numeric", "maxlength": "8", "placeholder": "Verification code"},
    {"placeholder": "Código de verificación"},
    {"placeholder": "Ingresa el código de 6 dígitos"},
    {"placeholder": "Token"},
    {"placeholder": "Security code"},
])
def test_secret_inputs_are_never_detected(fake_playwright, tmp_path, attrs):
    def form_with_secret(page):
        luk_form(page)
        page.elements.append(El("input", name="answers[extra]", type="text", **attrs))

    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = form_with_secret
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    assert "answers[extra]" not in [f.name for f in br.detect_fields()]


def test_a_short_numeric_input_without_a_code_label_is_still_a_field(fake_playwright, tmp_path):
    def form_with_age(page):
        luk_form(page)
        page.elements.append(El("input", name="answers[years]", type="text", inputmode="numeric", maxlength="2",
                                placeholder="Años de experiencia"))

    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = form_with_age
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    assert "answers[years]" in [f.name for f in br.detect_fields()]


def test_prefill_through_the_real_driver(fake_playwright, tmp_path, paz_answers):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    ctx = fake_playwright.chromium.ctx
    ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    result = prefill(br, make_answer_fn(paz_answers))
    page = ctx.pages[0]
    by_name = {e.attrs["name"]: e for e in page.elements}
    assert by_name["answers[residence]"].fills == ["Calle Falsa 123, Comuna Demo, Santiago"]
    assert by_name["answers[salary]"].fills == ["1500000"]
    assert by_name["answers[experience]"].fills == ["6 años en análisis comercial en Empresa Demo 01 SpA."]
    for untouched in ("search", "already", "user[password]", "consent", "cv_choice", "attachment", "commit"):
        assert by_name[untouched].fills == []
    assert [f.name for f in result.left_blank] == ["age"]


def test_fill_never_overwrites_what_you_typed_meanwhile(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    fake_playwright.chromium.ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    residence = next(f for f in br.detect_fields() if f.label == RESIDENCE)
    element = next(e for e in fake_playwright.chromium.ctx.pages[0].elements if e.attrs["name"] == residence.name)
    element.value = "Escrito por ti"
    with pytest.raises(B.FillSkipped):
        br.fill(residence, "Comuna Demo")
    assert element.fills == []


def test_fill_refuses_unknown_fields(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    with pytest.raises(B.FillSkipped):
        br.fill(Field(name="x", label="x"), "value")


def test_wait_until_closed_returns_when_the_window_closes(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    br.wait_until_closed(poll_ms=1)
    assert fake_playwright.chromium.ctx.pages == []


class TargetClosedError(Exception):
    """What Playwright raises when you poll a tab the user has just closed."""


def _two_offer_tabs(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    ctx = fake_playwright.chromium.ctx
    ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    br.open("https://www.takealuk.com/job_offers/demo-2?tab=apply")
    return br, ctx


def _close_first_tab_then_the_rest_after(ctx, polls_before_last_close):
    """The user closes the FIRST tab; the other one (a form under review) stays open for a while."""
    first = ctx.pages[0]
    context_closed_while_a_tab_was_open: list[bool] = []

    def on_wait(page, _ms):
        if page is first:
            first.closed = True  # Playwright drops it from ctx.pages a moment later
            raise TargetClosedError("Target page, context or browser has been closed")
        if first in ctx.pages:
            ctx.pages.remove(first)
        context_closed_while_a_tab_was_open.append(ctx.closed)
        if len(context_closed_while_a_tab_was_open) == polls_before_last_close:
            page.closed = True
            ctx.pages.clear()  # the user closes the last tab

    ctx.on_wait = on_wait
    return context_closed_while_a_tab_was_open


def test_closing_the_first_tab_does_not_end_the_wait(fake_playwright, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_sleep", lambda _s: None)
    br, ctx = _two_offer_tabs(fake_playwright, tmp_path)
    polls = _close_first_tab_then_the_rest_after(ctx, polls_before_last_close=3)
    br.wait_until_closed(poll_ms=1)
    assert polls == [False, False, False]  # kept waiting on the tab that was still open
    assert ctx.pages == [] and ctx.closed is False  # waiting never closes anything itself


def test_a_tab_that_fails_to_poll_but_stays_open_keeps_the_wait_going(fake_playwright, tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(B, "_sleep", slept.append)
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    ctx = fake_playwright.chromium.ctx
    polls = [0]

    def on_wait(page, _ms):
        polls[0] += 1
        if polls[0] <= 2:
            raise TargetClosedError("transient")  # still listed and not closed: try again later
        ctx.pages.clear()

    ctx.on_wait = on_wait
    br.wait_until_closed(poll_ms=250)
    assert polls[0] == 3 and slept == [0.25, 0.25]


def test_wait_returns_when_the_browser_disconnects(fake_playwright, tmp_path):
    state = tmp_path / "session.json"
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    br = PlaywrightBrowser(storage_state=state).start()
    ctx = fake_playwright.chromium.ctx
    ctx.site = luk_form
    br.open("https://www.takealuk.com/job_offers/demo-1?tab=apply")
    br._browser.is_connected = lambda: False  # the Chromium process went away; stale tabs remain listed
    ctx.on_wait = lambda _page, _ms: pytest.fail("must not poll a tab of a disconnected browser")
    br.wait_until_closed(poll_ms=1)


def test_wait_returns_when_the_context_is_gone(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    br.close()
    br.wait_until_closed(poll_ms=1)  # returns at once


def test_cli_closes_the_browser_only_after_the_last_tab(fake_playwright, tmp_path, monkeypatch):
    from luk_assist import cli, runner

    monkeypatch.setattr(runner, "_sleep", lambda _s: None)
    monkeypatch.setattr(B, "_sleep", lambda _s: None)
    listings = tmp_path / "offers.txt"
    listings.write_text("demo-1\ndemo-2\n", encoding="utf-8")
    answers = tmp_path / "answers.json"
    answers.write_text('{"profile": {"comuna": "Comuna Demo"}}', encoding="utf-8")
    chromium = fake_playwright.chromium
    chromium.site = luk_form
    original = chromium.launch_persistent_context
    polls: list[list[bool]] = []

    def launch_persistent_context(**kwargs):
        ctx = original(**kwargs)  # its blank first tab becomes the first offer's tab
        polls.append(_close_first_tab_then_the_rest_after(ctx, polls_before_last_close=2))
        return ctx

    chromium.launch_persistent_context = launch_persistent_context
    code = cli.main(["--listings", str(listings), "--answers", str(answers), "--out", str(tmp_path / "q.json"),
                     "--no-pause"], browser_factory=lambda _s: PlaywrightBrowser(profile_dir=tmp_path / "p"),
                    input_fn=lambda _p: "")
    assert code == 0
    assert polls == [[False, False]]  # the context was still open while the second tab was
    assert chromium.ctx.closed is True and chromium.ctx.pages == []  # closed only after the last tab


def test_show_does_not_wait_for_a_form(fake_playwright, tmp_path):
    br = PlaywrightBrowser(profile_dir=tmp_path / "p").start()
    br.show("https://www.takealuk.com/")
    assert fake_playwright.chromium.ctx.pages[0].gotos == [("https://www.takealuk.com/", "domcontentloaded")]
    assert br.form_ready is False


def test_fake_fields_helper_matches_the_real_questions():
    assert [f.label for f in luk_fields()] == [RESIDENCE, SALARY, EXPERIENCE]
