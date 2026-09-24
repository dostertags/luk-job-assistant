"""Open each offer, pre-fill it, and STOP: the review queue.

For every target: ``browser.open(apply URL)`` -> ``prefill`` -> a review record. The record says what
was typed and what was left blank for you; its status tells you to review and click
"Enviar postulación" yourself (READY_FOR_REVIEW), or that no application form was found on the page
(FORM_NOT_FOUND: nothing was typed). Nothing here can submit: it only uses the three `FormBrowser` methods.

Politeness: at least ``MIN_DELAY_S`` seconds between opening two offers (the time you spend reviewing
counts), so a ``--no-pause`` run does not hammer takealuk.com.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from luk_assist.browser import AnswerFn, FormBrowser, PrefillResult, prefill
from luk_assist.targets import Target

log = logging.getLogger("luk_assist.runner")

STATUS = 'READY_FOR_REVIEW — review and click "Enviar postulación" yourself'
STATUS_FORM_NOT_FOUND = ("FORM_NOT_FOUND — no application form on the page (not logged in, closed offer, "
                         "or it applies on another site); nothing was typed")
STATUS_NOT_OPENED = "NOT_OPENED — nothing was filled; open the offer yourself if you want to apply"
MIN_DELAY_S = 2.0

Record = dict[str, Any]
PauseFn = Callable[[Record], None]

_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def _unique_label(label: str, taken: dict[str, str]) -> str:
    key, n = label or "(no label)", 2
    while key in taken:
        key, n = f"{label} ({n})", n + 1
    return key


def assist_listing(browser: FormBrowser, target: Target, answer_fn: AnswerFn,
                   pause: PauseFn | None = None) -> Record:
    """Open ONE offer, pre-fill its free-text questions and stop. Returns its review record."""
    url = target.url
    # False: no application form on the page. None (a FormBrowser written before open() returned a
    # bool) keeps the old meaning: the page was opened and is checked by detect_fields.
    form_found = browser.open(url) is not False
    result = prefill(browser, answer_fn) if form_found else PrefillResult()
    answers: dict[str, str] = {}
    for item in result.filled:
        answers[_unique_label(item.label, answers)] = item.value
    record: Record = {
        "slug": target.slug,
        "title": target.title,
        "company": target.company,
        "url": url,
        "answers": answers,
        "left_blank": [f.label or f.name for f in result.left_blank],
        "fields_detected": result.detected,
        "status": STATUS if form_found else STATUS_FORM_NOT_FOUND,
    }
    if form_found:
        log.info("%s: %d filled, %d left blank (not submitted)", target.slug, len(result.filled),
                 len(result.left_blank))
    else:
        log.info("%s: no application form found; nothing typed", target.slug)
    if pause is not None:
        pause(record)
    return record


def _not_opened(target: Target) -> Record:
    return {"slug": target.slug, "title": target.title, "company": target.company, "url": target.url,
            "answers": {}, "left_blank": [], "fields_detected": 0, "status": STATUS_NOT_OPENED}


def run(targets: Iterable[Target], browser: FormBrowser, answer_fn: AnswerFn, *,
        pause: PauseFn | None = None, delay_s: float = MIN_DELAY_S,
        sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None) -> list[Record]:
    """Pre-fill the offers one by one, pausing after each (when `pause` is given). Returns the queue.

    An offer that cannot be opened (a page that times out, a closed browser window) gets a NOT_OPENED
    record and the run goes on. Ctrl+C (or end of input at a pause) stops the run; the records so far
    are returned.
    """
    sleep = sleep or _sleep
    clock = clock or _clock
    records: list[Record] = []
    last_open: float | None = None
    try:
        for target in targets:
            if last_open is not None:
                wait = delay_s - (clock() - last_open)
                if wait > 0:
                    sleep(wait)
            last_open = clock()
            try:
                record = assist_listing(browser, target, answer_fn)
            except Exception as exc:  # noqa: BLE001 - one broken page must not lose the queue
                log.error("%s: could not open or pre-fill it (%s); skipped", target.slug, type(exc).__name__)
                records.append(_not_opened(target))
                continue
            records.append(record)
            if pause is not None:
                pause(record)
    except (KeyboardInterrupt, EOFError):
        log.warning("stopped by the user after %d offer(s)", len(records))
    return records


def save_queue(path: Path | str, records: list[Record]) -> Path:
    """Write the review queue as JSON (it holds your answers: keep it out of any repository)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(p, 0o600)
    return p


def console_pause(record: Record, *, input_fn: Callable[[str], str] = input,
                  out: Callable[[str], None] = print) -> None:
    """Show what was pre-filled and wait for Enter. You review and submit in the browser meanwhile."""
    heading = record.get("title") or record["slug"]
    company = record.get("company") or ""
    out("")
    out(f"=== {heading}{' - ' + company if company else ''} ===")
    out(f"    {record['url']}")
    if record.get("status") == STATUS_FORM_NOT_FOUND:
        out("    No application form found: not logged in, offer closed, or it applies on another site.")
        out("    Nothing was typed on this offer.")
    elif not record.get("fields_detected"):
        out("    No empty free-text fields found: you typed in them already, not logged in, offer closed, "
            "or it applies on another site.")
    for label, value in record.get("answers", {}).items():
        shown = value if len(value) <= 120 else value[:117] + "..."
        out(f"    + {label}: {shown}")
    for label in record.get("left_blank", []):
        out(f"    - left blank for you: {label}")
    out("    Review everything in the browser tab, complete what is missing and, if you want to apply,")
    out("    click 'Enviar postulación' YOURSELF. luk-assist never submits.")
    input_fn("    [Enter] next offer, Ctrl+C to stop... ")
