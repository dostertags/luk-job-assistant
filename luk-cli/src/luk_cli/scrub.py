"""Capture scrubber for `luk debug capture` (spec §8.1). In memory only: the raw body and response
headers are never written; `scrub` returns the text the api may write, or raises.

Mechanism (selectolax, every node of the document incl. the doctype's siblings, in document order):
- identity: name/email from meta.json plus the captured header (`.header-dropdown-name`,
  `.header-dropdown-email`, the name in `aria-label="Avatar de …"`), expanded by `identity_strings`
  (casefolded + accent-stripped forms, `-`/`_`/`.`/joined slugs, name tokens ≥3 chars that are not
  particles like "del", the email and its local part). They are matched case- and accent-
  insensitively on letter/digit boundaries (so "Ana" never hits "Analista", but "cv_ana.pdf" is hit).
- every text node, attribute value and JSON-LD string goes through `_clean`: avatar URLs
  (googleusercontent, licdn), `/rails/active_storage/…`, `linkedin.com/in/<handle>`, the
  `X-Amz-*`/`sig`/`signature`/`token` (and `authenticity_token`) query params, Rails signed blobs
  `[A-Za-z0-9+/_=-]{16,}--[A-Za-z0-9+/_=-]{16,}`, emails, RUTs `\\b\\d{1,2}\\.?\\d{3}\\.?\\d{3}-[\\dkK]\\b`,
  CL phones `(\\+?56\\s?)?9\\s?\\d{4}\\s?\\d{4}` (not inside a longer digit run) and the identity become
  `SCRUBBED`; a percent-encoded value is also checked decoded. `class` and SVG `d` values only get
  the identity pass (BEM `--modifier`s look like signed blobs, path data like RUTs).
- attributes: `authenticity_token` inputs, `meta[name=csrf-token|csp-nonce]` content, `nonce`,
  `data-google-one-tap-csrf-token-value`, `data-amplitude-(user|device)-id-value`, turbo
  `signed-stream-name`, and any attribute whose name mentions user|email|phone|rut|avatar →
  `SCRUBBED`; `aria-label="Avatar de …"` → "Avatar de SCRUBBED"; on `/profile/*` every
  `input[value]` and `textarea` → `SCRUBBED`.
- CV file names (a whole text node or attribute value shaped like `name.pdf|doc|docx|odt|rtf`, not a
  path or `.ext` list, and every `download` value) → `cv-N.<ext>`, numbered in document order.
- `<script>` bodies are emptied except `application/ld+json`, whose strings are cleaned and which is
  re-serialised (compact, `<`/`>`/`&` as \\u003c/\\u003e/\\u0026 like Rails) only if something
  changed; unparsable JSON-LD is emptied. Comments are dropped.

Fail closed: the result is parsed again and every identity string searched for in text, attribute
values (also percent-decoded) and finally the raw markup; any hit raises ScrubFailed naming only the
location (e.g. "text in <p>"), never the matched text.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import unquote

from selectolax.parser import HTMLParser, Node

from luk_cli import parsers
from luk_cli.errors import ScrubFailed
from luk_cli.inputs import fold

MASK = "SCRUBBED"
MARKUP = "markup outside text and attribute values"
MIN_IDENTITY_LEN = 3
NAME_PARTICLES = frozenset({"del", "las", "los", "das", "dos", "van", "von", "der", "den"})
SCRUB_HINT = "Nothing was written. Report the location (not the page) so the scrubber can be fixed."

_SLUG_SEPARATORS = ("-", "_", ".", "")
_SECRET_ATTRS = frozenset(
    {
        "nonce", "signed-stream-name", "data-google-one-tap-csrf-token-value",
        "data-amplitude-user-id-value", "data-amplitude-device-id-value",
    }
)
_SECRET_META = frozenset({"csrf-token", "csp-nonce"})
_PERSONAL_ATTR_RE = re.compile(r"user|e-?mail|phone|rut|avatar")
_STRUCTURAL_ATTRS = frozenset({"class", "d"})  # CSS classes, SVG path data: only the identity is matched
_NAME_TOKEN_RE = re.compile(r"[^\W_]+")
_BOUNDARY = r"[^\W_]"  # a letter or digit: identity matches may not touch one
_FILE_NAME_RE = re.compile(r"[^\s/\\.](?:[^/\\\n]{0,250}[^\s/\\])?\.(pdf|docx?|odt|rtf)", re.IGNORECASE)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:https?:)?//[^\s\"'<>]*?(?:googleusercontent|licdn)\.com[^\s\"'<>]*", re.IGNORECASE), MASK),
    (re.compile(r"(/rails/active_storage/)[^\s\"'<>]*"), rf"\g<1>{MASK}"),
    (re.compile(r"(linkedin\.com/in/)[^\s\"'<>/?#]+", re.IGNORECASE), rf"\g<1>{MASK}"),
    (
        re.compile(r"([?&;](?:X-Amz-[A-Za-z-]+|sig|signature|token|authenticity_token)=)[^&#\s\"'<>]*", re.IGNORECASE),
        rf"\g<1>{MASK}",
    ),
    (re.compile(r"[A-Za-z0-9+/_=-]{16,}--[A-Za-z0-9+/_=-]{16,}"), MASK),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), MASK),
    (re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b"), MASK),
    (re.compile(r"(?<!\d)(?:\+?56\s?)?9\s?\d{4}\s?\d{4}(?!\d)"), MASK),
)
_JSON_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})


@dataclass(frozen=True)
class Identity:
    """Who must never appear in a capture (from meta.json and the captured page header)."""

    name: str | None
    email: str | None


def identity_strings(identity: Identity) -> list[str]:
    """Every variant searched for, folded (NFKD, no accents, casefolded), longest first: the name,
    its `-`/`_`/`.`/joined slugs, each name token ≥3 chars except particles, the email and its local
    part. Strings shorter than 3 characters are dropped."""
    found: set[str] = set()
    if identity.name:
        name = fold(identity.name)
        tokens = _NAME_TOKEN_RE.findall(name)
        found.add(name)
        found.update(separator.join(tokens) for separator in _SLUG_SEPARATORS)
        found.update(token for token in tokens if token not in NAME_PARTICLES)
    if identity.email:
        email = fold(identity.email)
        found.update((email, email.partition("@")[0]))
    return sorted((s for s in found if len(s) >= MIN_IDENTITY_LEN), key=lambda s: (-len(s), s))


def find_identity(text: str, identity: Identity) -> list[str]:
    """Locations (short, identity-free descriptions such as "text in <title>" or "attribute data-x on
    <div>") where an identity string survives in the HTML `text`; [] when clean."""
    return _locations(text, _Matcher(identity_strings(identity)))


def scrub(html: str, *, path: str, identity: Identity) -> str:
    """Scrub `html` captured from allowlisted `path` (a query is ignored) per the module docstring and
    run the fail-closed identity check; raises errors.ScrubFailed (exit 1) on any surviving identity."""
    tree = HTMLParser(html)
    header_name, header_email = parsers.header_identity(tree)
    identities = (identity, Identity(header_name, header_email), Identity(parsers.avatar_name(tree), None))
    matcher = _Matcher(sorted({s for who in identities for s in identity_strings(who)}, key=lambda s: (-len(s), s)))
    route = path.partition("?")[0]
    scrubber = _Scrubber(matcher, profile=route == "/profile" or route.startswith("/profile/"))
    for node in scrubber.targets(tree):
        scrubber.apply(node)
    result = tree.html or ""
    locations = _locations(result, matcher)
    if locations:
        raise ScrubFailed(f"{ScrubFailed.default_message} ({', '.join(locations)})", hint=SCRUB_HINT)
    return result


# --- identity matching ------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c)).casefold()


class _Matcher:
    """Case- and accent-insensitive search/replace of identity strings on letter/digit boundaries."""

    def __init__(self, strings: Sequence[str]) -> None:
        alternatives = "|".join(r"\s+".join(map(re.escape, s.split(" "))) for s in strings)
        self._pattern = re.compile(rf"(?<!{_BOUNDARY})(?:{alternatives})(?!{_BOUNDARY})") if strings else None

    def __bool__(self) -> bool:
        return self._pattern is not None

    def search(self, text: str) -> bool:
        return self._pattern is not None and self._pattern.search("".join(map(_fold_char, text))) is not None

    def sub(self, text: str) -> str:
        """`text` with every match replaced by MASK (spans mapped back through the per-character fold)."""
        if self._pattern is None:
            return text
        pieces = [_fold_char(char) for char in text]
        origin = [index for index, piece in enumerate(pieces) for _ in piece]
        parts: list[str] = []
        done = 0
        for match in self._pattern.finditer("".join(pieces)):
            start, end = origin[match.start()], origin[match.end() - 1] + 1
            while end < len(text) and not pieces[end]:  # combining marks of the last matched letter
                end += 1
            parts += [text[done:start], MASK]
            done = end
        return "".join(parts) + text[done:] if parts else text


def _clean(value: str, matcher: _Matcher) -> str:
    """The value-level rules of the module docstring; a percent-encoded value is also checked decoded."""
    cleaned = _clean_once(value, matcher)
    decoded = unquote(cleaned)
    if decoded != cleaned:
        decoded_clean = _clean_once(decoded, matcher)
        if decoded_clean != decoded:
            return decoded_clean
    return cleaned


def _clean_once(value: str, matcher: _Matcher) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return matcher.sub(value)


def _clean_json(value: Any, matcher: _Matcher) -> Any:
    if isinstance(value, str):
        return _clean(value, matcher)
    if isinstance(value, list):
        return [_clean_json(item, matcher) for item in value]
    if isinstance(value, dict):
        return {key: _clean_json(item, matcher) for key, item in value.items()}
    return value


# --- the document walk ------------------------------------------------------------------------


def _walk(tree: HTMLParser) -> Iterator[Node]:
    """Every node (elements, text, comments, the doctype and its siblings) in document order.
    Hand-rolled: selectolax's `Node.traverse()` misses what precedes `<html>` and, from an inner node,
    runs on into the following siblings."""
    top = tree.root
    while top is not None and top.prev is not None:
        top = top.prev
    stack = [top]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        yield node
        stack += [node.next, node.child]


def _is_element(node: Node) -> bool:
    return node.tag[0] not in "-_!"


class _Scrubber:
    def __init__(self, matcher: _Matcher, *, profile: bool) -> None:
        self.matcher = matcher
        self.profile = profile
        self.files: dict[str, str] = {}

    def owns_children(self, element: Node) -> bool:
        """Elements whose body is rewritten as a whole (their text children are never visited)."""
        return element.tag == "script" or (self.profile and element.tag == "textarea")

    def targets(self, tree: HTMLParser) -> list[Node]:
        """The nodes to scrub, collected before any mutation (so none is visited after removal)."""
        return [
            node for node in _walk(tree)
            if node.tag != "-text" or node.parent is None or not self.owns_children(node.parent)
        ]

    def apply(self, node: Node) -> None:
        if node.tag == "_comment":
            node.decompose()
        elif node.tag == "-text":
            raw = node.text(deep=False)
            new = self.text(raw)
            if new != raw:
                node.replace_with(new)
        elif _is_element(node):
            if node.tag == "script":
                self.script(node)
            elif self.owns_children(node):
                self.replace_body(node, MASK if node.text() else "")
            for name, value in node.attributes.items():
                if value is not None and (new := self.attribute(node, name.lower(), value)) != value:
                    node.attrs[name] = new

    def text(self, raw: str) -> str:
        core = raw.strip()
        if match := _FILE_NAME_RE.fullmatch(core):
            return raw.replace(core, self.file_name(core, match[1]))
        return _clean(raw, self.matcher)

    def attribute(self, element: Node, name: str, value: str) -> str:
        attributes = element.attributes
        if name == "aria-label" and value.startswith(parsers.AVATAR_PREFIX):
            return f"{parsers.AVATAR_PREFIX} {MASK}"
        if name in _SECRET_ATTRS or _PERSONAL_ATTR_RE.search(name):
            return MASK
        if element.tag == "meta" and name == "content" and (attributes.get("name") or "").lower() in _SECRET_META:
            return MASK
        if element.tag == "input" and name == "value" and (self.profile or attributes.get("name") == "authenticity_token"):
            return MASK
        core = value.strip()
        if match := _FILE_NAME_RE.fullmatch(core):
            return self.file_name(core, match[1])
        if name == "download" and core:
            return MASK
        if name in _STRUCTURAL_ATTRS:  # BEM "--modifier"s look like signed blobs, path data like RUTs
            return self.matcher.sub(value)
        return _clean(value, self.matcher)

    def file_name(self, name: str, extension: str) -> str:
        """`cv-N.<ext>`, N in order of first appearance (the same file name keeps its N)."""
        return self.files.setdefault(name, f"cv-{len(self.files) + 1}.{extension.lower()}")

    def script(self, node: Node) -> None:
        body = node.text()
        if (node.attributes.get("type") or "").strip().lower() != "application/ld+json":
            self.replace_body(node, "")
            return
        try:
            data = json.loads(body, strict=False)
        except ValueError:
            self.replace_body(node, "")
            return
        cleaned = _clean_json(data, self.matcher)
        if cleaned != data:
            self.replace_body(node, json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).translate(_JSON_ESCAPES))

    @staticmethod
    def replace_body(node: Node, body: str) -> None:
        for child in list(node.iter(include_text=True)):
            child.decompose()
        if body:
            node.insert_child(body)


# --- fail-closed check ------------------------------------------------------------------------


def _locations(html: str, matcher: _Matcher) -> list[str]:
    """Where an identity string survives: text nodes, attribute values (also percent-decoded), else the
    raw markup (tag/attribute names, comments). A location that would itself reveal it is generalised."""
    if not matcher:
        return []
    found: list[str] = []
    for node in _walk(HTMLParser(html)):
        if node.tag == "-text" and matcher.search(node.text(deep=False)):
            found.append(f"text in <{node.parent.tag if node.parent is not None else 'document'}>")
        elif _is_element(node):
            found += [
                f"attribute {name} on <{node.tag}>"
                for name, value in node.attributes.items()
                if value is not None and (matcher.search(value) or matcher.search(unquote(value)))
            ]
    locations = [MARKUP if matcher.search(location) else location for location in found]
    if not locations and matcher.search(html):
        locations = [MARKUP]
    return list(dict.fromkeys(locations))
