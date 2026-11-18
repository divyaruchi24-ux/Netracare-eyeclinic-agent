"""In-process login throttle (per email). Phase 7 can swap this for a shared store."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

from netracare.errors import RateLimitedError


class LoginThrottle:
    def __init__(self, max_failures: int = 5, window: timedelta = timedelta(minutes=15)) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: datetime) -> deque[datetime]:
        q = self._failures[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def check(self, key: str, now: datetime) -> None:
        with self._lock:
            q = self._prune(key, now)
            if len(q) >= self.max_failures:
                retry_after = int((q[0] + self.window - now).total_seconds()) + 1
                raise RateLimitedError(
                    "too_many_attempts",
                    "too many failed login attempts; try again later",
                    details={"retry_after_seconds": retry_after},
                )

    def record_failure(self, key: str, now: datetime) -> None:
        with self._lock:
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
