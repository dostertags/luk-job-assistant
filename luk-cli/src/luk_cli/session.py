"""The stored Luk session: filter, atomic writes, CAS write-back, meta, locks, jar, reload.

Spec §4.1 (storage + jar), §4.5 (reload), §4.8 (concurrency):
- session.json is a *filtered* Playwright storage_state: only takealuk.com cookies that apply to the
  LUK_BASE_URL host, minus analytics. It is a bearer credential; values stay `SecretStr`.
- Two filelocks beside it: `login.lock` for a whole interactive login (timeout 0) and
  `session.lock` around short read-modify-write sections (timeout 5 s). Readers never lock.
  Lock files are never deleted by this code (filelock owns them).
- Writes are mkstemp → fsync → os.replace, retried 10× at 50 ms on PermissionError (Windows
  sharing violations). Cookie write-back is compare-and-swap on the file's SHA-256.
- Reads (open-read-close, stat) retry the same way: a writer's os.replace in another luk process (or
  an antivirus scan) can hold the file for a moment on Windows. A persistent failure is an
  InternalError that names the file, never the raw OSError or its absolute path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from filelock import FileLock, Timeout
from pydantic import SecretStr, ValidationError

from luk_cli.config import Settings
from luk_cli.errors import AuthRequired, InternalError, SessionBusy
from luk_cli.models import CookieInfo, SessionMeta, StorageState, StoredCookie
from luk_cli.redact import register_secret

log = logging.getLogger("luk_cli.session")

SESSION_LOCK_TIMEOUT_S = 5.0
WRITE_ATTEMPTS = 10
WRITE_RETRY_DELAY_S = 0.05
_ANALYTICS_NAMES = frozenset({"_ga", "_gid", "g_state"})
_ANALYTICS_PREFIXES = ("_ga_", "_gcl_", "AMP_", "amplitude")
_POSIX = os.name != "nt"
T = TypeVar("T")


def is_luk_domain(domain: str) -> bool:
    """takealuk.com or a subdomain; rejects look-alikes such as eviltakealuk.com."""
    d = domain.lstrip(".").lower()
    return d == "takealuk.com" or d.endswith(".takealuk.com")


def is_analytics_cookie(name: str) -> bool:
    return name in _ANALYTICS_NAMES or name.startswith(_ANALYTICS_PREFIXES)


def _applies_to_host(domain: str, host: str) -> bool:
    d = domain.lstrip(".").lower()
    return host == d or host.endswith("." + d)


def filter_storage_state(raw: StorageState | Mapping[str, Any], host: str) -> StorageState:
    """Keep Luk cookies/origins that apply to `host` (the LUK_BASE_URL host), minus analytics."""
    state = raw if isinstance(raw, StorageState) else StorageState.model_validate(raw)
    cookies = [
        c for c in state.cookies
        if is_luk_domain(c.domain) and _applies_to_host(c.domain, host) and not is_analytics_cookie(c.name)
    ]
    origins = [o for o in state.origins if _luk_origin(o.origin, host)]
    return StorageState(cookies=cookies, origins=origins)


def _luk_origin(origin: str, host: str) -> bool:
    hostname = urlsplit(origin).hostname or ""
    return is_luk_domain(hostname) and _applies_to_host(hostname, host)


@dataclass(frozen=True)
class LoadedSession:
    """A session as read (or written) by this process; `sha256` is the CAS token."""

    state: StorageState = field(repr=False)
    sha256: str
    mtime_ns: int
    size: int

    @classmethod
    def from_state(cls, state: StorageState) -> LoadedSession:
        """An in-memory session with no file behind it (e.g. the --paste-cookie probe)."""
        data = _serialize(state)
        _register(state)
        return cls(state, hashlib.sha256(data).hexdigest(), 0, len(data))


def _serialize(state: StorageState) -> bytes:
    return json.dumps(state.to_playwright(), indent=2, ensure_ascii=False).encode("utf-8")


def _register(state: StorageState) -> None:
    for c in state.cookies:
        register_secret(c.value.get_secret_value())


def _jar_expires(expires: float) -> int | None:
    return None if expires < 0 else int(expires)


def build_cookiejar(state: StorageState) -> CookieJar:
    """A jar that keeps secure, expiry, host-only vs .domain, path and HttpOnly exactly as stored."""
    jar = CookieJar()
    for c in state.cookies:
        dotted = c.domain.startswith(".")
        expires = _jar_expires(c.expires)
        jar.set_cookie(
            Cookie(
                version=0, name=c.name, value=c.value.get_secret_value(), port=None, port_specified=False,
                domain=c.domain, domain_specified=dotted, domain_initial_dot=dotted,
                path=c.path, path_specified=True, secure=c.secure, expires=expires, discard=expires is None,
                comment=None, comment_url=None, rfc2109=False,
                # a bare flag, stored as None exactly like the stdlib's own Set-Cookie parsing (typeshed says str)
                rest={"HttpOnly": None} if c.http_only else {},  # type: ignore[dict-item]
            )
        )
    return jar


def cookie_updates(jar: CookieJar, state: StorageState) -> list[StoredCookie]:
    """Stored cookies (same name, domain, path) whose value or expiry the jar now holds differently."""
    stored = {c.key: c for c in state.cookies}
    updates: list[StoredCookie] = []
    for jc in jar:
        old = stored.get((jc.name, jc.domain, jc.path))
        if old is None or jc.value is None:
            continue
        if jc.value != old.value.get_secret_value() or jc.expires != _jar_expires(old.expires):
            expires = float(jc.expires) if jc.expires is not None else -1.0
            updates.append(old.model_copy(update={"value": SecretStr(jc.value), "expires": expires}))
    return updates


def describe_cookies(state: StorageState) -> list[CookieInfo]:
    """Name, domain and expiry ('session' or ISO-8601 UTC) of each cookie — never its value."""
    return [
        CookieInfo(
            name=c.name,
            domain=c.domain,
            expires="session" if c.expires < 0
            else datetime.fromtimestamp(int(c.expires), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for c in state.cookies
    ]


def _retry_permission(
    action: Callable[[], T], *, what: str, sleep: Callable[[float], None] | None = None
) -> T:
    """`action()`, retried WRITE_ATTEMPTS× at WRITE_RETRY_DELAY_S on PermissionError (a Windows sharing
    violation); then InternalError naming `what` only (no OSError text, no absolute path)."""
    for _ in range(WRITE_ATTEMPTS - 1):
        try:
            return action()
        except PermissionError:
            (sleep or time.sleep)(WRITE_RETRY_DELAY_S)
    try:
        return action()
    except PermissionError:
        raise InternalError(f"could not {what}: the file is in use by another program; retry") from None


def _read_file(path: Path) -> tuple[bytes, os.stat_result]:
    """Open-read-close without a lock (§4.8 readers), retried on sharing violations; FileNotFoundError
    passes through."""

    def read() -> tuple[bytes, os.stat_result]:
        with open(path, "rb") as fh:
            return fh.read(), os.fstat(fh.fileno())

    return _retry_permission(read, what=f"read {path.name}")


def _stat(path: Path) -> os.stat_result:
    return _retry_permission(lambda: os.stat(path), what=f"read {path.name}")


def atomic_write(
    path: Path,
    data: bytes,
    *,
    replace: Callable[[str, str], None] = os.replace,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """mkstemp beside `path` → write → fsync → os.replace (retried); the temp file never survives."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _retry_permission(lambda: replace(tmp, str(path)), what=f"write {path.name}", sleep=sleep)
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def _unlink_with_retry(path: Path) -> None:
    def unlink() -> None:
        with suppress(FileNotFoundError):
            path.unlink()

    _retry_permission(unlink, what=f"delete {path.name}")


class SessionStore:
    """session.json + meta.json + their two locks, all beside `settings.session_path`."""

    def __init__(self, settings: Settings, *, lock_timeout_s: float = SESSION_LOCK_TIMEOUT_S) -> None:
        self.path = settings.session_path
        self.meta_path = settings.meta_path
        self.host = settings.host
        self._login_lock = FileLock(str(settings.login_lock_path), timeout=0)
        self._session_lock = FileLock(str(settings.session_lock_path), timeout=lock_timeout_s)

    def __repr__(self) -> str:
        return f"SessionStore(path={str(self.path)!r})"

    # -- locks ---------------------------------------------------------------------------------
    def _ensure_dir(self) -> None:
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            if _POSIX:
                os.chmod(directory, 0o700)

    @contextmanager
    def login_lock(self, busy_message: str) -> Iterator[None]:
        """Held for a whole interactive login/refresh; `SessionBusy(busy_message)` if already held."""
        self._ensure_dir()
        try:
            self._login_lock.acquire()
        except Timeout:
            raise SessionBusy(busy_message) from None
        try:
            yield
        finally:
            self._login_lock.release()

    @contextmanager
    def _writing(self) -> Iterator[None]:
        self._ensure_dir()
        try:
            self._session_lock.acquire()
        except Timeout:
            raise SessionBusy("the session file is busy in another luk process; retry") from None
        try:
            yield
        finally:
            self._session_lock.release()

    # -- session.json --------------------------------------------------------------------------
    def exists(self) -> bool:
        try:
            _stat(self.path)
        except OSError:  # missing or unusable, as Path.exists() reports it; a lasting sharing violation raises
            return False
        return True

    def load(self) -> LoadedSession | None:
        """Open-read-close without locking; None if absent, AuthRequired if unreadable."""
        try:
            data, st = _read_file(self.path)
        except FileNotFoundError:
            return None
        if _POSIX and st.st_mode & 0o077:
            os.chmod(self.path, 0o600)
            log.warning("%s was readable by other users; permissions reset to 0600", self.path)
        try:
            state = filter_storage_state(json.loads(data), self.host)
        except (ValueError, ValidationError):
            raise AuthRequired("session.json is unreadable. Run `luk login` again.") from None
        _register(state)
        return LoadedSession(state, hashlib.sha256(data).hexdigest(), st.st_mtime_ns, st.st_size)

    def reload(self, current: LoadedSession | None) -> tuple[LoadedSession | None, bool]:
        """§4.5: (latest, changed). mtime+size first, SHA-256 only when either moved."""
        try:
            st = _stat(self.path)
        except FileNotFoundError:
            return None, current is not None
        if current is not None and (st.st_mtime_ns, st.st_size) == (current.mtime_ns, current.size):
            return current, False
        latest = self.load()
        if latest is None:
            return None, current is not None
        return latest, current is None or latest.sha256 != current.sha256

    def save(self, state: StorageState | Mapping[str, Any], meta: SessionMeta | None = None) -> LoadedSession:
        """Filter, then write session.json (and meta.json) atomically under session.lock."""
        filtered = filter_storage_state(state, self.host)
        data = _serialize(filtered)
        with self._writing():
            atomic_write(self.path, data)
            if meta is not None:
                atomic_write(self.meta_path, meta.model_dump_json(indent=2).encode("utf-8"))
            st = os.stat(self.path)
        _register(filtered)
        return LoadedSession(filtered, hashlib.sha256(data).hexdigest(), st.st_mtime_ns, st.st_size)

    def write_back(self, loaded: LoadedSession, updates: Sequence[StoredCookie]) -> LoadedSession | None:
        """Merge rotated cookies iff session.json is still what `loaded` read (CAS); else None.

        Raises SessionBusy when session.lock cannot be taken within its timeout.
        """
        with self._writing():
            try:
                current, _ = _read_file(self.path)
            except FileNotFoundError:
                return None
            if hashlib.sha256(current).hexdigest() != loaded.sha256:
                return None
            merged = {c.key: c for c in loaded.state.cookies}
            for c in updates:
                if c.key in merged:
                    merged[c.key] = c
            state = filter_storage_state(
                StorageState(cookies=list(merged.values()), origins=loaded.state.origins), self.host
            )
            data = _serialize(state)
            atomic_write(self.path, data)
            st = os.stat(self.path)
        _register(state)
        return LoadedSession(state, hashlib.sha256(data).hexdigest(), st.st_mtime_ns, st.st_size)

    def delete(self) -> bool:
        """`luk logout`: login.lock (busy → 'close the login window first'), then remove session + meta.

        Local-only and idempotent; returns whether a session existed. Lock files are left alone.
        """
        with self.login_lock("close the login window first"), self._writing():
            existed = self.path.exists()
            _unlink_with_retry(self.path)
            _unlink_with_retry(self.meta_path)
        return existed

    # -- meta.json -----------------------------------------------------------------------------
    def load_meta(self) -> SessionMeta | None:
        try:
            data, _ = _read_file(self.meta_path)
        except FileNotFoundError:
            return None
        try:
            return SessionMeta.model_validate_json(data)
        except ValidationError:
            log.warning("meta.json is unreadable; ignoring it")
            return None

    def update_meta(self, **changes: Any) -> SessionMeta | None:
        """Validate-and-merge `changes` into meta.json under session.lock; None when there is no meta."""
        with self._writing():
            meta = self.load_meta()
            if meta is None:
                return None
            meta = SessionMeta.model_validate({**meta.model_dump(), **changes})
            atomic_write(self.meta_path, meta.model_dump_json(indent=2).encode("utf-8"))
        return meta

    def touch_auth_ok(self) -> None:
        """Best-effort `last_auth_ok_at = now` after a non-redirected private 200 (§4.1)."""
        try:
            self.update_meta(last_auth_ok_at=datetime.now(timezone.utc))
        except SessionBusy:
            log.debug("meta.json busy; last_auth_ok_at not updated")
