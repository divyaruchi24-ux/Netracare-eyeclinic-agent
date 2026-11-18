from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netracare.errors import RateLimitedError
from netracare.security.rate_limit import LoginThrottle

T = datetime(2026, 1, 1, tzinfo=UTC)


def test_blocks_after_max_failures_and_recovers_after_window() -> None:
    t = LoginThrottle(max_failures=3, window=timedelta(minutes=15))
    for i in range(3):
        t.check("a", T + timedelta(minutes=i))
        t.record_failure("a", T + timedelta(minutes=i))
    with pytest.raises(RateLimitedError) as e:
        t.check("a", T + timedelta(minutes=3))
    assert e.value.details and e.value.details["retry_after_seconds"] > 0
    t.check("b", T)  # other keys unaffected
    t.check("a", T + timedelta(minutes=15, seconds=1))  # oldest failure aged out


def test_reset_clears() -> None:
    t = LoginThrottle(max_failures=1)
    t.record_failure("a", T)
    t.reset("a")
    t.check("a", T)
