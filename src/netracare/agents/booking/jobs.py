"""Booking-agent jobs. CLI: python -m netracare.agents.booking.jobs overdue

`overdue` sends the policy section 12 notice for every approval request pending longer than
APPROVAL_OVERDUE_HOURS (once per request). Safe to run from a scheduler every few minutes.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    from netracare.agents.booking.hitl import run_overdue
    from netracare.config import get_settings
    from netracare.db.session import make_engine, make_session_factory, session_scope
    from netracare.domain.clock import SystemClock

    parser = argparse.ArgumentParser(prog="python -m netracare.agents.booking.jobs")
    parser.add_argument("command", choices=["overdue"])
    parser.parse_args(argv)
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory, write=True) as s:
        sent = run_overdue(s, settings, SystemClock())
    print(f"sent {sent} overdue approval notice(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
