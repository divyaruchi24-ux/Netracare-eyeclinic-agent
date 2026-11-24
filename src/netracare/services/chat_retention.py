"""Chat transcript retention (default 90 days, user decision 2026-11-24).

Deletes conversations whose last activity is older than the window: their messages and
handoffs go too, and triage alerts keep their own record but drop the link to the session.
Sessions with an open or claimed handoff are kept until staff close it.

CLI: python -m netracare.services.chat_retention purge [--days N] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from netracare.db.models import ChatHandoff, ChatMessage, ChatSession, TriageAlert
from netracare.domain.enums import HandoffStatus


@dataclass(frozen=True)
class PurgeReport:
    sessions: int
    messages: int
    handoffs: int


def purge(session: Session, now: datetime, days: int, *, dry_run: bool = False) -> PurgeReport:
    cutoff = now - timedelta(days=days)
    busy = select(ChatHandoff.session_id).where(
        ChatHandoff.status.in_((HandoffStatus.OPEN, HandoffStatus.CLAIMED))
    )
    ids = list(
        session.scalars(
            select(ChatSession.id).where(
                ChatSession.last_active_at < cutoff, ChatSession.id.not_in(busy)
            )
        )
    )
    if not ids:
        return PurgeReport(0, 0, 0)
    n_msgs = len(
        list(session.scalars(select(ChatMessage.id).where(ChatMessage.session_id.in_(ids))))
    )
    n_hand = len(
        list(session.scalars(select(ChatHandoff.id).where(ChatHandoff.session_id.in_(ids))))
    )
    if not dry_run:
        session.execute(
            update(TriageAlert)
            .where(TriageAlert.chat_session_id.in_(ids))
            .values(chat_session_id=None)
        )
        session.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(ids)))
        session.execute(delete(ChatHandoff).where(ChatHandoff.session_id.in_(ids)))
        session.execute(delete(ChatSession).where(ChatSession.id.in_(ids)))
        session.flush()
    return PurgeReport(len(ids), n_msgs, n_hand)


def main(argv: list[str] | None = None) -> int:
    from netracare.config import get_settings
    from netracare.db.session import make_engine, make_session_factory, session_scope
    from netracare.domain.clock import SystemClock

    parser = argparse.ArgumentParser(prog="python -m netracare.services.chat_retention")
    parser.add_argument("command", choices=["purge"])
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    days = args.days or settings.chat_retention_days
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory, write=True) as s:
        r = purge(s, SystemClock().now_utc(), days, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {r.sessions} conversations older than {days} days "
          f"({r.messages} messages, {r.handoffs} handoffs)")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
