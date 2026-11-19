"""Scheduled LLM jobs. `python -m netracare.llm.jobs eye-fact [--date YYYY-MM-DD]`."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from netracare.config import get_settings
from netracare.db.session import make_engine, make_session_factory
from netracare.domain.clock import SystemClock
from netracare.llm.client import LLMClient, make_provider
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.observability.tracing import configure_tracing, flush_tracing
from netracare.services.eye_fact_service import EyeFactService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m netracare.llm.jobs")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ef = sub.add_parser("eye-fact", help="pre-generate the eye fact for a day (default: today)")
    ef.add_argument("--date", type=date.fromisoformat)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_tracing(settings)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    clock = SystemClock()
    llm = LLMClient(
        settings=settings,
        clock=clock,
        provider=make_provider(settings),
        registry=PromptRegistry(settings.prompts_dir).load(),
        pricing=Pricing.load(settings.pricing_file),
        session_factory=factory,
    )
    with factory() as session:
        svc = EyeFactService(session, clock, settings, llm)
        view = svc.for_day(args.date) if args.date else svc.today()
        session.commit()
    flush_tracing()
    engine.dispose()
    print(f"{view.day} [{view.source}] {view.topic}: {view.text}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
