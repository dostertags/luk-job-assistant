"""Shared test setup (spec §8.2).

Autouse: no network (socket connect + DNS blocked except loopback, which asyncio's Windows
socketpair needs), LUK_* env cleared, and platformdirs config/cache dirs redirected to tmp_path.

Fixtures for every test module: `settings`, `fake_clock`, `limiter` (fake clock, never sleeps),
`no_limit` (never waits and has no hourly budget, for tests that walk hundreds of pages), `store`,
`session_value`, `make_state`, `saved_session` (writes a session.json, returns its LoadedSession)
and `make_client` (LukClient over an httpx.MockTransport handler; private when given a session).
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from luk_cli import config
from luk_cli.config import Settings, load_settings
from luk_cli.http import LukClient
from luk_cli.ratelimit import RateLimiter
from luk_cli.session import LoadedSession, SessionStore

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_VALUE = "a" * 40 + "--" + "b" * 40  # shape of a Rails cookie-store value; tests only
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    return host in _LOOPBACK


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    local_family = getattr(socket, "AF_UNIX", None)

    def refuse(address: object) -> RuntimeError:
        return RuntimeError(f"network access is blocked in luk-cli tests: {address!r}")

    def connect(self: socket.socket, address: object) -> None:
        if self.family != local_family and not _is_loopback(address):
            raise refuse(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        if self.family != local_family and not _is_loopback(address):
            raise refuse(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> list:
        if not _is_loopback(host):
            raise refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def luk_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(config_dir, cache_dir) under tmp_path; no LUK_* variable leaks in from the shell."""
    for key in list(os.environ):
        if key.startswith("LUK_"):
            monkeypatch.delenv(key)
    config_dir, cache_dir = tmp_path / "config", tmp_path / "cache"
    monkeypatch.setattr(config, "user_config_dir", lambda *a, **k: str(config_dir))
    monkeypatch.setattr(config, "user_cache_dir", lambda *a, **k: str(cache_dir))
    return config_dir, cache_dir


class FakeClock:
    """Thread-safe wall clock whose sleep() only advances time and records the wait."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += max(0.0, seconds)


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(settings: Settings, fake_clock: FakeClock) -> RateLimiter:
    return RateLimiter.from_settings(settings, clock=fake_clock.time, sleep=fake_clock.sleep)


class NoLimit:
    """A limiter that never waits and never counts (the fake-clock `limiter` stops at 240 per hour)."""

    def acquire(self) -> None:
        return None


@pytest.fixture
def no_limit() -> NoLimit:
    return NoLimit()


@pytest.fixture
def store(settings: Settings) -> SessionStore:
    return SessionStore(settings)


def _storage_state(value: str = SESSION_VALUE, **cookie: object) -> dict:
    base = {
        "name": "_portal_de_empleos_session",
        "value": value,
        "domain": "www.takealuk.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    return {"cookies": [{**base, **cookie}], "origins": []}


@pytest.fixture
def session_value() -> str:
    """Value of the session cookie that `make_state`/`saved_session` store by default."""
    return SESSION_VALUE


@pytest.fixture
def make_state() -> Callable[..., dict]:
    """make_state(value=…, **cookie_overrides) → Playwright storage_state with the Luk session cookie."""
    return _storage_state


@pytest.fixture
def saved_session(store: SessionStore) -> Callable[..., LoadedSession]:
    """saved_session(value=…, **cookie_overrides) → writes session.json and returns its LoadedSession."""

    def save(value: str = SESSION_VALUE, **cookie: object) -> LoadedSession:
        return store.save(_storage_state(value, **cookie))

    return save


@pytest.fixture
def make_client(settings: Settings, limiter: RateLimiter) -> Callable[..., LukClient]:
    def make(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        session: LoadedSession | None = None,
        store: SessionStore | None = None,
        verbose: bool = False,
    ) -> LukClient:
        transport = httpx.MockTransport(handler)
        opts = {"transport": transport, "verbose": verbose, "sleep": lambda s: None}
        if session is None:
            return LukClient.anonymous(settings, limiter, **opts)
        return LukClient.private(settings, limiter, session, store=store, **opts)

    return make
