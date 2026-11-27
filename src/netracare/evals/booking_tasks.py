"""Booking-agent conversation evals (Phase 4).

Each case is a multi-turn conversation over a fresh copy of the seeded demo DB, with the clock
frozen at Tue 10 Nov 2026 09:00 IST. Turns are user texts or scripted events:
    {"user": "..."}                      a patient message (a plain string means the same)
    {"advance_minutes": 45}              time passes
    {"steal_chosen": true}               another patient books the read-back's slot first
    {"fault": "book"}                    the next book/reschedule call raises (a DB failure)
    {"decide": "approved"|"rejected"|"changed"}   the front desk decides the pending request
    {"emergency": true}                  ChatService's layer 1 pauses the flow (policy 5.1)
Setup: `setup.bookings` pre-books appointments for the patient (for reschedule/cancel cases).

* `booking_agent_offline` (CI gate): understanding by keywords (no LLM), so the graph, tools,
  policy and write safety are tested deterministically.
* `booking_agent_live`: the same conversations with the real `booking_nlu@v1` on Groq.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from netracare.config import get_settings

IST = ZoneInfo("Asia/Kolkata")
EVAL_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=IST)


@lru_cache(maxsize=1)
def _template() -> Path:
    """A seeded DB built once per process (copied per conversation)."""
    from netracare.db.base import Base
    from netracare.db.session import make_engine, make_session_factory
    from netracare.seed.runner import Seeder

    settings = get_settings()
    path = Path(tempfile.mkdtemp(prefix="netracare-booking-eval-")) / "template.db"
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        Seeder(
            s,
            data_dir=Path("seed/data"),
            kb_dir=Path(settings.kb_dir),
            root=Path(),
            today=EVAL_NOW.date(),
            tz=IST,
            password=settings.seed_default_password.get_secret_value(),
        ).run()
        s.commit()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


def run_conversation(inp: dict[str, Any], *, llm: Any = None) -> dict[str, Any]:
    """Play one conversation; returns per-turn kinds, tool calls, writes and the final state."""
    from netracare.agents.booking import tools as bt
    from netracare.agents.booking.agent import BookingAgent
    from netracare.agents.booking.checkpoint import memory_checkpointer
    from netracare.agents.booking.confirm import is_explicit_yes
    from netracare.agents.booking.state import AgentRuntime
    from netracare.db.models import ChatSession, Doctor, Slot, User
    from netracare.db.session import make_engine, make_session_factory
    from netracare.domain.actor import CurrentUser
    from netracare.domain.enums import Role
    from netracare.seed.ids import seed_id
    from netracare.seed.patients import make_patients
    from netracare.services.approval_service import ApprovalService
    from netracare.services.booking_service import BookingService

    settings = get_settings().model_copy(update={"opik_enabled": False})
    tmp = Path(tempfile.mkdtemp(prefix="netracare-conv-")) / "conv.db"
    shutil.copyfile(_template(), tmp)
    engine = make_engine(f"sqlite:///{tmp.as_posix()}")
    s = make_session_factory(engine)()

    class Clock:
        def __init__(self) -> None:
            self.now = EVAL_NOW

        def now_utc(self) -> datetime:
            return self.now.astimezone(ZoneInfo("UTC"))

    clock = Clock()
    people = {p.key: p for p in make_patients(EVAL_NOW.date())}
    who = people[inp.get("patient", "P010")]
    email = who.email or (who.guardian.email if who.guardian else "")
    user = s.scalar(select(User).where(User.email == email))
    actor = CurrentUser(user.id, Role.PATIENT, user.email, user.email)  # type: ignore[union-attr]
    staff_user = s.scalar(select(User).where(User.role == Role.FRONT_DESK))
    staff = CurrentUser(staff_user.id, Role.FRONT_DESK, staff_user.email, "FD")  # type: ignore[union-attr]
    chat = ChatSession(user_id=actor.id)
    s.add(chat)
    s.flush()
    agent = BookingAgent(memory_checkpointer())

    for i, b in enumerate((inp.get("setup") or {}).get("bookings", [])):
        doctor_id = seed_id("doctor", b["doctor"])
        start = datetime(2026, 11, b["day"], b["hh"], b.get("mm", 0), tzinfo=IST)
        slot = s.scalar(select(Slot).where(Slot.doctor_id == doctor_id, Slot.start_utc == start))
        ctx = bt.ToolContext(s, clock, settings, actor, "setup")
        bt.book_appointment(ctx, patient_id=seed_id("patient", inp.get("patient", "P010")),
                            doctor_id=doctor_id, slot_ids=[slot.id], proposal_id=f"setup{i}")  # type: ignore[union-attr]  # fmt: skip
    s.commit()

    kinds: list[str] = []
    calls: list[str] = []
    replies: list[str] = []
    fault: dict[str, bool] = {"book": False}
    original_book, original_resched = BookingService.book, BookingService.reschedule

    def failing(fn: Any) -> Any:
        def wrapped(self: Any, *a: Any, **k: Any) -> Any:
            if fault["book"]:
                raise RuntimeError("injected failure")
            return fn(self, *a, **k)

        return wrapped

    BookingService.book = failing(original_book)  # type: ignore[method-assign]
    BookingService.reschedule = failing(original_resched)  # type: ignore[method-assign]
    try:
        for turn in inp["turns"]:
            step = turn if isinstance(turn, dict) else {"user": turn}
            if "advance_minutes" in step:
                from datetime import timedelta

                clock.now += timedelta(minutes=step["advance_minutes"])
                continue
            if step.get("fault"):
                fault["book"] = True
                continue
            if step.get("emergency"):
                agent.pause_for_safety(chat.id)
                continue
            if step.get("steal_chosen"):
                chosen = agent.state(chat.id).get("chosen") or {}
                other = people["P011"]
                ou = s.scalar(select(User).where(User.email == other.email))
                octx = bt.ToolContext(s, clock, settings,
                                      CurrentUser(ou.id, Role.PATIENT, ou.email, ou.email), "thief")  # type: ignore[union-attr]  # fmt: skip
                bt.book_appointment(octx, patient_id=seed_id("patient", "P011"),
                                    doctor_id=chosen["doctor_id"], slot_ids=chosen["slot_ids"],
                                    proposal_id="steal")  # fmt: skip
                s.commit()
                continue
            if step.get("decide"):
                pending = agent.pending(chat.id)
                req_id = pending[-1]["approval_request_id"]
                svc = ApprovalService(s, clock, settings, hooks=[])
                rt = AgentRuntime(s, clock, settings, actor, chat.id)
                ev: dict[str, Any] = {"request_id": req_id, "decision": step["decide"]}
                if step["decide"] == "approved":
                    svc.approve(staff, req_id)
                elif step["decide"] == "rejected":
                    svc.reject(staff, req_id, "not today")
                else:
                    view = svc.get(staff, req_id)
                    appt = view.target or view.appointment
                    later = s.scalars(select(Slot).where(
                        Slot.doctor_id == appt.doctor_id, Slot.start_utc > appt.start_utc
                    ).order_by(Slot.start_utc).limit(10)).all()  # fmt: skip
                    from netracare.services.availability_service import slots_needed

                    n = slots_needed(appt.type)
                    svc.approve_with_changes(staff, req_id, [x.id for x in later[2 : 2 + n]])
                    s.flush()
                    doc = s.get(Doctor, appt.doctor_id)
                    ev.update(start_local=appt.start_utc.astimezone(IST).isoformat(),
                              doctor_name=doc.full_name if doc else None)  # fmt: skip
                t = agent.on_approval_decided(rt, ev)
                s.commit()
                kinds.append(t.kind if t else "none")
                replies.append(t.text if t else "")
                continue
            rt = AgentRuntime(s, clock, settings, actor, chat.id, llm, chat_session=chat)
            t = agent.handle(rt, step["user"])
            s.commit()
            fault["book"] = False
            kinds.append(t.kind)
            calls.extend(t.tool_calls)
            replies.append(t.text)
    finally:
        BookingService.book, BookingService.reschedule = original_book, original_resched  # type: ignore[method-assign]

    state = agent.state(chat.id)
    writes = state.get("writes") or []
    safe = all(
        0 <= w.get("readback_turn", -1) < w["turn"] and is_explicit_yes(w["yes_text"])
        for w in writes
    )
    out = {
        "kinds": kinds,
        "final_step": state.get("step"),
        "final_kind": kinds[-1] if kinds else None,
        "writes_ok": [w["tool"] for w in writes if w.get("ok")],
        "write_attempts": len(writes),
        "write_safe": safe,
        "tool_calls": calls,
        "turns": len(kinds),
        "last_reply": replies[-1][:300] if replies else "",
    }
    s.close()
    engine.dispose()
    return out


def booking_agent_offline_task(inp: dict[str, Any]) -> dict[str, Any]:
    return run_conversation(inp, llm=None)


def booking_agent_live_task(inp: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - live
    from netracare.evals.tasks import live_llm

    return run_conversation(inp, llm=live_llm())
