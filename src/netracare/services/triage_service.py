"""Red-flag triage: deterministic rules (floor) + LLM classifier (can only escalate).

Decision (safety policy 4.3 / 4.4):
1. empty / emoji / punctuation-only -> `non_clinical`, no LLM call; > 2000 chars -> 422;
2. rules layer on the whole message (cannot be disabled);
3. LLM layer via a LangChain runnable chain around `LLMClient` (prompt `red_flag_classifier`);
   if it is unavailable, fails, or is over budget -> rules only (`rules_fallback`);
4. final = the more severe of the two; LLM confidence below the threshold escalates
   non_clinical -> routine and routine -> urgent;
5. emergency -> verbatim 5.1 script (+ E4 rinse line) and a front-desk alert; urgent -> 5.2
   script and an alert; routine -> a section 6 routing template; never generated text.

The LLM call happens outside any DB transaction (`outside_transaction`), so a slow model
never holds the SQLite write lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from langchain_core.runnables import Runnable, RunnableLambda
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from netracare.config import Settings
from netracare.db.models import Doctor, Patient, Specialty, TriageAlert
from netracare.db.session import outside_transaction
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import AlertStatus, RedFlagLevel
from netracare.errors import ConflictError, NotFoundError, ValidationAppError
from netracare.guardrails import scripts
from netracare.guardrails.advice_filter import safe_concern
from netracare.guardrails.redflag_rules import (
    CODE_CONCERNS,
    RuleMatch,
    asks_medical_advice,
    detect,
    level_for,
    looks_clinical,
    specialty_for,
)
from netracare.llm.client import LLMClient
from netracare.llm.schemas import TriageLLMOutput
from netracare.llm.types import LLMError
from netracare.log import get_logger, redact_all
from netracare.observability.tracing import traced
from netracare.services.audit_service import AuditService

log = get_logger(__name__)

MAX_TEXT = 2000
EXCERPT_LEN = 200
_ALERT_LEVELS = frozenset({RedFlagLevel.EMERGENCY, RedFlagLevel.URGENT})
_LOW_CONF_ESCALATES = frozenset({RedFlagLevel.NON_CLINICAL, RedFlagLevel.ROUTINE})


@dataclass
class TriageResult:
    level: RedFlagLevel
    codes: list[str]
    specialty_code: str | None
    doctor_name: str | None
    confidence: float | None
    source: str  # rules | llm+rules | rules_fallback | shortcircuit
    message: str
    alert_id: str | None = None
    needs_handoff: bool = False
    is_medical_question: bool = False
    concern_phrase: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _meaningful(text: str) -> bool:
    return bool(re.search(r"[^\W_]", text))  # any letter or digit (emoji/punctuation only -> no)


def combine(
    rules_level: RedFlagLevel | None,
    llm_out: TriageLLMOutput | None,
    threshold: float,
) -> tuple[RedFlagLevel, bool]:
    """(final level, escalated_for_low_confidence). The LLM can never lower the rules level."""
    base = rules_level or RedFlagLevel.NON_CLINICAL
    if llm_out is None:
        return base, False
    final = RedFlagLevel.most_severe(base, llm_out.level)
    if llm_out.confidence < threshold and final in _LOW_CONF_ESCALATES:
        return final.escalate(), True
    return final, False


@dataclass(frozen=True)
class Decision:
    """The DB-free part of triage (shared by the service and the eval tasks)."""

    level: RedFlagLevel
    codes: list[str]
    specialty: str | None
    concern: str | None
    confidence: float | None
    source: str
    needs_handoff: bool
    is_medical_question: bool

    @property
    def route(self) -> str:
        """Eval-facing route label (matches `safety_redflags_v0` expectations)."""
        if self.level is RedFlagLevel.EMERGENCY:
            return "emergency_script"
        if self.level is RedFlagLevel.URGENT:
            return "urgent_script"
        if self.level is RedFlagLevel.ROUTINE:
            return "medical_refusal" if self.is_medical_question else "booking"
        return "faq"


SHORTCIRCUIT = Decision(
    level=RedFlagLevel.NON_CLINICAL,
    codes=[],
    specialty=None,
    concern=None,
    confidence=None,
    source="shortcircuit",
    needs_handoff=False,
    is_medical_question=False,
)


def is_shortcircuit(text: str) -> bool:
    return not _meaningful(text)


def decide(
    text: str,
    matches: list[RuleMatch],
    llm_out: TriageLLMOutput | None,
    source: str,
    threshold: float,
) -> Decision:
    rule_codes = [m.code for m in matches]
    clinical = looks_clinical(text)
    # The rules floor: red-flag codes, else "routine" for anything that looks clinical. The LLM
    # can only raise it (an eye question is never downgraded to non-clinical by the LLM).
    rules_level = level_for(matches) or (RedFlagLevel.ROUTINE if clinical else None)
    level, escalated = combine(rules_level, llm_out, threshold)
    codes = sorted(set(rule_codes) | set(llm_out.matched_codes if llm_out else []), key=_code_order)
    # Clinical-looking text with no red flags and no LLM help: routine, with a handoff when the
    # LLM failed - never "nothing to see here".
    needs_handoff = escalated or (
        llm_out is None and not rule_codes and clinical and source == "rules_fallback"
    )
    confidence = llm_out.confidence if llm_out else (1.0 if rule_codes else None)
    specialty = specialty_for(codes, text)
    if specialty is None and llm_out is not None and level is not RedFlagLevel.NON_CLINICAL:
        specialty = llm_out.suggested_specialty
    if specialty is None and level is not RedFlagLevel.NON_CLINICAL:
        specialty = "GENERAL"
    concern = next((CODE_CONCERNS[c] for c in codes if c in CODE_CONCERNS), None) or (
        safe_concern(llm_out.concern_phrase) if llm_out else None
    )
    return Decision(
        level=level,
        codes=codes,
        specialty=specialty,
        concern=concern,
        confidence=confidence,
        source=source,
        needs_handoff=needs_handoff,
        is_medical_question=bool(llm_out and llm_out.is_medical_question)
        or asks_medical_advice(text),
    )


def build_chain(
    llm: LLMClient, *, actor_user_id: str | None, names: list[str]
) -> Runnable[str, TriageLLMOutput]:
    """text -> prompt variables -> LLMClient (ChatPromptTemplate render, budget, retries,
    tracing) -> validated `TriageLLMOutput`."""

    def to_vars(text: str) -> dict[str, str]:
        return {"text": text}

    def call(variables: dict[str, str]) -> Any:
        return llm.generate(
            "red_flag_classifier",
            variables,
            purpose="triage",
            schema=TriageLLMOutput,
            actor_user_id=actor_user_id,
            redact_names=names,
        )

    def parse(result: Any) -> TriageLLMOutput:
        value = result.value
        return (
            value if isinstance(value, TriageLLMOutput) else TriageLLMOutput.model_validate(value)
        )

    return RunnableLambda(to_vars) | RunnableLambda(call) | RunnableLambda(parse)


def _classify_inputs(_: object, text: str, **kw: Any) -> dict[str, Any]:
    return {"text_len": len(text), "has_patient": kw.get("patient_id") is not None}


def _classify_outputs(r: TriageResult) -> dict[str, Any]:
    return {
        "level": r.level,
        "codes": r.codes,
        "source": r.source,
        "specialty": r.specialty_code,
        "alert": r.alert_id is not None,
        "handoff": r.needs_handoff,
    }


class TriageService:
    def __init__(
        self, session: Session, clock: Clock, settings: Settings, llm: LLMClient | None
    ) -> None:
        self.s = session
        self.clock = clock
        self.settings = settings
        self.llm = llm
        self.audit = AuditService(session)

    # ------------------------------------------------------------------ classification

    @traced(
        "triage.classify",
        tags=["triage", "safety"],
        inputs=_classify_inputs,
        outputs=_classify_outputs,
    )
    def classify(
        self,
        text: str,
        *,
        actor: CurrentUser | None = None,
        patient_id: str | None = None,
        raise_alert: bool = True,
        dedup_key: str | None = None,
        chat_session_id: str | None = None,
        min_level: RedFlagLevel | None = None,
    ) -> TriageResult:
        """Classify `text`. `dedup_key`: an open alert with the same key raised within
        ALERT_DEDUP_MINUTES is reused instead of creating another (retried bookings, repeated
        chat messages). `min_level`: a floor from another signal (e.g. the chat router said
        "emergency"); the result is never less severe than it (safety policy 4.4)."""
        if len(text) > MAX_TEXT:
            raise ValidationAppError("text_too_long", f"please keep it under {MAX_TEXT} characters")
        if not _meaningful(text):
            return TriageResult(
                level=RedFlagLevel.NON_CLINICAL,
                codes=[],
                specialty_code=None,
                doctor_name=None,
                confidence=None,
                source="shortcircuit",
                message=scripts.NON_CLINICAL_HANDOFF,
            )

        matches = detect(text)
        names = self._patient_names(patient_id)
        llm_out, source = self._llm(text, actor, names, level_for(matches))
        d = decide(text, matches, llm_out, source, self.settings.triage_confidence_threshold)
        if min_level is not None and min_level.severity() > d.level.severity():
            d = replace(d, level=min_level, specialty=d.specialty or "GENERAL")
        doctor = self._doctor_name(d.specialty)
        result = TriageResult(
            level=d.level,
            codes=d.codes,
            specialty_code=d.specialty,
            doctor_name=doctor,
            confidence=d.confidence,
            source=d.source,
            message=self._message(
                d.level, d.codes, d.specialty, d.concern, doctor, d.is_medical_question
            ),
            needs_handoff=d.needs_handoff,
            is_medical_question=d.is_medical_question,
            concern_phrase=d.concern,
        )
        level = d.level
        if raise_alert and level in _ALERT_LEVELS:
            existing = self._recent_alert(dedup_key)
            if existing is not None:
                result.alert_id = existing.id
                result.meta["alert_deduplicated"] = True
            else:
                result.alert_id = self._raise_alert(
                    result, text, patient_id, actor, names, dedup_key, chat_session_id
                )
        return result

    def _recent_alert(self, dedup_key: str | None) -> TriageAlert | None:
        if not dedup_key or self.settings.alert_dedup_minutes <= 0:
            return None
        since = self.clock.now_utc() - timedelta(minutes=self.settings.alert_dedup_minutes)
        return self.s.scalar(
            select(TriageAlert)
            .where(
                TriageAlert.dedup_key == dedup_key,
                TriageAlert.status == AlertStatus.OPEN,
                TriageAlert.created_at >= since,
            )
            .order_by(TriageAlert.created_at.desc())
            .limit(1)
        )

    def doctor_for(self, specialty: str | None) -> str | None:
        """Name of the first active doctor of a specialty (public helper for chat routing)."""
        return self._doctor_name(specialty)

    def _llm(
        self,
        text: str,
        actor: CurrentUser | None,
        names: list[str],
        rules_level: RedFlagLevel | None,
    ) -> tuple[TriageLLMOutput | None, str]:
        if self.llm is None or not self.llm.available:
            return None, "rules"
        chain = build_chain(self.llm, actor_user_id=actor.id if actor else None, names=names)
        try:
            with outside_transaction(self.s):
                out = chain.invoke(text)
        except LLMError as exc:
            log.warning("triage.llm_fallback", error=exc.code, rules_level=str(rules_level))
            return None, "rules_fallback"
        return out, "llm+rules"

    def _message(
        self,
        level: RedFlagLevel,
        codes: list[str],
        specialty: str | None,
        concern: str | None,
        doctor: str | None,
        is_question: bool,
    ) -> str:
        if level is RedFlagLevel.EMERGENCY:
            return scripts.emergency_message(codes)
        if level is RedFlagLevel.URGENT:
            return scripts.URGENT_SCRIPT
        if level is RedFlagLevel.ROUTINE:
            routing = scripts.routing_sentence(specialty, concern, doctor)
            if is_question:
                return scripts.MEDICAL_REFUSAL.format(
                    topic=concern or "this", routing_sentence=routing
                )
            return f"{routing} {scripts.CHECK_AVAILABILITY}"
        return scripts.NON_CLINICAL_HANDOFF

    def _doctor_name(self, specialty: str | None) -> str | None:
        if specialty is None:
            return None
        return self.s.scalar(
            select(Doctor.full_name)
            .join(Specialty, Doctor.specialty_id == Specialty.id)
            .where(Specialty.code == specialty, Doctor.is_active)
            .order_by(Doctor.code)
            .limit(1)
        )

    def _patient_names(self, patient_id: str | None) -> list[str]:
        if patient_id is None:
            return []
        name = self.s.scalar(select(Patient.full_name).where(Patient.id == patient_id))
        return [name] if name else []

    # ------------------------------------------------------------------ alerts

    def _raise_alert(
        self,
        result: TriageResult,
        text: str,
        patient_id: str | None,
        actor: CurrentUser | None,
        names: list[str],
        dedup_key: str | None = None,
        chat_session_id: str | None = None,
    ) -> str:
        excerpt = redact_all(" ".join(text.split()), names)
        if len(excerpt) > EXCERPT_LEN:
            excerpt = excerpt[: EXCERPT_LEN - 1] + "…"
        alert = TriageAlert(
            created_at=self.clock.now_utc(),
            level=result.level,
            codes=result.codes,
            confidence=result.confidence,
            source=result.source,
            excerpt_redacted=excerpt,
            patient_id=patient_id,
            actor_user_id=actor.id if actor else None,
            status=AlertStatus.OPEN,
            dedup_key=dedup_key,
            chat_session_id=chat_session_id,
        )
        self.s.add(alert)
        self.s.flush()
        self.audit.record(
            actor,
            "triage.alert",
            "triage_alert",
            alert.id,
            after={"level": result.level.value, "codes": result.codes, "source": result.source},
        )
        log.warning("triage.alert", level=result.level.value, codes=result.codes, alert_id=alert.id)
        return alert.id

    def list_alerts(self, status: AlertStatus | None = None, limit: int = 100) -> list[TriageAlert]:
        stmt = select(TriageAlert)
        if status is not None:
            stmt = stmt.where(TriageAlert.status == status)
        severity = {RedFlagLevel.EMERGENCY: 0, RedFlagLevel.URGENT: 1}
        alerts = list(self.s.scalars(stmt.order_by(TriageAlert.created_at.desc()).limit(limit)))
        open_first = {AlertStatus.OPEN: 0, AlertStatus.ACKNOWLEDGED: 1, AlertStatus.RESOLVED: 2}
        return sorted(
            alerts,
            key=lambda a: (
                open_first[a.status],
                severity.get(a.level, 2),
                -a.created_at.timestamp(),
            ),
        )

    def count_open(self, level: RedFlagLevel | None = None) -> int:
        stmt = select(TriageAlert).where(TriageAlert.status == AlertStatus.OPEN)
        if level is not None:
            stmt = stmt.where(TriageAlert.level == level)
        return len(list(self.s.scalars(stmt)))

    def _get(self, alert_id: str) -> TriageAlert:
        alert = self.s.get(TriageAlert, alert_id)
        if alert is None:
            raise NotFoundError("alert_not_found", "alert not found")
        return alert

    def ack(
        self, actor: CurrentUser, alert_id: str, *, expected_version: int | None = None
    ) -> TriageAlert:
        alert = self._get(alert_id)
        if alert.status is not AlertStatus.OPEN or (
            expected_version is not None and alert.version != expected_version
        ):
            raise ConflictError("already_handled", "this alert was already handled")
        alert.status = AlertStatus.ACKNOWLEDGED
        alert.acknowledged_by = actor.id
        alert.acknowledged_at = self.clock.now_utc()
        self._flush()
        self.audit.record(actor, "triage.ack", "triage_alert", alert.id)
        return alert

    def resolve(self, actor: CurrentUser, alert_id: str, note: str) -> TriageAlert:
        if not note.strip():
            raise ValidationAppError("note_required", "a resolution note is required")
        alert = self._get(alert_id)
        if alert.status is AlertStatus.RESOLVED:
            raise ConflictError("already_handled", "this alert was already resolved")
        if alert.status is AlertStatus.OPEN:
            alert.acknowledged_by = actor.id
            alert.acknowledged_at = self.clock.now_utc()
        alert.status = AlertStatus.RESOLVED
        alert.resolved_by = actor.id
        alert.resolved_at = self.clock.now_utc()
        alert.resolution_note = note.strip()
        self._flush()
        self.audit.record(actor, "triage.resolve", "triage_alert", alert.id, reason=note.strip())
        return alert

    def _flush(self) -> None:
        try:
            self.s.flush()
        except StaleDataError as exc:  # pragma: no cover - needs a true concurrent writer
            raise ConflictError("already_handled", "this alert was already handled") from exc


def _code_order(code: str) -> tuple[int, int]:
    return (0 if code.startswith("E") else 1, int(code[1:]))


def alert_dedup_key(owner: str, codes: list[str] | tuple[str, ...]) -> str:
    """Same caller (patient or chat session) + same red-flag codes -> same key."""
    import hashlib

    return hashlib.sha256(f"{owner}|{','.join(sorted(codes))}".encode()).hexdigest()
