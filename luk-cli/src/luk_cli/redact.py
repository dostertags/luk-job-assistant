"""Secrets hygiene for logs and error text (spec §4.6).

- `redact(text)` masks `_portal_de_empleos_session=…` / `remember_user_token=…` and every
  registered (loaded) cookie value with `***`; the CLI and MCP top-level handlers run it last.
- `RedactingFilter` applies it to log records; `install_log_redaction()` puts it on the root logger,
  its handlers and the LogRecord factory, so records of any logger are masked when created.
- `pin_http_loggers()` keeps httpx/httpcore at WARNING (httpcore logs raw Set-Cookie at DEBUG).
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

MASK = "***"
_COOKIE_PAIR_RE = re.compile(r"(_portal_de_empleos_session|remember_user_token)=[^;\s'\"]+")
_MIN_SECRET_LEN = 8  # shorter values are not credentials and would mask ordinary text
_secrets: set[str] = set()
_secrets_lock = threading.Lock()
_FORMATTER = logging.Formatter()


def register_secret(value: str) -> None:
    """Mask `value` in everything `redact` sees from now on (called for every loaded cookie value)."""
    if len(value) >= _MIN_SECRET_LEN:
        with _secrets_lock:
            _secrets.add(value)


def redact(text: str) -> str:
    text = _COOKIE_PAIR_RE.sub(lambda match: f"{match.group(1)}={MASK}", text)
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


class RedactingFilter(logging.Filter):
    """Masks the message, arguments, exception text and stack of every record it sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # malformed %-args: logging reports it at emit time
            return True
        record.msg, record.args = redact(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = _FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FILTER = RedactingFilter()
_install_lock = threading.Lock()


def pin_http_loggers() -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def install_log_redaction() -> None:
    """Idempotently attach the filter to root + its handlers and wrap the LogRecord factory."""
    with _install_lock:
        root = logging.getLogger()
        for target in (root, *root.handlers):
            if not any(isinstance(f, RedactingFilter) for f in target.filters):
                target.addFilter(_FILTER)
        base = logging.getLogRecordFactory()
        if not getattr(base, "_luk_redacting", False):
            logging.setLogRecordFactory(_redacting_factory(base))


def _redacting_factory(base: Callable[..., logging.LogRecord]) -> Callable[..., logging.LogRecord]:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        _FILTER.filter(record)
        return record

    factory._luk_redacting = True  # type: ignore[attr-defined]
    return factory
