"""config.py — env overrides, paths and constraints of spec §1.5 and §3."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from luk_cli import __version__, config
from luk_cli.config import AlgoliaConfig, load_settings
from luk_cli.errors import InvalidArgument


def test_defaults(luk_dirs):
    config_dir, cache_dir = luk_dirs
    s = load_settings({})
    assert (s.base_url, s.host) == ("https://www.takealuk.com", "www.takealuk.com")
    assert s.session_path == config_dir / "session.json"
    assert s.meta_path == config_dir / "meta.json"
    assert s.login_lock_path == config_dir / "login.lock"
    assert s.session_lock_path == config_dir / "session.lock"
    assert s.ratelimit_path == cache_dir / "ratelimit.json"
    assert s.ratelimit_lock_path == cache_dir / "ratelimit.lock"
    assert s.algolia_cache_path == cache_dir / "algolia.json"
    assert s.areas_cache_dir == cache_dir / "areas"
    assert s.captures_dir == cache_dir / "captures"
    assert s.user_agent == f"luk-cli/{__version__} (personal read-only client)"
    assert (s.login_timeout_s, s.max_req_per_hour, s.mcp_private) == (300, 240, True)
    assert s.algolia_override is None


def test_platformdirs_called_without_appauthor(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return "/tmp/x"

    monkeypatch.setattr(config, "user_config_dir", fake)
    monkeypatch.setattr(config, "user_cache_dir", fake)
    load_settings({})
    assert calls == [(("luk-cli",), {"appauthor": False})] * 2


@pytest.mark.parametrize(
    "url",
    ["http://www.takealuk.com", "https://www.takealuk.com/job_offers", "https://u:p@www.takealuk.com",
     "https://www.takealuk.com:8443", "https://www.takealuk.com?x=1", "ftp://x", "www.takealuk.com"],
)
def test_base_url_must_be_bare_https_origin(url):
    with pytest.raises(InvalidArgument):
        load_settings({"LUK_BASE_URL": url})


def test_base_url_override_sets_the_only_host():
    s = load_settings({"LUK_BASE_URL": "https://staging.takealuk.com/"})
    assert (s.base_url, s.host) == ("https://staging.takealuk.com", "staging.takealuk.com")


@pytest.mark.parametrize(
    "ua",
    ["Mozilla/5.0 luk-cli/1", "luk-cli/1 Mozilla/5.0", "luk-cli/1 Chrome/120", "luk-cli/1 Safari/1",
     "luk-cli/1 like Gecko", "curl/8", "LUK-CLI/1"],
)
def test_user_agent_override_is_constrained(ua):
    with pytest.raises(InvalidArgument):
        load_settings({"LUK_USER_AGENT": ua})


@pytest.mark.parametrize("ua", ["luk-cli/0.1 (José Ñuñoa)", "luk-cli/0.1\r\nX-Evil: 1", "luk-cli/0.1\t(me)",
                                "luk-cli/0.1 \x7f"])
def test_user_agent_override_must_be_printable_ascii(ua):
    """httpx encodes header values as ASCII when it builds a client: a value that passed here would crash
    every network command as INTERNAL while `luk doctor` stayed green. It is a bad env value (exit 1)."""
    with pytest.raises(InvalidArgument, match="printable ASCII"):
        load_settings({"LUK_USER_AGENT": ua})


def test_user_agent_override_accepted():
    assert load_settings({"LUK_USER_AGENT": "luk-cli/9 (me)"}).user_agent == "luk-cli/9 (me)"


def test_hourly_budget_can_only_be_lowered():
    assert load_settings({"LUK_MAX_REQ_PER_HOUR": "100"}).max_req_per_hour == 100
    assert load_settings({"LUK_MAX_REQ_PER_HOUR": "5000"}).max_req_per_hour == 240
    for bad in ("0", "-3", "abc"):
        with pytest.raises(InvalidArgument):
            load_settings({"LUK_MAX_REQ_PER_HOUR": bad})


def test_mcp_private_switch():
    assert load_settings({"LUK_MCP_PRIVATE": "0"}).mcp_private is False
    assert load_settings({"LUK_MCP_PRIVATE": "1"}).mcp_private is True
    with pytest.raises(InvalidArgument):
        load_settings({"LUK_MCP_PRIVATE": "yes"})


def test_login_timeout():
    assert load_settings({"LUK_LOGIN_TIMEOUT": "60"}).login_timeout_s == 60
    for bad in ("0", "x"):
        with pytest.raises(InvalidArgument):
            load_settings({"LUK_LOGIN_TIMEOUT": bad})


def test_algolia_override_needs_all_three():
    env = {"LUK_ALGOLIA_APP_ID": "TESTAPPID0", "LUK_ALGOLIA_API_KEY": "k" * 32,
           "LUK_ALGOLIA_INDEX": "JobOffer_query_suggestions"}
    assert load_settings(env).algolia_override == AlgoliaConfig("TESTAPPID0", "k" * 32, "JobOffer_query_suggestions")
    for missing in env:
        partial = {k: v for k, v in env.items() if k != missing}
        with pytest.raises(InvalidArgument, match=missing):
            load_settings(partial)


@pytest.mark.parametrize("app_id", ["testappid0", "TESTAPPID", "TESTAPPID0X", "evil.com/#", "TESTAPPID-"])
def test_algolia_app_id_must_match(app_id):
    with pytest.raises(InvalidArgument):
        AlgoliaConfig(app_id, "key", "JobOffer_query_suggestions")


def test_algolia_config_hides_key_and_builds_url():
    cfg = AlgoliaConfig("TESTAPPID0", "SECRETKEY", "JobOffer_query_suggestions")
    assert "SECRETKEY" not in repr(cfg)
    assert cfg.queries_url == "https://TESTAPPID0-dsn.algolia.net/1/indexes/*/queries"
    assert cfg.host == "testappid0-dsn.algolia.net"


def test_session_path_override_moves_meta_and_locks(tmp_path):
    s = load_settings({"LUK_SESSION_PATH": str(tmp_path / "safe" / "s.json")})
    assert s.session_path == (tmp_path / "safe" / "s.json").resolve()
    assert s.meta_path.parent == s.login_lock_path.parent == s.session_lock_path.parent == s.session_path.parent


def test_session_path_refused_inside_git_work_tree(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(InvalidArgument, match="git work tree"):
        load_settings({"LUK_SESSION_PATH": str(tmp_path / "repo" / "sub" / "session.json")})


@pytest.mark.parametrize("folder", ["OneDrive", "OneDrive - Empresa Demo", "Dropbox", "Google Drive", "My Drive"])
def test_session_path_refused_in_cloud_synced_folder(tmp_path, folder):
    with pytest.raises(InvalidArgument, match="cloud-synced"):
        load_settings({"LUK_SESSION_PATH": str(tmp_path / folder / "session.json")})


def test_session_path_refused_under_onedrive_env_root(tmp_path):
    root = tmp_path / "Synced"
    env = {"OneDrive": str(root), "LUK_SESSION_PATH": str(root / "luk" / "session.json")}
    with pytest.raises(InvalidArgument, match="cloud-synced"):
        load_settings(env)


def test_santiago_today_uses_chile_time():
    # 02:00 UTC on 23 Sep 2026 is still 22 Sep in Santiago (UTC-3).
    assert config.santiago_today(datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc)) == date(2026, 9, 22)
    assert isinstance(config.santiago_today(), date)


def test_verified_params_registry_is_a_subset_of_known_names():
    assert config.VERIFIED_PARAMS <= set(config.OPTIONAL_PARAMS)


def test_real_environment_is_read_by_default(monkeypatch):
    monkeypatch.setenv("LUK_LOGIN_TIMEOUT", "42")
    assert load_settings().login_timeout_s == 42
    assert Path(load_settings().session_path).name == "session.json"
