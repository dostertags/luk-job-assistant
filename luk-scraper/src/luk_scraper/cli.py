"""Command line: ``luk-scraper crawl --out offers.jsonl`` (or ``python -m luk_scraper``).

Exit codes: 0 OK; 1 error or partial result; 2 robots.txt disallows the crawl or Luk is
blocking us (stop; do not retry or work around it). argparse also uses 2 for usage errors.
Progress and a one-line summary go to stderr.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from typing import Optional

from . import __version__, config, crawler, output
from .fetcher import Fetcher
from .robots import RobotsDisallowed

log = logging.getLogger("luk_scraper")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STOP = 2


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _delay(value: str) -> float:
    try:
        d = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not math.isfinite(d) or d < 0:
        raise argparse.ArgumentTypeError("must be a finite number of seconds")
    return d


def _countries(value: str) -> list[str]:
    try:
        return config.normalize_countries(value.split(","))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{e} (e.g. Chile or Chile,Perú)") from None


def _date_filter(value: str) -> str:
    v = value.strip()
    if v in config.DATE_FILTERS:
        return config.DATE_FILTERS[v]
    if v in config.DATE_FILTERS.values():
        return v
    raise argparse.ArgumentTypeError(
        f"unknown --date {value!r}; use one of {', '.join(config.DATE_FILTERS)}")


def _out_path(value: str) -> str:
    try:
        output.format_for(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="luk-scraper",
        description="Polite crawler for Luk's (takealuk.com) public job offers -> JSONL/CSV. "
                    "Unofficial; not affiliated with Luk or Buk.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    c = sub.add_parser("crawl", help="crawl the public listing and write offers to a file",
                       description="Crawl the public listing (Chile by default) and write the "
                                   "offers to --out. robots.txt is checked first.")
    c.add_argument("--out", required=True, type=_out_path,
                   help="output file: .jsonl/.ndjson (JSON Lines) or .csv")
    c.add_argument("--countries", type=_countries, default=None,
                   help="comma-separated countries, e.g. Chile,Perú (default: Chile). One or more "
                        f"of {', '.join(config.COUNTRIES)}; case and accents are ignored")
    c.add_argument("--date", type=_date_filter, default=None,
                   help="only recent offers: " + "|".join(config.DATE_FILTERS))
    c.add_argument("--max-pages", type=_positive_int, default=None,
                   help=f"stop after N listing pages (hard cap {config.MAX_PAGES})")
    c.add_argument("--no-enrich", action="store_true",
                   help="skip detail pages (no description, salary or dates; much faster)")
    c.add_argument("--enrich-limit", type=_positive_int, default=None,
                   help="enrich only the first N offers")
    c.add_argument("--delay", type=_delay, default=config.RATE_LIMIT_S,
                   help=f"seconds between requests (default {config.RATE_LIMIT_S}; "
                        f"never less than {config.MIN_DELAY_S})")
    c.add_argument("-q", "--quiet", action="store_true", help="only warnings and the summary")
    c.set_defaults(func=_cmd_crawl)
    return p


def _summary(stats: dict, out: str, wrote: Optional[int]) -> str:
    target = f"-> {out}" if wrote is not None else "(nothing written)"
    return (f"luk-scraper: {stats['status']}: {stats['offers']} offers from "
            f"{stats['pages']} pages, {stats['enriched']} enriched, {stats['errors']} errors, "
            f"{stats['requests']} requests in {stats['duration_s']}s "
            f"[stop: {stats['stop_reason']}] {target}")


def _cmd_crawl(args: argparse.Namespace) -> int:
    if args.delay < config.MIN_DELAY_S:
        log.warning("--delay %.2f is below the %.1fs floor; using %.1fs", args.delay,
                    config.MIN_DELAY_S, config.MIN_DELAY_S)
    fetcher = Fetcher(delay_s=args.delay)
    try:
        offers, stats = crawler.crawl(fetcher=fetcher, countries=args.countries,
                                      date_filter=args.date, max_pages=args.max_pages,
                                      enrich=not args.no_enrich,
                                      enrich_limit=args.enrich_limit)
    except RobotsDisallowed as e:
        print(f"luk-scraper: STOP: {e}. Nothing was crawled.", file=sys.stderr)
        return EXIT_STOP

    wrote = None
    if offers or stats["status"] == "OK":
        try:
            wrote = output.write(offers, args.out)
        except OSError as e:
            print(f"luk-scraper: could not write {args.out}: {e}", file=sys.stderr)
            return EXIT_ERROR
    print(_summary(stats, args.out, wrote), file=sys.stderr)
    if stats["blocked"] or stats["stop_reason"] == "robots":
        print("luk-scraper: Luk refused or robots.txt forbids further requests. Stop here; "
              "do not retry or work around it.", file=sys.stderr)
        return EXIT_STOP
    return EXIT_OK if stats["status"] == "OK" else EXIT_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.WARNING if getattr(args, "quiet", False) else logging.INFO)
    try:
        return args.func(args)
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)


if __name__ == "__main__":
    sys.exit(main())
