"""Daily eye-fact banner: one fact per clinic-local day from an approved topic list.

Generated lazily on the first request of the day, validated by the advice filter, and stored
in `eye_facts` (primary key = day). Any rejection, LLM error, disabled LLM or exhausted budget
falls back to the reviewed static fact for that topic. Never raises for LLM reasons.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import EyeFact
from netracare.db.session import begin_write, outside_transaction
from netracare.domain.clock import Clock, clinic_today
from netracare.guardrails.advice_filter import check_fact
from netracare.guardrails.scripts import DISCLAIMERS
from netracare.llm.client import LLMClient
from netracare.llm.schemas import EyeFactOutput
from netracare.llm.types import LLMError
from netracare.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class EyeFactView:
    day: date
    topic: str
    text: str
    source: str
    disclaimer: str = DISCLAIMERS["eye_fact"]


@lru_cache(maxsize=4)
def load_topics(prompts_dir: str) -> tuple[tuple[Topic, ...], dict[str, str]]:
    root = Path(prompts_dir) / "eye_fact"
    topics_raw: dict[str, Any] = yaml.safe_load((root / "topics.yaml").read_text(encoding="utf-8"))
    facts_raw: dict[str, Any] = yaml.safe_load(
        (root / "static_facts.yaml").read_text(encoding="utf-8")
    )
    topics = tuple(
        Topic(id=t["id"], title=t["title"], keywords=tuple(t["keywords"]))
        for t in topics_raw["topics"]
    )
    facts: dict[str, str] = dict(facts_raw["facts"])
    missing = {t.id for t in topics} - set(facts)
    if missing:
        raise ValueError(f"static facts missing for topics: {sorted(missing)}")
    return topics, facts


def topic_for(day: date, topics: tuple[Topic, ...]) -> Topic:
    """Stable across processes and restarts: sha256 of the ISO date, mod the topic count."""
    digest = hashlib.sha256(day.isoformat().encode()).digest()
    return topics[int.from_bytes(digest[:8], "big") % len(topics)]


class EyeFactService:
    def __init__(
        self, session: Session, clock: Clock, settings: Settings, llm: LLMClient | None
    ) -> None:
        self.s = session
        self.clock = clock
        self.settings = settings
        self.llm = llm
        self.topics, self.static = load_topics(settings.prompts_dir)

    def today(self) -> EyeFactView:
        return self.for_day(clinic_today(self.clock, self.settings.tz))

    def for_day(self, day: date) -> EyeFactView:
        existing = self.s.get(EyeFact, day)
        if existing is not None:
            return _view(existing)
        topic = topic_for(day, self.topics)
        text, source, version, rejected = self._generate(topic)
        row = EyeFact(
            day=day,
            topic=topic.id,
            text=text,
            source=source,
            prompt_version=version,
            rejected_reason=rejected,
            created_at=self.clock.now_utc(),
        )
        return _view(self._insert(row))

    def _generate(self, topic: Topic) -> tuple[str, str, int | None, str | None]:
        static = self.static[topic.id]
        if self.llm is None or not self.llm.available:
            return static, "static", None, "llm_unavailable"
        try:
            with outside_transaction(self.s, write_after=False):
                result = self.llm.generate(
                    "eye_fact",
                    {
                        "topic_id": topic.id,
                        "topic_title": topic.title,
                        "keywords": ", ".join(topic.keywords),
                    },
                    purpose="eye_fact",
                    schema=EyeFactOutput,
                )
        except LLMError as exc:
            log.info("eye_fact.fallback", topic=topic.id, reason=exc.code)
            return static, "static", None, exc.code
        fact: EyeFactOutput = result.value
        text = " ".join(fact.fact.split())
        violations = check_fact(text, topic.keywords)
        if violations:
            log.warning("eye_fact.rejected", topic=topic.id, violations=violations)
            return static, "static", result.prompt_version, ",".join(violations)[:255]
        return text, "llm", result.prompt_version, None

    def _insert(self, row: EyeFact) -> EyeFact:
        """Insert in a short write transaction; if another request won the race, read theirs."""
        self.s.rollback()  # end any read transaction (nothing pending: this path only reads)
        begin_write(self.s)
        try:
            self.s.add(row)
            self.s.commit()
            return row
        except IntegrityError:
            self.s.rollback()
            winner = self.s.get(EyeFact, row.day)
            if winner is None:  # pragma: no cover - the conflict implies a row exists
                raise
            return winner


def _view(row: EyeFact) -> EyeFactView:
    return EyeFactView(day=row.day, topic=row.topic, text=row.text, source=row.source)
