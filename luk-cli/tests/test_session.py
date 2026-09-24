"""session.py — filtering, atomic writes, CAS write-back, reload and locks (spec §4.1, §4.5, §4.8)."""

import json
import os
import pathlib
import stat
import sys
import threading
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

import pytest
from filelock import FileLock

from luk_cli import session as s
from luk_cli.errors import AuthRequired, InternalError, SessionBusy
from luk_cli.models import SessionMeta, StorageState
from luk_cli.session import SessionStore

SESSION_VALUE = "s" * 64


def storage_state(value=SESSION_VALUE, **kw):
    return {"cookies": [cookie("_portal_de_empleos_session", "www.takealuk.com", value, httpOnly=True, **kw)],
            "origins": []}


def cookie(name, domain, value="v" * 20, **kw):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1,
            "httpOnly": False, "secure": True, "sameSite": "Lax", **kw}


MIXED = {
    "cookies": [
        cookie("_portal_de_empleos_session", "www.takealuk.com", SESSION_VALUE, httpOnly=True),
        cookie("remember_user_token", ".takealuk.com", expires=1_900_000_000.5),
        cookie("evil", "eviltakealuk.com"),
        cookie("evil2", ".takealuk.com.evil.com"),
        cookie("other_sub", "api.takealuk.com"),
        cookie("SID", ".google.com"),
        cookie("li_at", ".www.linkedin.com"),
        cookie("_ga", ".takealuk.com"),
        cookie("_ga_ABC123", ".takealuk.com"),
        cookie("_gid", ".takealuk.com"),
        cookie("_gcl_au", ".takealuk.com"),
        cookie("g_state", "www.takealuk.com"),
        cookie("AMP_1234", ".takealuk.com"),
        cookie("amplitude_idtakealuk", ".takealuk.com"),
    ],
    "origins": [
        {"origin": "https://www.takealuk.com", "localStorage": [{"name": "k", "value": "v"}]},
        {"origin": "https://accounts.google.com", "localStorage": []},
        {"origin": "https://eviltakealuk.com", "localStorage": []},
    ],
}


def test_filter_keeps_only_luk_session_cookies(settings):
    state = s.filter_storage_state(MIXED, settings.host)
    assert [c.name for c in state.cookies] == ["_portal_de_empleos_session", "remember_user_token"]
    assert [o.origin for o in state.origins] == ["https://www.takealuk.com"]


@pytest.mark.parametrize(
    ("domain", "ok"),
    [("takealuk.com", True), (".takealuk.com", True), ("www.takealuk.com", True), ("eviltakealuk.com", False),
     ("takealuk.com.evil.com", False), (".google.com", False), ("WWW.TAKEALUK.COM", True)],
)
def test_luk_domain_predicate(domain, ok):
    assert s.is_luk_domain(domain) is ok


def test_save_filters_before_writing_and_round_trips(store, settings):
    loaded = store.save(MIXED)
    on_disk = json.loads(settings.session_path.read_text("utf-8"))
    assert [c["name"] for c in on_disk["cookies"]] == ["_portal_de_empleos_session", "remember_user_token"]
    assert on_disk["cookies"][0]["value"] == SESSION_VALUE  # Playwright storage_state shape
    again = store.load()
    assert again == loaded and again.state.cookies[0].value.get_secret_value() == SESSION_VALUE
    assert SESSION_VALUE not in repr(loaded) and SESSION_VALUE not in repr(store)


def test_load_missing_is_none_and_corrupt_is_auth_required(store, settings):
    assert store.load() is None
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text("{not json", "utf-8")
    with pytest.raises(AuthRequired):
        store.load()


META = SessionMeta(logged_in_at="2026-09-23T12:00:00Z", browser="chromium", luk_cli_version="0.1.0", name="Ana")


def busy_open(monkeypatch, paths, failures):
    """`open()` inside luk_cli.session raises a Windows sharing violation `failures` times per path."""
    real_open = open
    left = {str(p): failures for p in paths}

    def fake(file, *args, **kwargs):
        if left.get(str(file), 0) > 0:
            left[str(file)] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another "
                                      "process", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(s, "open", fake, raising=False)
    monkeypatch.setattr(s, "WRITE_RETRY_DELAY_S", 0.0)
    return left


def test_readers_retry_a_sharing_violation(store, settings, monkeypatch):
    """§4.8 readers never lock, so a writer's os.replace or an antivirus scan can hold session.json or
    meta.json for a moment on Windows: every reader retries like the writers (10 × 50 ms)."""
    loaded = store.save(storage_state(), META)
    busy_open(monkeypatch, [settings.session_path, settings.meta_path], failures=3)
    assert store.load() == loaded and store.load_meta() == META
    busy_open(monkeypatch, [settings.session_path], failures=3)
    assert store.reload(None) == (loaded, True)


def test_a_persistent_sharing_violation_is_a_luk_error_without_the_path(store, settings, monkeypatch):
    store.save(storage_state(), META)
    busy_open(monkeypatch, [settings.session_path, settings.meta_path], failures=10**6)
    for read in (store.load, store.load_meta, lambda: store.reload(None)):
        with pytest.raises(InternalError, match="in use by another program; retry") as info:
            read()
        assert str(settings.session_path.parent) not in info.value.message and "Errno" not in info.value.message


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing violations")
def test_a_session_file_held_without_sharing_is_retried(store, settings):
    """A real share-mode-0 handle (what a replace in progress or a scanner holds) on session.json."""
    import ctypes
    from ctypes import wintypes

    loaded = store.save(storage_state())
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(settings.session_path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ,
    assert handle not in (None, wintypes.HANDLE(-1).value)  # no sharing, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    released = threading.Event()
    try:
        with pytest.raises(InternalError, match="in use by another program"):
            store.load()
        threading.Timer(0.1, lambda: (kernel32.CloseHandle(handle), released.set())).start()
        assert store.load() == loaded  # the handle went away while the reader was retrying
    finally:
        if not released.wait(5):
            kernel32.CloseHandle(handle)


def test_save_with_meta_writes_both(store):
    meta = SessionMeta(logged_in_at="2026-09-23T12:00:00Z", browser="chromium", luk_cli_version="0.1.0", name="Ana")
    store.save(storage_state(), meta)
    assert store.load_meta() == meta


def test_update_meta_and_touch(store):
    store.save(storage_state())
    assert store.update_meta(name="x") is None  # no meta.json → nothing to update
    store.save(storage_state(), SessionMeta(logged_in_at="2026-09-23T12:00:00Z", browser="msedge", luk_cli_version="0"))
    store.touch_auth_ok()
    assert store.load_meta().last_auth_ok_at is not None
    assert store.update_meta(email="a@example.com").email == "a@example.com"


def test_atomic_write_retries_permission_errors(tmp_path):
    calls, sleeps = [], []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) < 4:
            raise PermissionError("sharing violation")
        os.replace(src, dst)

    target = tmp_path / "session.json"
    s.atomic_write(target, b"{}", replace=flaky_replace, sleep=sleeps.append)
    assert target.read_bytes() == b"{}" and len(calls) == 4 and sleeps == [0.05] * 3
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_gives_up_after_ten_attempts_and_cleans_up(tmp_path):
    def always_busy(src, dst):
        raise PermissionError("sharing violation")

    sleeps = []
    with pytest.raises(InternalError):
        s.atomic_write(tmp_path / "session.json", b"{}", replace=always_busy, sleep=sleeps.append)
    assert len(sleeps) == 9 and list(tmp_path.iterdir()) == []


def test_atomic_write_temp_file_naming(tmp_path, monkeypatch):
    seen = []
    real_mkstemp = s.tempfile.mkstemp

    def spy(**kwargs):
        seen.append(kwargs)
        return real_mkstemp(**kwargs)

    monkeypatch.setattr(s.tempfile, "mkstemp", spy)
    s.atomic_write(tmp_path / "session.json", b"{}")
    assert seen == [{"dir": tmp_path, "prefix": ".session-", "suffix": ".tmp"}]


def test_write_back_merges_stored_cookies_only(store):
    loaded = store.save(storage_state())
    jar = s.build_cookiejar(loaded.state)
    for c in jar:
        c.value = "R" * 40  # the server rotated the session cookie
    jar.set_cookie(_jar_cookie("unrelated", "x" * 20))
    updates = s.cookie_updates(jar, loaded.state)
    assert [(u.name, u.value.get_secret_value()) for u in updates] == [("_portal_de_empleos_session", "R" * 40)]
    new = store.write_back(loaded, updates)
    assert new is not None and new.sha256 != loaded.sha256
    assert [c.name for c in store.load().state.cookies] == ["_portal_de_empleos_session"]
    assert store.load().state.cookies[0].value.get_secret_value() == "R" * 40


def test_cookie_updates_ignore_unchanged_cookies_with_fractional_expiry(store):
    loaded = store.save(storage_state(expires=1_900_000_000.75))
    assert s.cookie_updates(s.build_cookiejar(loaded.state), loaded.state) == []


def test_cas_rejects_write_back_after_logout(store, settings):
    loaded = store.save(storage_state())
    store.delete()
    assert store.write_back(loaded, loaded.state.cookies) is None
    assert not settings.session_path.exists()


def test_cas_rejects_write_back_after_a_new_login(store):
    old = store.save(storage_state("O" * 40))
    store.save(storage_state("N" * 40))
    rotated = old.state.cookies[0].model_copy(update={"value": s.SecretStr("X" * 40)})
    assert store.write_back(old, [rotated]) is None
    assert store.load().state.cookies[0].value.get_secret_value() == "N" * 40


def test_reload_detects_changes(store, settings):
    assert store.reload(None) == (None, False)
    first = store.save(storage_state("A" * 40))
    assert store.reload(first) == (first, False)
    same_content = first.state
    os.utime(settings.session_path, ns=(first.mtime_ns + 10**9, first.mtime_ns + 10**9))
    touched, changed = store.reload(first)
    assert changed is False and touched.state == same_content and touched.mtime_ns != first.mtime_ns
    second = store.save(storage_state("B" * 40))
    assert store.reload(touched) == (second, True)
    store.delete()
    assert store.reload(second) == (None, True)
    assert store.reload(None)[0] is None
    third = store.save(storage_state("C" * 40))
    assert store.reload(None) == (third, True)


def test_login_lock_contention(settings):
    first, second = SessionStore(settings), SessionStore(settings)
    with first.login_lock("login already in progress"):
        with pytest.raises(SessionBusy, match="login already in progress"):
            with second.login_lock("login already in progress"):
                pass
        with pytest.raises(SessionBusy, match="close the login window first"):
            second.delete()
    assert second.delete() is False  # idempotent once the login finished


def test_session_lock_contention_times_out(settings):
    busy = FileLock(str(settings.session_lock_path))
    settings.session_lock_path.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(settings, lock_timeout_s=0.05)
    with busy:
        with pytest.raises(SessionBusy):
            store.save(storage_state())
        store.touch_auth_ok()  # best-effort: never raises


def test_logout_never_deletes_lock_files(store, settings, monkeypatch):
    """§4.8: whatever call luk_cli uses (Path.unlink, os.unlink, os.remove), logout removes session.json and
    meta.json only. filelock itself may drop a lock file on release (3.29 does), so only deletions requested
    from luk_cli code count."""
    store.save(storage_state(), META)
    deleted = []

    def spy(real):
        def unlink(path, *args, **kwargs):
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller.split(".")[0] == "luk_cli":
                deleted.append(Path(os.fspath(path)))
            return real(path, *args, **kwargs)
        return unlink

    monkeypatch.setattr(pathlib.Path, "unlink", spy(pathlib.Path.unlink))
    monkeypatch.setattr(os, "unlink", spy(os.unlink))
    monkeypatch.setattr(os, "remove", spy(os.remove))
    assert store.delete() is True
    assert sorted(deleted) == sorted([settings.session_path, settings.meta_path])
    assert not settings.session_path.exists() and not settings.meta_path.exists()


def test_the_lock_spy_sees_a_lock_deleted_from_luk_cli(store, settings, monkeypatch):
    """The spy above is not blind: a lock file unlinked by luk_cli code after release is caught."""
    store.save(storage_state())
    real_delete = SessionStore.delete

    def deleting_locks(self):
        existed = real_delete(self)
        for lock in (settings.login_lock_path, settings.session_lock_path):
            s._unlink_with_retry(Path(lock))  # what the §4.8 rule forbids
        return existed

    monkeypatch.setattr(SessionStore, "delete", deleting_locks)
    with pytest.raises(AssertionError):
        test_logout_never_deletes_lock_files(store, settings, monkeypatch)


def test_build_cookiejar_preserves_attributes(settings):
    state = s.filter_storage_state(MIXED, settings.host)
    jar = {c.name: c for c in s.build_cookiejar(state)}
    session_cookie, remember = jar["_portal_de_empleos_session"], jar["remember_user_token"]
    assert session_cookie.secure and session_cookie.expires is None and session_cookie.discard
    assert session_cookie.domain == "www.takealuk.com" and not session_cookie.domain_specified
    assert session_cookie.has_nonstandard_attr("HttpOnly")
    assert remember.domain == ".takealuk.com" and remember.domain_specified and remember.domain_initial_dot
    assert remember.expires == 1_900_000_000 and not remember.has_nonstandard_attr("HttpOnly")


def test_describe_cookies_never_includes_values(store):
    loaded = store.save(MIXED)
    infos = s.describe_cookies(loaded.state)
    assert [i.model_dump() for i in infos] == [
        {"name": "_portal_de_empleos_session", "domain": "www.takealuk.com", "expires": "session"},
        {"name": "remember_user_token", "domain": ".takealuk.com", "expires": "2030-03-17T17:46:40Z"},
    ]
    assert SESSION_VALUE not in repr(infos)


def test_in_memory_session_for_probes():
    loaded = s.LoadedSession.from_state(StorageState.model_validate(storage_state()))
    assert loaded.mtime_ns == 0 and len(loaded.sha256) == 64


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions; Windows relies on the %LOCALAPPDATA% ACL")
def test_posix_permissions(store, settings, caplog):
    store.save(storage_state())
    assert stat.S_IMODE(settings.session_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.session_path.stat().st_mode) == 0o600
    os.chmod(settings.session_path, 0o644)
    store.load()
    assert stat.S_IMODE(settings.session_path.stat().st_mode) == 0o600
    assert "0600" in caplog.text


def _jar_cookie(name, value):
    return Cookie(0, name, value, None, False, "www.takealuk.com", False, False, "/", True, True, None, True,
                  None, None, {}, False)


def test_jar_helper_is_a_cookiejar():
    jar = CookieJar()
    jar.set_cookie(_jar_cookie("a", "b"))
    assert len(jar) == 1
