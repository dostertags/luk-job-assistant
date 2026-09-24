"""``luk-assist``: open Luk offers in a visible browser, pre-fill free-text answers, and STOP.

    luk-assist --init-answers                       # create your answers file (never overwrites)
    luk-assist --listings offers.jsonl [--cv CV.pdf] [--answers PATH] [--limit N]
               [--storage-state PATH] [--out review_queue.json] [--no-pause] [--fill-generic]

Exit codes: 0 done, 1 no offers to open, 2 bad input, 3 Playwright/Chromium missing, 130 interrupted.
There is no option to submit, to run headless or to type a password: those do not exist.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from luk_assist import __version__
from luk_assist.answers import (
    APP_NAME,
    AnswersError,
    default_answers_path,
    load_answers,
    make_answer_fn,
    write_template,
)
from luk_assist.browser import BrowserUnavailable, PlaywrightBrowser
from luk_assist.cv import merge, profile_from_text, seeded_keys, text_from_pdf
from luk_assist.runner import STATUS_FORM_NOT_FOUND, STATUS_NOT_OPENED, console_pause, run, save_queue
from luk_assist.targets import LUK_BASE_URL, TargetsError, load_targets

BANNER = "Pre-fills only. You review and click 'Enviar postulación' yourself. luk-assist never submits."
DEFAULT_LIMIT = 10
LUK_HOME = LUK_BASE_URL + "/"


def default_queue_path() -> Path:
    """``<user data dir>/luk-assist/review_queue.json``: it holds your answers, so not in the working dir."""
    return Path(user_data_dir(APP_NAME, appauthor=False)) / "review_queue.json"


def banner() -> str:
    rule = "=" * (len(BANNER) + 4)
    return f"{rule}\n  {BANNER}\n{rule}"


def inside_git_work_tree(path: Path) -> bool:
    """True if `path` is inside a git work tree (a place where a personal file could get committed)."""
    try:
        p = path.expanduser().resolve()
    except OSError:
        return False
    return any((parent / ".git").exists() for parent in (p, *p.parents))


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="luk-assist",
        description="Open Luk (takealuk.com) offers in a visible browser, pre-fill the free-text questions "
                    "from your answers, and stop so you can review and submit yourself.",
        epilog=BANNER,
    )
    p.add_argument("--listings", metavar="FILE",
                   help="offers to open: luk-scraper JSONL/CSV, a luk-cli --json file, or a .txt with one "
                        "offer URL or slug per line")
    p.add_argument("--cv", metavar="PDF",
                   help="your CV; only fills answers that are empty in your answers file")
    p.add_argument("--answers", metavar="PATH",
                   help=f"your answers file (default: {default_answers_path()})")
    p.add_argument("--init-answers", action="store_true",
                   help="create the answers template (never overwrites an existing file) and exit")
    p.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, metavar="N",
                   help=f"open at most N offers (default {DEFAULT_LIMIT})")
    p.add_argument("--storage-state", metavar="PATH",
                   help="use a Playwright storage-state file (e.g. the session saved by luk-cli's "
                        "'luk login') instead of luk-assist's own browser profile")
    p.add_argument("--out", metavar="PATH",
                   help=f"where to write the review queue (default: {default_queue_path()})")
    p.add_argument("--no-pause", action="store_true",
                   help="pre-fill every offer in its own tab without stopping; review the tabs afterwards")
    p.add_argument("--fill-generic", action="store_true",
                   help="opt-in: put a neutral sentence in free-text questions you have no answer for")
    p.add_argument("--version", action="version", version=f"luk-assist {__version__}")
    return p


def _err(message: str) -> None:
    print(f"luk-assist: error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None, *, browser_factory: Callable[[Path | None], Any] | None = None,
         input_fn: Callable[[str], str] = input) -> int:
    for stream in (sys.stdout, sys.stderr):  # "Enviar postulación" must print on any Windows console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    answers_path = Path(args.answers).expanduser() if args.answers else default_answers_path()

    if args.init_answers:
        created = write_template(answers_path)
        print(f"{'Created' if created else 'Left unchanged (it already exists)'}: {answers_path}")
        print("Edit it and fill in only what is true for you. Empty values and <placeholders> are never typed.")
        return 0
    if not args.listings:
        parser.error("--listings is required (create your answers first with --init-answers)")

    print(banner())
    for what, value in (("answers file", args.answers), ("CV", args.cv), ("review queue", args.out),
                        ("storage state", args.storage_state)):
        if value and inside_git_work_tree(Path(value)):
            print(f"WARNING: the {what} '{value}' is inside a git repository. Never commit it.")

    try:
        answers = load_answers(answers_path)
    except AnswersError as exc:
        _err(str(exc))
        return 2
    if not answers_path.is_file():
        print(f"No answers file at {answers_path}. Run 'luk-assist --init-answers' and edit it; "
              "until then only your CV (if given) provides answers.")

    if args.cv:
        cv_path = Path(args.cv).expanduser()
        if not cv_path.is_file():
            _err(f"CV not found: {cv_path}")
            return 2
        derived = profile_from_text(text_from_pdf(cv_path))
        seeded = seeded_keys(answers, derived)
        answers = merge(answers, derived)
        print(f"CV: used for {', '.join(seeded)} (your answers file wins)." if seeded
              else "CV: nothing new to use (your answers file wins).")

    try:
        targets = load_targets(args.listings, limit=args.limit)
    except TargetsError as exc:
        _err(str(exc))
        return 2
    if not targets:
        print(f"No Luk offers found in {args.listings}.")
        return 1

    storage_state: Path | None = None
    if args.storage_state:
        storage_state = Path(args.storage_state).expanduser()
        if not storage_state.is_file():
            _err(f"storage state not found: {storage_state}")
            return 2

    answer_fn = make_answer_fn(answers, fill_generic=args.fill_generic)
    out_path = Path(args.out).expanduser() if args.out else default_queue_path()
    factory = browser_factory or (lambda state: PlaywrightBrowser(storage_state=state))
    browser = factory(storage_state)
    try:
        browser.start()
    except BrowserUnavailable as exc:
        _err(str(exc))
        return 3
    except Exception as exc:  # noqa: BLE001 - e.g. the profile is open in another luk-assist window
        _err(f"could not start the browser ({type(exc).__name__}: {exc}). Is another luk-assist window open?")
        return 3

    print(f"Opening {len(targets)} offer(s), each in its own tab.")
    try:
        if not args.no_pause and storage_state is None:
            browser.show(LUK_HOME)
            input_fn("Log in to Luk in the browser window if you are not logged in yet (you type your own "
                     "password; luk-assist never does). Then press Enter to start... ")
        pause = None if args.no_pause else partial(console_pause, input_fn=input_fn)
        records = run(targets, browser, answer_fn, pause=pause)
        save_queue(out_path, records)
        filled = sum(len(r["answers"]) for r in records)
        failed = sum(1 for r in records if r["status"] == STATUS_NOT_OPENED)
        no_form = sum(1 for r in records if r["status"] == STATUS_FORM_NOT_FOUND)
        print(f"\n{len(records) - failed} offer(s) opened, {filled} answer(s) typed"
              f"{f', {no_form} without an application form' if no_form else ''}"
              f"{f', {failed} could not be opened' if failed else ''}; none submitted. Review queue: {out_path}")
        print("Review each tab and click 'Enviar postulación' yourself where you want to apply. "
              "luk-assist waits until you have closed every tab (or the browser window).")
        browser.wait_until_closed()  # returns only when no tab is left, so close() below closes nothing you use
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Nothing was submitted.")
        return 130
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
