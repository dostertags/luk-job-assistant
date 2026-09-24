"""robots.txt: fetched and checked before every crawl.

Luk's robots.txt (verified live 2026-09-23) starts with ``User-agent: *`` / ``Allow: /`` and
then disallows private pages, Turbo fragments and ``?sort_by=`` / ``?locale=`` using wildcards.

The standard library's :mod:`urllib.robotparser` is used, but on its own it is not enough for
that file: it applies the *first* matching rule (so a leading ``Allow: /`` would allow
everything) and it does not understand ``*`` / ``$`` wildcards. So a URL is allowed only when
BOTH agree:

1. :class:`urllib.robotparser.RobotFileParser.can_fetch`, and
2. RFC 9309 matching: the group for our product token (else ``*``), the *longest* matching
   rule wins, ``Allow`` wins a tie, ``*`` and ``$`` are wildcards.

When in doubt we don't crawl. HTTP semantics follow RFC 9309 §2.3.1: a 4xx robots.txt means
"no rules" (allowed), except 401/403 which we treat as disallowed; an unreachable robots.txt
(5xx or network errors after retries) means "don't crawl".
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

from . import config
from .fetcher import Blocked, FetchError

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    """robots.txt disallows the URL for our User-Agent (or could not be read). Do not crawl."""

    def __init__(self, url: str, reason: str = "disallowed by robots.txt"):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


def _groups(text: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    """robots.txt -> ``[(user_agents, [(is_allow, path_pattern), ...]), ...]``."""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    seen_rule = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if seen_rule:
                groups.append((agents, rules))
                agents, rules, seen_rule = [], [], False
            agents.append(value.lower())
        elif name in ("allow", "disallow") and agents:
            seen_rule = True
            rules.append((name == "allow", value))
    if agents:
        groups.append((agents, rules))
    return groups


def _rule_matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(piece) for piece in body.split("*"))
    return re.match(regex + ("$" if anchored else ""), path) is not None


def _rfc9309_allowed(text: str, token: str, url: str) -> bool:
    groups = _groups(text)
    token = token.lower()
    rules = [r for agents, rs in groups if token in agents for r in rs]
    if not any(token in agents for agents, _ in groups):
        rules = [r for agents, rs in groups if "*" in agents for r in rs]
    parts = urlsplit(url)
    path = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
    best: Optional[tuple[int, bool]] = None
    for allow, pattern in rules:
        pattern = unquote(pattern)
        if not pattern or not _rule_matches(pattern, path):   # empty Disallow = no rule
            continue
        length = len(pattern)
        if best is None or length > best[0] or (length == best[0] and allow):
            best = (length, allow)
    return True if best is None else best[1]


#: What a 401/403 robots.txt means (RFC 9309 §2.3.1.3 lets us assume complete disallow).
DISALLOW_ALL_TEXT = "User-agent: *\nDisallow: /\n"


class RobotsPolicy:
    """A parsed robots.txt, answering "may our User-Agent fetch this URL?".

    It is built from robots.txt text only: there is no allow-all (or any other) switch. "No
    rules" is only ever the result of reading the site's robots.txt (an empty file or a 404).
    """

    def __init__(self, text: str = ""):
        self.text = text or ""
        self._parser = RobotFileParser()
        self._parser.parse(self.text.splitlines())

    @classmethod
    def from_text(cls, text: str) -> "RobotsPolicy":
        return cls(text)

    def allowed(self, url: str, user_agent: str = config.PRODUCT_TOKEN) -> bool:
        token = user_agent.split("/", 1)[0].strip() or config.PRODUCT_TOKEN
        return self._parser.can_fetch(token, url) and _rfc9309_allowed(self.text, token, url)

    def crawl_delay(self, user_agent: str = config.PRODUCT_TOKEN) -> Optional[float]:
        token = user_agent.split("/", 1)[0].strip() or config.PRODUCT_TOKEN
        delay = self._parser.crawl_delay(token)
        try:
            return float(delay) if delay is not None else None
        except (TypeError, ValueError):
            return None


def load(fetcher, url: str = config.ROBOTS_URL) -> RobotsPolicy:
    """Fetch and parse robots.txt.

    Raises :class:`~luk_scraper.fetcher.Blocked` when Luk keeps refusing us, and
    :class:`RobotsDisallowed` when robots.txt is unreachable (we don't crawl blind).
    """
    try:
        status, text = fetcher.get_text_status(url)
    except Blocked:
        raise
    except FetchError as e:
        raise RobotsDisallowed(url, f"robots.txt unreachable ({e}); not crawling") from e
    if status >= 500:
        raise RobotsDisallowed(url, f"robots.txt answered {status}; not crawling")
    if status in (401, 403):
        return RobotsPolicy.from_text(DISALLOW_ALL_TEXT)
    if 400 <= status < 500:
        log.info("robots.txt answered %s: no rules apply", status)
        return RobotsPolicy.from_text("")
    return RobotsPolicy.from_text(text)


def ensure_allowed(policy: RobotsPolicy, url: str, user_agent: str) -> None:
    """Raise :class:`RobotsDisallowed` unless ``policy`` allows ``url`` for ``user_agent``."""
    if not policy.allowed(url, user_agent):
        raise RobotsDisallowed(url)
