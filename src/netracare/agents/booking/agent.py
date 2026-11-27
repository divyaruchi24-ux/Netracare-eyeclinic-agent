"""`BookingAgent`: the facade ChatService and the approval hook use.

agent = BookingAgent(checkpointer)
turn = agent.handle(runtime, text)              # one patient turn (graph run)
agent.on_approval_decided(runtime, event)       # HITL resume after a front-desk decision
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from netracare.agents.booking.graph import ACTIVE_STEPS, build_graph, wants_resume
from netracare.agents.booking.state import AgentRuntime, PatientChoice
from netracare.db.models import Holiday, Patient
from netracare.domain.enums import AgentStep
from netracare.domain.policies import is_minor
from netracare.log import get_logger
from netracare.observability.tracing import traced
from netracare.services.access import AccessPolicy

log = get_logger(__name__)


@dataclass
class AgentTurn:
    kind: str
    text: str
    step: str
    options: list[dict[str, Any]] = field(default_factory=list)
    readback: dict[str, Any] | None = None
    safety: dict[str, Any] = field(default_factory=dict)
    approval_request_id: str | None = None
    appointment_id: str | None = None
    llm_call_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    tool_calls: list[str] = field(default_factory=list)
    wrote: bool = False


def patient_choices(rt: AgentRuntime) -> list[PatientChoice]:
    ids = AccessPolicy(rt.session).patient_ids_for(rt.actor)
    if not ids:
        return []
    today = rt.clock.now_utc().astimezone(rt.settings.tz).date()
    rows = rt.session.scalars(
        select(Patient).where(Patient.id.in_(ids)).order_by(Patient.full_name)
    )
    return [
        PatientChoice(p.id, p.full_name, is_minor(p.dob, today), p.user_id == rt.actor.id)
        for p in rows
    ]


def load_holidays(rt: AgentRuntime) -> dict[Any, str]:
    return {h.day: h.name for h in rt.session.scalars(select(Holiday))}


class BookingAgent:
    def __init__(self, checkpointer: Any) -> None:
        self.checkpointer = checkpointer
        self.graph = build_graph(checkpointer)
        self._lock = threading.Lock()  # one graph run per thread at a time

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}

    def state(self, thread_id: str) -> dict[str, Any]:
        snap = self.graph.get_state(self._config(thread_id))
        return dict(snap.values) if snap and snap.values else {}

    def is_active(self, thread_id: str) -> bool:
        return self.state(thread_id).get("step") in ACTIVE_STEPS

    def is_paused(self, thread_id: str) -> bool:
        return self.state(thread_id).get("step") == AgentStep.PAUSED_SAFETY.value

    def wants_resume(self, thread_id: str, text: str) -> bool:
        return self.is_paused(thread_id) and wants_resume(text)

    def pending(self, thread_id: str) -> list[dict[str, Any]]:
        return list(self.state(thread_id).get("pending") or [])

    def pause_for_safety(self, thread_id: str) -> None:
        st = self.state(thread_id)
        if st.get("step") in ACTIVE_STEPS:
            self.graph.update_state(
                self._config(thread_id),
                {"step": AgentStep.PAUSED_SAFETY.value, "step_before_pause": st.get("step")},
            )

    def _prepare(self, rt: AgentRuntime) -> None:
        if not rt.patients:
            rt.patients = patient_choices(rt)
        if not rt.holidays:
            rt.holidays = load_holidays(rt)
        if not rt.redact_names:
            rt.redact_names = [p.name for p in rt.patients]

    @traced(
        "agent.turn",
        tags=["agent", "booking"],
        inputs=lambda self, rt, text: {"chars": len(text)},
        outputs=lambda t: {"kind": t.kind, "step": t.step, "tools": t.tool_calls, "wrote": t.wrote},
    )
    def handle(self, rt: AgentRuntime, text: str) -> AgentTurn:
        self._prepare(rt)
        with self._lock:
            before = len(self.state(rt.thread_id).get("writes") or [])
            out = self.graph.invoke(
                {"input": text, "event": None}, self._config(rt.thread_id), context=rt
            )
        reply = out.get("reply") or {}
        return AgentTurn(
            kind=str(reply.get("kind", "info")),
            text=str(reply.get("text", "")),
            step=str(out.get("step", "collect")),
            options=list(reply.get("options") or []),
            readback=reply.get("readback"),
            safety=dict(reply.get("safety") or {}),
            approval_request_id=reply.get("approval_request_id"),
            appointment_id=reply.get("appointment_id"),
            llm_call_ids=list(rt.llm_call_ids),
            cost_usd=rt.cost_usd,
            tool_calls=list(rt.tool_calls),
            wrote=len(out.get("writes") or []) > before
            and bool((out.get("writes") or [{}])[-1].get("ok")),
        )

    def on_approval_decided(self, rt: AgentRuntime, event: dict[str, Any]) -> AgentTurn | None:
        """Resume the thread that is waiting for this approval request (None if none is)."""
        if not any(
            p["approval_request_id"] == event.get("request_id") for p in self.pending(rt.thread_id)
        ):
            return None
        with self._lock:
            out = self.graph.invoke(
                {"input": "", "event": event}, self._config(rt.thread_id), context=rt
            )
        reply = out.get("reply") or {}
        return AgentTurn(
            kind=str(reply.get("kind")), text=str(reply.get("text", "")), step=str(out.get("step"))
        )

    def purge(self, thread_ids: list[str]) -> None:
        for t in thread_ids:
            try:
                self.checkpointer.delete_thread(t)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("agent.purge_failed", error=type(exc).__name__)
