"""Settings, paths and constants — the one place for them (spec §1.5, §3, §4.9).

Paths come from platformdirs with `appauthor=False` (Windows: `%LOCALAPPDATA%\\luk-cli` and
`…\\luk-cli\\Cache`). Env overrides are validated here; a bad value is an `InvalidArgument` (exit 1).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from platformdirs import user_cache_dir, user_config_dir

from luk_cli import __version__
from luk_cli.errors import InvalidArgument

APP_NAME = "luk-cli"
DEFAULT_BASE_URL = "https://www.takealuk.com"
SESSION_COOKIE = "_portal_de_empleos_session"
REMEMBER_COOKIE = "remember_user_token"
SIGN_IN_PATH = "/users/sign_in"
SANTIAGO_TZ = "America/Santiago"
CHILE_AREA_ID = 1021

MIN_REQUEST_INTERVAL_S = 1.5  # takealuk.com, across all luk processes and threads (§1.5)
MAX_REQ_PER_HOUR = 240  # rolling hour; LUK_MAX_REQ_PER_HOUR may only lower it
ALGOLIA_MIN_INTERVAL_S = 0.2  # ≤5 req/s per process
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER_S = 60.0
CACHE_TTL_S = 24 * 3600  # algolia.json and areas/
DEFAULT_LOGIN_TIMEOUT_S = 300
DEFAULT_ALGOLIA_INDEX = "JobOffer_query_suggestions"

OptionalParam = Literal[
    "job_types", "posted_within", "salary", "countries", "worldwide", "companies_location", "areas_companies"
]
OPTIONAL_PARAMS: tuple[OptionalParam, ...] = (
    "job_types", "posted_within", "salary", "countries", "worldwide", "companies_location", "areas_companies",
)
# Phase-0 registry (§2.2, §10): a param is registered in the CLI and the MCP schema only after its
# live check passed and was logged in docs/endpoints.md; unverified params are absent, not hidden.
# Phase 0 of 2026-09-23 (docs/endpoints.md §4) passed all of them; `countries[]` only when sent
# together with `worldwide=1` (without it the server ANDs it with the geo-IP default, Chile).
VERIFIED_PARAMS: frozenset[OptionalParam] = frozenset(
    {"job_types", "posted_within", "salary", "countries", "worldwide", "companies_location", "areas_companies"}
)

_BANNED_UA_TOKENS = ("mozilla/", "chrome/", "safari/", "gecko")
_ALGOLIA_APP_ID_RE = re.compile(r"[A-Z0-9]{10}")
_CLOUD_DIR_NAMES = frozenset({"dropbox", "google drive", "googledrive", "my drive"})
_ONEDRIVE_ENV = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


@dataclass(frozen=True)
class AlgoliaConfig:
    """Public query-suggestion config; `app_id` must match ^[A-Z0-9]{10}$ (blocks host injection)."""

    app_id: str
    api_key: str = field(repr=False)
    index: str = DEFAULT_ALGOLIA_INDEX

    def __post_init__(self) -> None:
        if not _ALGOLIA_APP_ID_RE.fullmatch(self.app_id):
            raise InvalidArgument("invalid Algolia application id (expected 10 characters A-Z/0-9)")
        if not self.api_key or not self.index:
            raise InvalidArgument("Algolia API key and index must be non-empty")

    @property
    def host(self) -> str:
        return f"{self.app_id.lower()}-dsn.algolia.net"

    @property
    def queries_url(self) -> str:
        return f"https://{self.app_id}-dsn.algolia.net/1/indexes/*/queries"


@dataclass(frozen=True)
class Settings:
    base_url: str
    config_dir: Path
    cache_dir: Path
    session_path: Path
    login_timeout_s: int
    user_agent: str
    max_req_per_hour: int
    mcp_private: bool
    algolia_override: AlgoliaConfig | None  # all three LUK_ALGOLIA_* set → discovery skipped

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def meta_path(self) -> Path:
        return self.session_path.with_name("meta.json")

    @property
    def login_lock_path(self) -> Path:
        return self.session_path.with_name("login.lock")

    @property
    def session_lock_path(self) -> Path:
        return self.session_path.with_name("session.lock")

    @property
    def ratelimit_path(self) -> Path:
        return self.cache_dir / "ratelimit.json"

    @property
    def ratelimit_lock_path(self) -> Path:
        return self.cache_dir / "ratelimit.lock"

    @property
    def algolia_cache_path(self) -> Path:
        return self.cache_dir / "algolia.json"

    @property
    def areas_cache_dir(self) -> Path:
        return self.cache_dir / "areas"

    @property
    def captures_dir(self) -> Path:
        return self.cache_dir / "captures"


def default_user_agent() -> str:
    return f"luk-cli/{__version__} (personal read-only client)"


def santiago_today(now: datetime | None = None) -> date:
    """Today's date in America/Santiago (dates in §5.2/§5.4); `now` must be timezone-aware."""
    tz = ZoneInfo(SANTIAGO_TZ)
    return (now.astimezone(tz) if now else datetime.now(tz)).date()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate the LUK_* overrides of §3 (default: `os.environ`)."""
    env = os.environ if env is None else env

    def get(name: str) -> str | None:
        value = env.get(name, "").strip()
        return value or None

    config_dir = Path(user_config_dir(APP_NAME, appauthor=False))
    cache_dir = Path(user_cache_dir(APP_NAME, appauthor=False))
    session_override = get("LUK_SESSION_PATH")
    if session_override:
        session_path = Path(session_override).expanduser().resolve()
        _refuse_unsafe_session_path(session_path, env)
    else:
        session_path = config_dir / "session.json"
    return Settings(
        base_url=_base_url(get("LUK_BASE_URL") or DEFAULT_BASE_URL),
        config_dir=config_dir,
        cache_dir=cache_dir,
        session_path=session_path,
        login_timeout_s=_positive_int("LUK_LOGIN_TIMEOUT", get("LUK_LOGIN_TIMEOUT"), DEFAULT_LOGIN_TIMEOUT_S),
        user_agent=_user_agent(get("LUK_USER_AGENT")),
        max_req_per_hour=min(
            MAX_REQ_PER_HOUR, _positive_int("LUK_MAX_REQ_PER_HOUR", get("LUK_MAX_REQ_PER_HOUR"), MAX_REQ_PER_HOUR)
        ),
        mcp_private=_switch("LUK_MCP_PRIVATE", get("LUK_MCP_PRIVATE")),
        algolia_override=_algolia_override(
            get("LUK_ALGOLIA_APP_ID"), get("LUK_ALGOLIA_API_KEY"), get("LUK_ALGOLIA_INDEX")
        ),
    )


def _base_url(value: str) -> str:
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or port is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise InvalidArgument("LUK_BASE_URL must be https://<host> with no port, path, query or credentials")
    return f"https://{parts.hostname}"


def _user_agent(value: str | None) -> str:
    if value is None:
        return default_user_agent()
    lowered = value.lower()
    if not value.startswith("luk-cli/") or any(token in lowered for token in _BANNED_UA_TOKENS):
        raise InvalidArgument(
            "LUK_USER_AGENT must start with 'luk-cli/' and must not imitate a browser "
            "(Mozilla/, Chrome/, Safari/, Gecko)"
        )
    if not (value.isascii() and value.isprintable()):  # httpx sends header values as ASCII
        raise InvalidArgument("LUK_USER_AGENT must be printable ASCII (no accents, tabs or line breaks)")
    return value


def _positive_int(name: str, value: str | None, default: int) -> int:
    if value is None:
        return default
    if not value.isdigit() or int(value) < 1:
        raise InvalidArgument(f"{name} must be a positive integer")
    return int(value)


def _switch(name: str, value: str | None) -> bool:
    if value is None or value == "1":
        return True
    if value == "0":
        return False
    raise InvalidArgument(f"{name} must be 0 or 1")


def _algolia_override(app_id: str | None, api_key: str | None, index: str | None) -> AlgoliaConfig | None:
    if app_id and api_key and index:
        return AlgoliaConfig(app_id, api_key, index)
    values = {"LUK_ALGOLIA_APP_ID": app_id, "LUK_ALGOLIA_API_KEY": api_key, "LUK_ALGOLIA_INDEX": index}
    missing = [name for name, value in values.items() if value is None]
    if len(missing) < len(values):
        raise InvalidArgument(f"set all three LUK_ALGOLIA_* variables or none (missing: {', '.join(missing)})")
    return None


def _refuse_unsafe_session_path(path: Path, env: Mapping[str, str]) -> None:
    """LUK_SESSION_PATH is a bearer credential: never inside a git work tree or a synced folder."""
    folders = [part.casefold() for part in path.parent.parts]
    synced = any(part.startswith("onedrive") or part in _CLOUD_DIR_NAMES for part in folders)
    for var in _ONEDRIVE_ENV:
        root = env.get(var)
        if root and path.is_relative_to(Path(root).expanduser().resolve()):
            synced = True
    if synced:
        raise InvalidArgument(f"LUK_SESSION_PATH is inside a cloud-synced folder: {path.parent}")
    for parent in path.parents:
        if (parent / ".git").exists():
            raise InvalidArgument(f"LUK_SESSION_PATH is inside a git work tree: {parent}")
