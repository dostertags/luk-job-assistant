"""redact.py — log filter, cookie masking and pinned http loggers (spec §4.6)."""

import io
import logging

import pytest

from luk_cli import redact


def test_cookie_pairs_are_masked():
    text = "Cookie: _portal_de_empleos_session=abc123%2B==; theme=dark; remember_user_token=W1sxXQ--x"
    assert redact.redact(text) == "Cookie: _portal_de_empleos_session=***; theme=dark; remember_user_token=***"
    assert redact.redact("'_portal_de_empleos_session=v1' \"remember_user_token=v2\"") == (
        "'_portal_de_empleos_session=***' \"remember_user_token=***\""
    )


def test_registered_cookie_values_are_masked_anywhere():
    redact.register_secret("LOADED_VALUE_123456")
    assert redact.redact("value is LOADED_VALUE_123456!") == "value is ***!"


def test_short_values_are_not_registered():
    redact.register_secret("1")
    assert redact.redact("page 1 of 10") == "page 1 of 10"


def _logger_with_stream(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_filter_masks_message_args_and_exceptions():
    logger, stream = _logger_with_stream("luk_cli.test.filter")
    logger.handlers[0].addFilter(redact.RedactingFilter())
    redact.register_secret("SECRET_IN_ARGS_0001")
    logger.debug("sending %s", "_portal_de_empleos_session=RAWVALUE")
    logger.info("jar holds %s", "SECRET_IN_ARGS_0001")
    try:
        raise RuntimeError("remember_user_token=RAWTOKEN")
    except RuntimeError:
        logger.exception("failed")
    out = stream.getvalue()
    assert "RAWVALUE" not in out and "SECRET_IN_ARGS_0001" not in out and "RAWTOKEN" not in out
    assert "sending _portal_de_empleos_session=***" in out and "jar holds ***" in out
    assert "RuntimeError: remember_user_token=***" in out


def test_installed_redaction_reaches_records_of_any_logger():
    redact.install_log_redaction()
    redact.install_log_redaction()  # idempotent
    logger, stream = _logger_with_stream("thirdparty.lib")  # no filter on this handler
    logger.warning("header %s", "_portal_de_empleos_session=RAWVALUE2")
    assert "RAWVALUE2" not in stream.getvalue()
    root = logging.getLogger()
    assert sum(isinstance(f, redact.RedactingFilter) for f in root.filters) == 1


@pytest.mark.parametrize("name", ["httpx", "httpcore"])
def test_http_loggers_pinned_to_warning(name):
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger(name).setLevel(logging.DEBUG)
    redact.pin_http_loggers()
    assert logging.getLogger(name).level == logging.WARNING
    assert not logging.getLogger(f"{name}.connection").isEnabledFor(logging.DEBUG)
