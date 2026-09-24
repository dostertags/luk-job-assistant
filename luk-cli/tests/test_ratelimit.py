"""ratelimit.py — cross-process spacing, hourly budget, injectable clock/sleep (spec §1.5, §4.9)."""

import json
import threading
from itertools import pairwise

import pytest
from filelock import FileLock

from luk_cli.config import load_settings
from luk_cli.errors import BudgetExceeded, RateLimited
from luk_cli.ratelimit import IntervalLimiter, RateLimiter


def make(settings, clock, **kw):
    return RateLimiter.from_settings(settings, clock=clock.time, sleep=clock.sleep, **kw)


def recent(settings):
    return json.loads(settings.ratelimit_path.read_text("utf-8"))["recent"]


def test_spacing_within_one_instance(settings, fake_clock):
    limiter = make(settings, fake_clock)
    start = fake_clock.now
    limiter.acquire()
    limiter.acquire()
    assert fake_clock.sleeps == [1.5]
    assert recent(settings) == [start, start + 1.5]
    state = json.loads(settings.ratelimit_path.read_text("utf-8"))
    assert state["next_allowed_at"] == start + 3.0


def test_spacing_across_instances_sharing_the_cache_dir(settings, fake_clock):
    first, second = make(settings, fake_clock), make(settings, fake_clock)
    first.acquire()
    fake_clock.now += 0.5
    second.acquire()
    assert fake_clock.sleeps == [pytest.approx(1.0)]


def test_spacing_across_threads(settings, fake_clock):
    limiter = make(settings, fake_clock)
    other = make(settings, fake_clock)
    threads = [threading.Thread(target=lambda lim=lim: [lim.acquire() for _ in range(3)])
               for lim in (limiter, other, limiter, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    times = recent(settings)
    assert len(times) == 12
    assert all(b - a == pytest.approx(1.5) for a, b in pairwise(times))


def test_hourly_budget(fake_clock):
    settings = load_settings({"LUK_MAX_REQ_PER_HOUR": "3"})
    limiter = make(settings, fake_clock)
    for _ in range(3):
        limiter.acquire()
    with pytest.raises(BudgetExceeded, match="hourly budget reached"):
        limiter.acquire()
    assert len(recent(settings)) == 3
    fake_clock.now += 3600
    limiter.acquire()
    assert len(recent(settings)) == 1


def test_budget_can_be_lowered_but_never_raised(settings, fake_clock):
    assert make(settings, fake_clock).max_per_hour == 240
    assert make(load_settings({"LUK_MAX_REQ_PER_HOUR": "9999"}), fake_clock).max_per_hour == 240


def test_clock_skew_never_sleeps_more_than_the_interval(settings, fake_clock):
    settings.cache_dir.mkdir(parents=True)
    settings.ratelimit_path.write_text(json.dumps({"next_allowed_at": fake_clock.now + 10_000, "recent": []}))
    make(settings, fake_clock).acquire()
    assert fake_clock.sleeps == [1.5]


def test_corrupt_state_is_treated_as_empty(settings, fake_clock):
    settings.cache_dir.mkdir(parents=True)
    settings.ratelimit_path.write_text("{broken")
    make(settings, fake_clock).acquire()
    assert recent(settings) == [fake_clock.now]


def test_file_lock_held_elsewhere_times_out_as_rate_limited(settings, fake_clock):
    settings.cache_dir.mkdir(parents=True)
    with FileLock(str(settings.ratelimit_lock_path)), pytest.raises(RateLimited):
        make(settings, fake_clock, lock_timeout_s=0.05).acquire()


def test_interval_limiter_spaces_calls():
    now = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = IntervalLimiter(0.2, clock=lambda: now[0], sleep=sleep)
    limiter.acquire()
    now[0] += 0.05
    limiter.acquire()
    now[0] += 1.0
    limiter.acquire()
    assert sleeps == [pytest.approx(0.15)]
