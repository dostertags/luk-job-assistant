"""Politeness limits (spec §1.5, §4.9).

`RateLimiter` spaces every takealuk.com request (CLI, MCP threads, doctor, login probe) ≥1.5 s
apart across all luk processes and threads, and enforces the rolling hourly budget:
module `threading.Lock` → `FileLock(ratelimit.lock)` → read `ratelimit.json`
`{next_allowed_at, recent: [ts…]}` → sleep until `next_allowed_at` → prune `recent` to 3600 s →
budget check → write `next_allowed_at = now + 1.5`, append now → release → caller sends.

`IntervalLimiter` is the per-process ≥200 ms spacing for Algolia. Clocks and sleeps are injectable.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from luk_cli.config import ALGOLIA_MIN_INTERVAL_S, MAX_REQ_PER_HOUR, MIN_REQUEST_INTERVAL_S, Settings
from luk_cli.errors import BudgetExceeded, RateLimited

HOUR_S = 3600.0
_PROCESS_LOCK = threading.Lock()


class RateLimiter:
    """Cross-process ≥`min_interval_s` spacing plus ≤`max_per_hour` requests per rolling hour."""

    def __init__(
        self,
        state_path: Path,
        lock_path: Path,
        *,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        max_per_hour: int = MAX_REQ_PER_HOUR,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        lock_timeout_s: float = 60.0,
    ) -> None:
        self.state_path = state_path
        self.lock_path = lock_path
        self.min_interval_s = min_interval_s
        self.max_per_hour = min(max_per_hour, MAX_REQ_PER_HOUR)
        self._clock = clock
        self._sleep = sleep
        self._lock_timeout_s = lock_timeout_s

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> RateLimiter:
        return cls(
            settings.ratelimit_path, settings.ratelimit_lock_path, max_per_hour=settings.max_req_per_hour, **kwargs
        )

    def acquire(self) -> None:
        """Block until this process may send one takealuk.com request; BudgetExceeded when spent."""
        with _PROCESS_LOCK:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with FileLock(str(self.lock_path), timeout=self._lock_timeout_s):
                    self._take_slot()
            except Timeout:
                raise RateLimited("another luk process holds the rate limiter; retry shortly") from None

    def _take_slot(self) -> None:
        next_allowed_at, recent = self._read()
        now = self._clock()
        wait = min(next_allowed_at - now, self.min_interval_s)  # a clock jump never stalls for long
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
        recent = [t for t in recent if t > now - HOUR_S]
        if len(recent) >= self.max_per_hour:
            minutes = math.ceil((min(recent) + HOUR_S - now) / 60)
            raise BudgetExceeded(
                f"hourly budget reached ({self.max_per_hour} requests per rolling hour); next slot in ~{minutes} min"
            )
        recent.append(now)
        self.state_path.write_text(
            json.dumps({"next_allowed_at": now + self.min_interval_s, "recent": recent}), "utf-8"
        )

    def _read(self) -> tuple[float, list[float]]:
        try:
            raw = json.loads(self.state_path.read_text("utf-8"))
            return float(raw["next_allowed_at"]), [float(t) for t in raw["recent"]]
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return 0.0, []


class IntervalLimiter:
    """Per-process minimum spacing between calls (thread-safe)."""

    def __init__(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._next = float("-inf")
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            wait = self._next - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._next = self._clock() + self.min_interval_s


ALGOLIA_LIMITER = IntervalLimiter(ALGOLIA_MIN_INTERVAL_S)
