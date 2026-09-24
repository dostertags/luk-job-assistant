"""Identity and policy constants: honest UA (never an e-mail), delays, filters."""
from __future__ import annotations

import re

import pytest

from luk_scraper import __version__, config


def test_default_user_agent_is_honest_and_has_no_email(monkeypatch):
    monkeypatch.delenv(config.CONTACT_ENV, raising=False)
    ua = config.user_agent()
    assert ua == f"luk-scraper/{__version__} (+{config.DEFAULT_CONTACT})"
    assert "@" not in ua
    assert not re.search(r"mozilla|chrome|safari|gecko", ua, re.I)


def test_contact_env_is_honoured(monkeypatch):
    monkeypatch.setenv(config.CONTACT_ENV, "https://example.com/about-my-crawler")
    assert config.user_agent() == f"luk-scraper/{__version__} (+https://example.com/about-my-crawler)"


@pytest.mark.parametrize("value", [
    "paz.prueba@example.com",                  # an e-mail address
    "mailto:paz.prueba@example.com",
    "https://paz.prueba@example.com/",         # userinfo in a URL
    "https://example.com/a b",                 # whitespace
    "https://example.com/)(",                  # would break the UA comment
    "https://example.com/\r\nX-Evil: 1",       # header injection
    "ftp://example.com/",
    "Paz Prueba",
])
def test_contact_env_rejects_emails_and_junk(monkeypatch, value):
    monkeypatch.setenv(config.CONTACT_ENV, value)
    ua = config.user_agent()
    assert "@" not in ua and "\n" not in ua and "Paz" not in ua
    assert ua.endswith(f"(+{config.DEFAULT_CONTACT})")


@pytest.mark.parametrize("value", [
    "https://example.com/\x7f",                # control characters would reach the header
    "https://example.com/\tx",
    "https://example.com/año",                 # non-ASCII: percent-encode it instead
])
def test_contact_env_must_be_printable_ascii(monkeypatch, value):
    monkeypatch.setenv(config.CONTACT_ENV, value)
    assert config.user_agent().endswith(f"(+{config.DEFAULT_CONTACT})")


def test_a_nul_in_the_contact_never_reaches_the_header(monkeypatch):
    # Most platforms refuse a NUL inside an environment variable ("embedded null byte"), so the
    # same validator is exercised through the explicit contact argument and the honest-UA check.
    monkeypatch.delenv(config.CONTACT_ENV, raising=False)
    assert config.user_agent("https://example.com/\x00").endswith(f"(+{config.DEFAULT_CONTACT})")
    assert not config.is_honest_user_agent("luk-scraper/0.1.0 (+https://example.com/\x00)")


def test_default_user_agent_passes_the_honest_check(monkeypatch):
    monkeypatch.delenv(config.CONTACT_ENV, raising=False)
    assert config.is_honest_user_agent(config.user_agent())
    monkeypatch.setenv(config.CONTACT_ENV, "https://example.com/about-my-crawler")
    assert config.is_honest_user_agent(config.user_agent())


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/128.0",
    "luk-scraper/0.1.0 (+mailto:paz.prueba@example.com)",
    "luk-scraper/0.1.0 (+https://example.com/contact) extra",
    None, 42,
])
def test_is_honest_user_agent_rejects_anything_else(ua):
    assert not config.is_honest_user_agent(ua)


def test_blank_contact_env_uses_default(monkeypatch):
    monkeypatch.setenv(config.CONTACT_ENV, "   ")
    assert config.contact() == config.DEFAULT_CONTACT


def test_explicit_contact_argument_is_validated(monkeypatch):
    monkeypatch.delenv(config.CONTACT_ENV, raising=False)
    assert config.user_agent("https://example.com/x").endswith("(+https://example.com/x)")
    assert "@" not in config.user_agent("paz.prueba@example.com")


def test_politeness_constants():
    assert config.RATE_LIMIT_S >= 2.0
    assert config.MIN_DELAY_S == 1.0
    assert config.clamp_delay(0) == 1.0
    assert config.FORBIDDEN_PARAMS == {"sort_by", "locale"}
    assert config.COUNTRIES_DEFAULT == ["Chile"]
    assert config.MAX_PAGES == 400


def test_countries_are_the_site_checkbox_values():
    assert config.COUNTRIES == ("Brasil", "Chile", "Colombia", "México", "Perú")
    assert config.WORLDWIDE_PARAM == ("worldwide", "1")


@pytest.mark.parametrize("given,canonical", [
    ("Chile", "Chile"), ("chile", "Chile"), ("peru", "Perú"), ("PERÚ", "Perú"),
    (" méxico ", "México"), ("Mexico", "México"), ("BRASIL", "Brasil"), ("colombia", "Colombia"),
])
def test_country_names_are_normalized_to_the_checkbox_value(given, canonical):
    assert config.normalize_country(given) == canonical


@pytest.mark.parametrize("value", ["Narnia", "Argentina", "Brazil", "CL", "", "  "])
def test_unknown_country_is_refused_with_the_valid_names(value):
    with pytest.raises(ValueError) as exc:
        config.normalize_country(value)
    for name in config.COUNTRIES:
        assert name in str(exc.value)


def test_normalize_countries_keeps_order_drops_blanks_and_repeats():
    assert config.normalize_countries(["peru", " ", "Chile", "Perú"]) == ["Perú", "Chile"]
    assert config.normalize_countries(None) == ["Chile"]
    with pytest.raises(ValueError):
        config.normalize_countries([" ", ""])


def test_date_filters():
    assert config.DATE_FILTERS == {
        "1d": "last_day", "3d": "last_3_days", "1sem": "last_week",
        "1mes": "last_month", "3meses": "last_3_months", "6meses": "last_6_months"}


def test_detail_url_keeps_the_slug_in_one_segment():
    assert config.detail_url("cargo-demo-1") == "https://www.takealuk.com/job_offers/cargo-demo-1"
    assert config.detail_url("a/b?c") == "https://www.takealuk.com/job_offers/a%2Fb%3Fc"
