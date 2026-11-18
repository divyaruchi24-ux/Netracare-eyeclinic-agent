"""CLI: `python -m netracare.seed [--reset] [--weeks 8] [--today YYYY-MM-DD] [--summary]`."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from netracare.config import get_settings
from netracare.db import models
from netracare.db.session import make_engine, make_session_factory, session_scope
from netracare.domain.clock import SystemClock, clinic_today
from netracare.seed.runner import Seeder

SUMMARY_MODELS = (
    models.Specialty,
    models.Doctor,
    models.User,
    models.StaffUser,
    models.Patient,
    models.PatientGuardian,
    models.ScheduleTemplate,
    models.Holiday,
    models.Leave,
    models.Slot,
    models.KnowledgeDoc,
    models.AuditLog,
)


def _alembic(url: str, root: Path) -> Config:
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netracare.seed", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repo root")
    parser.add_argument("--reset", action="store_true", help="downgrade+upgrade DB first")
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    parser.add_argument("--summary", action="store_true", help="print table row counts")
    args = parser.parse_args(argv)

    settings = get_settings()
    root: Path = args.root.resolve()
    if args.reset:
        cfg = _alembic(settings.database_url, root)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

    today = args.today or clinic_today(SystemClock(), settings.tz)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        result = Seeder(
            session,
            data_dir=root / "seed" / "data",
            kb_dir=root / "kb",
            root=root,
            today=today,
            tz=settings.tz,
            password=settings.seed_default_password.get_secret_value(),
            weeks=args.weeks,
        ).run()
    print(f"seed complete (today={today}); inserted: {result.inserted or 'nothing new'}")

    if args.summary:
        with session_scope(factory) as session:
            for model in SUMMARY_MODELS:
                n = session.scalar(select(func.count()).select_from(model))
                print(f"  {model.__tablename__:<20} {n}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
