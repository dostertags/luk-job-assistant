"""The browser layer: a `FormBrowser` contract with NO way to submit, `prefill`, and a Playwright driver.

Red line, made structural: `FormBrowser` has exactly three methods, ``open``, ``detect_fields`` and
``fill``. Nothing in this package submits a form, clicks, presses keys, ticks consent boxes, radios or
selects, uploads files or types a password; tests/test_red_lines.py scans the source to keep it so.
The only navigation is `PlaywrightBrowser._goto_new_tab`, which always uses a new tab.
The user logs in to Luk and clicks "Enviar postulación" themselves.

How Luk's application form works (reverse-engineered; last verified live 2026-09-23):
  - ``https://www.takealuk.com/job_offers/{slug}?tab=apply`` opens an offer on its "Postular" tab.
  - The form is not in the initial HTML: it loads lazily inside a Hotwire Turbo Frame from
    ``/job_offers/{slug}/external_application_form``, and only for a logged-in user (otherwise Luk
    asks you to log in). `PlaywrightBrowser.open` therefore waits for the "Enviar postulación" button
    or a textarea inside a ``<turbo-frame>`` before looking for fields.
  - Free-text questions seen on every offer: "Indica tu lugar de residencia (calle, comuna, ciudad)"
    (one-line input), "¿Cuál es tu expectativa de renta mensual líquida?" (one-line input) and
    "Comenta tu experiencia relacionada al cargo" (textarea). Employers may add their own questions.
    Everything else (which CV to send, consent, the submit button) is left to you.
  - Field scoping (not verified against every offer): fields are looked up ONLY inside the ``<form>``
    that contains the "Enviar postulación" button, else inside the application Turbo Frame. If neither
    is there (or the form never loaded) no field is detected: never anything else on the page.

Only visible, named, editable, EMPTY one-line text/number inputs and textareas are ever filled; never a
field you already typed in, never a password, one-time-code or card field, never a checkbox, radio,
select or file input. Right before typing, `fill` checks again that the element is still the same
question (name, label, kind) and still empty and safe; otherwise it skips it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from platformdirs import user_data_dir

from luk_assist.answers import APP_NAME, is_credential, normalize

log = logging.getLogger("luk_assist.browser")

SUBMIT_BUTTON_TEXT = "Enviar postulación"  # located to know the form has loaded; never clicked
FORM_FRAME_SELECTOR = "turbo-frame[src*='application_form']"
FILLABLE_KINDS = ("text", "number", "textarea")
DEFAULT_MAX_LENGTH = {"text": 255, "number": 20, "textarea": 2000}
VIEWPORT = {"width": 1280, "height": 900}
HANDLE_TIMEOUT_MS = 2_000

# Inputs that hold secrets, whatever their label says (HTML autocomplete tokens; "cc-*" = card data).
SECRET_AUTOCOMPLETE = frozenset({"one-time-code", "current-password", "new-password"})
# A short numeric input with a label like this is a verification code (inputmode=numeric, maxlength<=8).
_CODE_LIKE = re.compile(r"(codigo|code|verific|token)")
CODE_MAX_LENGTH = 8

INSTALL_HINT = (
    "Playwright is not installed. From your luk-job-assistant checkout (luk-assist is not on PyPI), "
    "install the browser extra and Chromium:\n"
    '  python -m pip install -U "pip>=21.3"\n'
    '  python -m pip install -e "<path to luk-job-assistant>/luk-assist[browser]"\n'
    "  python -m playwright install chromium"
)

_sleep: Callable[[float], None] = time.sleep
CHROMIUM_HINT = "Chromium for Playwright is missing. Install it with:\n  python -m playwright install chromium"


class BrowserUnavailable(Exception):
    """Playwright or its Chromium is not installed."""


class FillSkipped(Exception):
    """The driver declined to fill a field (for example, you typed in it meanwhile)."""


def default_profile_dir() -> Path:
    """``<user data dir>/luk-assist/browser-profile``: the persistent browser profile, outside any repo."""
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "browser-profile"


@dataclass(frozen=True)
class Field:
    """A fillable free-text field of the application form."""

    name: str
    label: str
    kind: str = "text"  # 'text' | 'number' | 'textarea'
    max_length: int | None = None
    ref: str = ""  # driver-internal handle key (two fields may share a name)


@dataclass(frozen=True)
class PrefilledField:
    name: str
    label: str
    value: str


@dataclass
class PrefillResult:
    filled: list[PrefilledField] = field(default_factory=list)
    left_blank: list[Field] = field(default_factory=list)

    @property
    def detected(self) -> int:
        return len(self.filled) + len(self.left_blank)


class FormBrowser(Protocol):
    """A visible browser session the user controls.

    DESIGN NOTE: there is deliberately no submit method. Orchestration can only open a page, list its
    empty text fields and type into them; submitting is the user's click in their own window.

    ``open`` returns True when the application form was found (False: not logged in, closed offer, or
    it applies on another site; then ``detect_fields`` returns nothing).
    """

    def open(self, url: str) -> bool: ...

    def detect_fields(self) -> list[Field]: ...

    def fill(self, field: Field, value: str) -> None: ...


AnswerFn = Callable[[Field], str]


def clip(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters, at a word boundary when there is one."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    cut = text[:limit]
    if text[limit].isspace() or cut[-1].isspace():
        return cut.rstrip()
    parts = cut.rsplit(None, 1)
    return parts[0].rstrip() if len(parts) == 2 else cut


def prefill(browser: FormBrowser, answer_fn: AnswerFn) -> PrefillResult:
    """Type your answers into the detected empty text fields. Never submits.

    A field stays blank (listed in ``left_blank``) when ``answer_fn`` returns '' for it, when a number
    field would get anything but digits, or when the driver declines to fill it. Password-like fields
    are skipped entirely.
    """
    result = PrefillResult()
    for fld in browser.detect_fields():
        if fld.kind not in FILLABLE_KINDS or is_credential(fld.name) or is_credential(fld.label):
            continue
        value = (answer_fn(fld) or "").strip()
        limit = fld.max_length or DEFAULT_MAX_LENGTH[fld.kind]
        if fld.kind == "number":
            if not (value.isascii() and value.isdigit()) or len(value) > limit:
                value = ""
        else:
            value = clip(value, limit)
        if not value:
            result.left_blank.append(fld)
            continue
        try:
            browser.fill(fld, value)
        except Exception as exc:  # noqa: BLE001 - one field must not stop the others
            log.warning("left %r blank: %s", fld.label, exc or type(exc).__name__)
            result.left_blank.append(fld)
            continue
        result.filled.append(PrefilledField(name=fld.name, label=fld.label, value=value))
    return result


def _css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _to_int(value: str | None) -> int | None:
    try:
        n = int((value or "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


class PlaywrightBrowser:
    """The real `FormBrowser`: Chromium in a VISIBLE window (there is no headless option).

    Session, one of:
      - a persistent profile (default ``default_profile_dir()``): you log in to Luk yourself in that
        window once and the cookies stay in the profile;
      - ``storage_state=PATH``: a Playwright storage-state JSON (for example the session file that
        ``luk login`` from luk-cli saves) loaded into a fresh, non-persistent context.

    Each ``open`` uses a new tab, so a form you are reviewing or submitting is never navigated away.
    """

    def __init__(
        self,
        *,
        profile_dir: Path | str | None = None,
        storage_state: Path | str | None = None,
        timeout_ms: int = 45_000,
        form_timeout_ms: int = 20_000,
    ) -> None:
        if profile_dir is not None and storage_state is not None:
            raise ValueError("use either a persistent profile or a storage state, not both")
        self.profile_dir = Path(profile_dir) if profile_dir is not None else default_profile_dir()
        self.storage_state = Path(storage_state) if storage_state is not None else None
        self.timeout_ms = timeout_ms
        self.form_timeout_ms = form_timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._blank: Any = None
        self._page: Any = None
        self._handles: dict[str, Any] = {}
        self.form_ready = False

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> PlaywrightBrowser:
        try:
            from playwright.sync_api import sync_playwright  # lazy: optional dependency
        except ImportError as exc:
            raise BrowserUnavailable(INSTALL_HINT) from exc
        self._pw = sync_playwright().start()
        try:
            chromium = self._pw.chromium
            if self.storage_state is not None:
                self._browser = chromium.launch(headless=False)
                self._ctx = self._browser.new_context(storage_state=str(self.storage_state), viewport=VIEWPORT)
            else:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._ctx = chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir), headless=False, viewport=VIEWPORT
                )
            self._ctx.set_default_timeout(self.timeout_ms)
            self._blank = self._ctx.pages[0] if self._ctx.pages else None
        except Exception as exc:
            self.close()
            if "executable doesn't exist" in str(exc).lower():
                raise BrowserUnavailable(CHROMIUM_HINT) from exc
            raise
        return self

    def close(self) -> None:
        for closer in (
            lambda: self._ctx.close() if self._ctx is not None else None,
            lambda: self._browser.close() if self._browser is not None else None,
            lambda: self._pw.stop() if self._pw is not None else None,
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - the user may already have closed the window
                pass
        self._ctx = self._browser = self._pw = self._page = self._blank = None

    def __enter__(self) -> PlaywrightBrowser:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open_pages(self) -> list[Any]:
        """The tabs still open ([] once the context or the browser is gone)."""
        try:
            is_connected = getattr(self._browser, "is_connected", None)  # only for --storage-state
            if callable(is_connected) and not is_connected():
                return []
            pages = list(self._ctx.pages) if self._ctx is not None else []
        except Exception:  # noqa: BLE001 - the context is gone
            return []
        alive: list[Any] = []
        for page in pages:
            try:
                if not page.is_closed():
                    alive.append(page)
            except Exception:  # noqa: BLE001 - a tab that cannot even say so is gone
                continue
        return alive

    def wait_until_closed(self, poll_ms: int = 500) -> None:
        """Block until you have closed EVERY tab (so you can review and submit in peace).

        Closing one tab, even the first, does not end the wait: the pre-filled forms in the other
        tabs stay open until you close them too. Returns once no tab is left or the browser is gone.
        """
        while True:
            pages = self._open_pages()
            if not pages:
                return
            for page in pages:
                try:
                    page.wait_for_timeout(poll_ms)
                    break
                except Exception:  # noqa: BLE001 - that tab was just closed; poll another one
                    continue
            else:  # no tab could be polled right now: wait, then look again
                _sleep(poll_ms / 1000)

    # -- navigation --------------------------------------------------------------------------
    def _new_page(self) -> Any:
        if self._ctx is None:
            raise RuntimeError("call start() first")
        if self._blank is not None:  # reuse the empty first tab of a persistent profile
            page, self._blank = self._blank, None
            return page
        return self._ctx.new_page()

    def _goto_new_tab(self, url: str) -> Any:
        """The ONLY navigation in luk-assist: always a new tab, never the tab of a form you are reviewing."""
        page = self._new_page()
        page.goto(url, wait_until="domcontentloaded")
        return page

    def show(self, url: str) -> None:
        """Open `url` in a new tab without waiting for a form (e.g. Luk's home page to log in)."""
        self._page, self._handles, self.form_ready = self._goto_new_tab(url), {}, False

    def open(self, url: str) -> bool:
        """Open an offer's apply tab in a new tab and wait for the lazily loaded application form.

        Returns whether the form was found (``form_ready``); without it ``detect_fields`` finds nothing.
        """
        self._page, self._handles, self.form_ready = self._goto_new_tab(url), {}, False
        self.form_ready = self._wait_for_form()
        return self.form_ready

    def _wait_for_form(self) -> bool:
        page = self._page
        ready = page.get_by_role("button", name=SUBMIT_BUTTON_TEXT).or_(page.locator("turbo-frame textarea"))
        try:
            ready.first.wait_for(state="visible", timeout=self.form_timeout_ms)
            return True
        except Exception as exc:  # noqa: BLE001 - not logged in, closed offer, or an external application
            log.warning("no application form appeared (%s): not logged in, closed offer, or it applies "
                        "on another site", type(exc).__name__)
            return False

    # -- fields ------------------------------------------------------------------------------
    def _root(self) -> Any | None:
        """The application form: the <form> holding the submit button, else the application frame.

        None when neither is on the page: then nothing is detected (never a page-wide search).
        """
        page = self._page
        button = page.get_by_role("button", name=SUBMIT_BUTTON_TEXT)
        for candidate in (
            page.locator("form", has=button),
            page.locator(FORM_FRAME_SELECTOR),
            page.locator("turbo-frame", has=button),
        ):
            try:
                if candidate.count() > 0:
                    return candidate.first
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _kind(element: Any, tag: str) -> str | None:
        if tag == "textarea":
            return "textarea"
        typ = (element.get_attribute("type") or "text").strip().lower()
        return typ if typ in ("text", "number") else None

    @staticmethod
    def _usable(element: Any) -> bool:
        try:
            return bool(element.is_visible() and element.is_editable() and not (element.input_value() or "").strip())
        except Exception:  # noqa: BLE001
            return False

    def _label(self, element: Any, name: str) -> str:
        el_id = element.get_attribute("id")
        if el_id:
            labels = self._page.locator(f"label[for={_css_string(el_id)}]")
            if labels.count() > 0:
                text = " ".join((labels.first.inner_text() or "").split())
                if text:
                    return text
        for attr in ("aria-label", "placeholder", "title"):
            value = element.get_attribute(attr)
            if value and value.strip():
                return " ".join(value.split())
        return name

    @staticmethod
    def _is_secret(element: Any, name: str, label: str) -> bool:
        """Passwords, PINs, one-time / verification codes and card data: never detected, never filled."""
        if is_credential(name) or is_credential(label):
            return True
        tokens = (element.get_attribute("autocomplete") or "").strip().lower().split()
        if any(t in SECRET_AUTOCOMPLETE or t.startswith("cc-") for t in tokens):
            return True
        if (element.get_attribute("inputmode") or "").strip().lower() == "numeric":
            limit = _to_int(element.get_attribute("maxlength"))
            if limit is not None and limit <= CODE_MAX_LENGTH and _CODE_LIKE.search(normalize(f"{label} {name}")):
                return True
        return False

    def _describe(self, element: Any, tag: str) -> tuple[str, str, str] | None:
        """(kind, name, label) of a fillable, non-secret field, or None."""
        kind = self._kind(element, tag)
        name = (element.get_attribute("name") or "").strip()
        if kind is None or not name:
            return None
        label = self._label(element, name)
        if self._is_secret(element, name, label):
            return None
        return kind, name, label

    def detect_fields(self) -> list[Field]:
        """Visible, named, editable, EMPTY one-line text/number inputs and textareas of the form.

        Nothing unless the application form was found (``form_ready``) and can be isolated (`_root`).
        Each field keeps a handle pinned to its element, so `fill` types into that very element.
        """
        self._handles = {}
        if self._page is None or not self.form_ready:
            return []
        root = self._root()
        if root is None:
            log.warning("the application form could not be isolated on the page: nothing detected")
            return []
        fields: list[Field] = []
        handles: dict[str, Any] = {}
        for tag in ("textarea", "input"):
            found = root.locator(tag)
            for i in range(found.count()):
                try:
                    element = found.nth(i).element_handle(timeout=HANDLE_TIMEOUT_MS)
                    described = self._describe(element, tag) if element is not None else None
                except Exception:  # noqa: BLE001 - it left the page meanwhile
                    continue
                if described is None or not self._usable(element):
                    continue
                kind, name, label = described
                ref = f"{tag}#{i}"
                handles[ref] = element
                fields.append(Field(name=name, label=label, kind=kind,
                                    max_length=_to_int(element.get_attribute("maxlength")), ref=ref))
        self._handles = handles
        return fields

    def fill(self, field: Field, value: str) -> None:
        """Type `value` into a field found by `detect_fields`, only if it is still that same empty field.

        Right before typing it checks again: same name, label and kind, still not a secret field, still
        visible, editable and empty. Anything else (the page changed, you typed in it) skips the field.
        """
        element = self._handles.get(field.ref)
        if element is None:
            raise FillSkipped("field is no longer on the page")
        tag = field.ref.split("#", 1)[0]
        try:
            now = self._describe(element, tag)
            usable = self._usable(element)
        except Exception as exc:  # noqa: BLE001 - it left the page meanwhile
            raise FillSkipped("field is no longer on the page") from exc
        if now != (field.kind, field.name, field.label):
            raise FillSkipped("the field changed since it was detected")
        if not usable:
            raise FillSkipped("you already typed in it, or it is no longer visible and editable")
        element.fill(value)
