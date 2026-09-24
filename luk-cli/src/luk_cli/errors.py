"""Error hierarchy (spec §5.3, §5.4).

Every failure a user or MCP client can see is a `LukError` carrying a JSON `code`, the process
`exit_code`, a `message` and an optional `hint`. Messages never contain cookies, headers or reprs.
"""

from __future__ import annotations

from typing import ClassVar, Literal

ErrorCode = Literal[
    "INVALID_ARGUMENT",
    "AUTH_REQUIRED",
    "NOT_FOUND",
    "NETWORK",
    "RATE_LIMITED",
    "BLOCKED",
    "BUDGET_EXCEEDED",
    "SITE_CHANGED",
    "INTERRUPTED",
    "INTERNAL",
]

EXIT_CODES: dict[ErrorCode, int] = {
    "INVALID_ARGUMENT": 1,
    "AUTH_REQUIRED": 2,
    "NOT_FOUND": 3,
    "NETWORK": 4,
    "RATE_LIMITED": 4,
    "BLOCKED": 4,
    "BUDGET_EXCEEDED": 4,
    "SITE_CHANGED": 5,
    "INTERRUPTED": 130,
    "INTERNAL": 1,
}


class LukError(Exception):
    """Base error: `code` (§5.4), `exit_code` (§5.3), human `message`, optional `hint` (next step)."""

    code: ClassVar[ErrorCode] = "INTERNAL"
    default_message: ClassVar[str] = "unexpected error"
    default_hint: ClassVar[str | None] = None

    def __init__(self, message: str | None = None, *, hint: str | None = None) -> None:
        self.message = message or self.default_message
        self.hint = hint if hint is not None else self.default_hint
        super().__init__(self.message)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]


class InvalidArgument(LukError):
    """Bad user input or configuration (exit 1); raised before any request is sent."""

    code = "INVALID_ARGUMENT"
    default_message = "invalid argument"


class SessionBusy(InvalidArgument):
    """A session lock is held by another luk process (e.g. 'login already in progress')."""

    default_message = "the session is busy in another luk process"


class AuthRequired(LukError):
    """No session, expired session, or an onboarding redirect (exit 2, §4.5)."""

    code = "AUTH_REQUIRED"
    default_message = "Session expired or missing. Run `luk login`."
    default_hint = "Run `luk login` (a browser window opens; you type your own credentials), then retry."


class NotFound(LukError):
    """Unknown job/company slug or area (exit 3)."""

    code = "NOT_FOUND"
    default_message = "not found on Luk"


class NetworkError(LukError):
    """Connection/read failures or retryable statuses after the last attempt (exit 4, §4.9)."""

    code = "NETWORK"
    default_message = "network error talking to Luk"
    default_hint = "Check your connection, then retry."


class RateLimited(LukError):
    """HTTP 429 after the last attempt, or a Retry-After above 60 s (exit 4, §4.9)."""

    code = "RATE_LIMITED"
    default_message = "Luk is rate-limiting requests"
    default_hint = "Wait a few minutes before retrying; never work around it."


class Blocked(LukError):
    """403, `cf-mitigated` or a challenge page: stop, never retry with other headers (exit 4)."""

    code = "BLOCKED"
    default_message = "Blocked by Luk (HTTP 403) — stopping; not retrying (repo red line: no evasion)"
    default_hint = "Stop and tell the user; revisit ADR-0001 instead of working around it."

    @classmethod
    def for_status(cls, status: int) -> Blocked:
        return cls(f"Blocked by Luk (HTTP {status}) — stopping; not retrying (repo red line: no evasion)")


class AlgoliaRejected(Blocked):
    """Algolia answered 401/403 even after one rediscovery of the public key (§4.9)."""

    default_message = "Algolia refused the suggestions request — `suggest` is disabled"
    default_hint = "Luk's public search key may have changed; retry later."


class BudgetExceeded(LukError):
    """The rolling hourly request budget (≤240, §1.5) is used up (exit 4)."""

    code = "BUDGET_EXCEEDED"
    default_message = "hourly budget reached"
    default_hint = "Wait up to an hour; LUK_MAX_REQ_PER_HOUR can only lower the budget."


class SiteChanged(LukError):
    """A required anchor of a page is missing (exit 5, §5.5)."""

    code = "SITE_CHANGED"
    default_message = "Luk's page structure changed"

    @classmethod
    def missing_anchor(cls, page: str, selector: str, capture_path: str) -> SiteChanged:
        """The path is double-quoted so the suggested command pastes as one command: a query with 2+
        params holds `&`, a command separator in bash and cmd that PowerShell refuses to parse. It is
        URL-encoded (no `"`, `$` or backtick), so the quotes work in all three shells."""
        return cls(
            f"Luk {page} page changed: missing {selector}. "
            f'Run `luk debug capture "{capture_path}"` and report it.'
        )


class Interrupted(LukError):
    """Ctrl-C (exit 130)."""

    code = "INTERRUPTED"
    default_message = "interrupted"


class InternalError(LukError):
    """Anything unexpected (exit 1)."""

    code = "INTERNAL"
    default_hint = "Run `luk doctor`."


class UnsafeRequest(InternalError):
    """A request or redirect that breaks §4.7 (non-GET, non-https, foreign host); never sent."""

    default_message = "refused an unsafe request"


class ScrubFailed(InternalError):
    """The capture scrubber found identity text after scrubbing; nothing was written (§8.1)."""

    default_message = "capture not written: identity text survived scrubbing"
