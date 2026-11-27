"""The booking agent as a LangGraph state machine. One graph run = one patient turn; the
checkpointer (thread = chat session) carries the state between turns and across restarts.

    START -> ingest -> route -> collect | choose | confirm | list | abort | decided -> END
                                   confirm --(explicit yes, code)--> execute -> END

* `ingest`: expiry, safety resume, understanding the turn (LLM or keywords; buttons skip it).
* `collect`: merge what the patient said (patient, doctor/specialty, dates, time, target
  appointment), ask for what is missing, else check availability and propose options.
* `choose`: map the reply to a proposed option -> fixed read-back (with the approval note).
* `confirm`: ONLY `is_explicit_yes(text)` leads to `execute`; changes go back to `collect`.
* `execute`: the write tool (idempotent, savepoint); outcomes: confirmed, pending approval
  (the HITL wait is persisted in `pending` and resolved by the approval hook), slot gone
  (re-propose), emergency reason (pause), failure (one retry, then a human handoff).
Every reply text is a fixed template from `templates.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from sqlalchemy import select

from netracare.agents.booking import templates as tpl
from netracare.agents.booking import tools as bt
from netracare.agents.booking.confirm import is_explicit_yes, is_stop
from netracare.agents.booking.nlu import choose_option, is_list_request, understand
from netracare.agents.booking.state import AgentRuntime, BookingState, fresh_flow
from netracare.agents.specialist import SpecialistRouter
from netracare.db.models import Doctor, Specialty
from netracare.db.session import begin_write
from netracare.domain.enums import AgentStep, AgentTurnKind, AppointmentType, HandoffReason
from netracare.domain.relative_dates import (
    explain_unavailable,
    format_day,
    resolve_dates,
    resolve_time,
)
from netracare.errors import AppError
from netracare.llm.schemas import BookingTurn
from netracare.log import get_logger
from netracare.observability.tracing import traced
from netracare.rag.doctor_names import DoctorName, match_doctor
from netracare.services.booking_service import BookingService

log = get_logger(__name__)

ACTIVE_STEPS = (AgentStep.COLLECT.value, AgentStep.PROPOSE.value, AgentStep.READBACK.value)
SLOT_GONE = ("slot_unavailable", "slot_taken", "slot_not_open", "slots_unavailable")
_CONTINUE = (
    "continue booking",
    "continue",
    "i'm ok",
    "im ok",
    "i am ok",
    "i'm safe",
    "i am safe",
    "im safe",
    "i've called",
    "i have called",
    "called them",
    "resume",
)


def wants_resume(text: str) -> bool:
    t = text.lower().strip()
    return any(p in t for p in _CONTINUE)


# -- helpers -----------------------------------------------------------------------------------


def _rt(runtime: Runtime[AgentRuntime]) -> AgentRuntime:
    return runtime.context


def _tool_ctx(rt: AgentRuntime) -> bt.ToolContext:
    ctx = bt.ToolContext(rt.session, rt.clock, rt.settings, rt.actor, rt.thread_id, rt.llm)
    ctx.calls = rt.tool_calls  # shared list: every tool call of the turn, in order
    return ctx


def _today(rt: AgentRuntime) -> date:
    return rt.clock.now_utc().astimezone(rt.settings.tz).date()


def _reply(kind: AgentTurnKind, text: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind.value, "text": text, **extra}


def _turn(state: BookingState) -> BookingTurn | None:
    raw = state.get("nlu")
    return BookingTurn.model_validate(raw) if raw else None


def _summary(state: BookingState) -> str:
    parts = [f"action={state.get('action', 'none')}", f"step={state.get('step', 'collect')}"]
    for i, o in enumerate(state.get("options") or [], start=1):
        parts.append(f"option {i}: {o['label']}")
    chosen = state.get("chosen")
    if chosen:
        parts.append(f"read-back shown for: {chosen['label']}")
    for c in state.get("appointment_choices") or []:
        parts.append(f"appointment {c['n']}: {c['label']}")
    return "\n".join(parts)


def _doctors(rt: AgentRuntime) -> list[dict[str, Any]]:
    rows = rt.session.execute(
        select(Doctor.id, Doctor.code, Doctor.full_name, Specialty.code)
        .join(Specialty, Doctor.specialty_id == Specialty.id)
        .where(Doctor.is_active)
        .order_by(Doctor.code)
    ).all()
    return [{"id": r[0], "code": r[1], "name": r[2], "specialty": r[3]} for r in rows]


def _specialty_name(rt: AgentRuntime, code: str | None) -> str:
    if not code:
        return "clinic"
    name = rt.session.scalar(select(Specialty.name).where(Specialty.code == code))
    return name or code.title()


# -- nodes -------------------------------------------------------------------------------------


def ingest(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    rt = _rt(runtime)
    out: dict[str, Any] = {
        "turn": state.get("turn", 0) + 1,
        "nlu": None,
        "reply": {},
        "expired_note": False,
        "resumed": False,
        "out_of_hours": False,
    }
    if not state.get("step"):
        out.update(fresh_flow())
        out.update(pending=state.get("pending") or [], writes=state.get("writes") or [], flows=0)
    now = rt.clock.now_utc()
    stale = state.get("updated_at") and now - datetime.fromisoformat(
        str(state["updated_at"])
    ) > timedelta(hours=rt.settings.agent_flow_expiry_hours)
    if stale and state.get("step") in ACTIVE_STEPS:
        out.update(fresh_flow())
        out["expired_note"] = True
    out["updated_at"] = now.isoformat()
    if state.get("event"):
        return out
    text = state.get("input", "")
    step = out.get("step", state.get("step"))
    if step == AgentStep.PAUSED_SAFETY.value and wants_resume(text):
        out["step"] = state.get("step_before_pause") or AgentStep.COLLECT.value
        out["resumed"] = True
        return out
    deterministic = text.startswith("option:") or (
        step == AgentStep.READBACK.value and (is_explicit_yes(text) or is_stop(text))
    )
    if not deterministic:
        merged = {**state, **out}
        turn, ids, cost = understand(
            rt.llm,
            text,
            _summary(merged),  # type: ignore[arg-type]
            current_action=str(merged.get("action", "none")),
            actor_user_id=rt.actor.id,
            redact_names=rt.redact_names,
        )
        rt.llm_call_ids += ids
        rt.cost_usd += cost
        out["nlu"] = turn.model_dump()
    return out


def route(state: BookingState) -> str:
    if state.get("event"):
        return "decided"
    if state.get("resumed"):
        return "resume"
    text = state.get("input", "")
    turn = _turn(state)
    if is_stop(text) or (turn and turn.reply_kind == "stop"):
        return "abort"
    step = state.get("step")
    wants_list = (turn is not None and turn.action == "list") or is_list_request(text)
    if wants_list and step != AgentStep.READBACK.value:
        return "list"
    if step == AgentStep.READBACK.value:
        return "confirm"
    if step == AgentStep.PROPOSE.value:
        return "choose"
    return "collect"


def abort(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    return {
        **fresh_flow(),
        "step": AgentStep.ABORTED.value,
        "reply": _reply(AgentTurnKind.INFO, tpl.ABORTED),
    }


def resume(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    if state.get("step") == AgentStep.READBACK.value and state.get("chosen"):
        return {
            "reply": _reply(
                AgentTurnKind.READBACK,
                f"{tpl.RESUMED} {tpl.REPEAT_READBACK}",
                readback=state.get("reply", {}).get("readback"),
            )
        }
    if state.get("step") == AgentStep.PROPOSE.value and state.get("options"):
        return {
            "reply": _reply(
                AgentTurnKind.OPTIONS, f"{tpl.RESUMED} {tpl.PICK_AGAIN}", options=state["options"]
            )
        }
    return {"reply": _reply(AgentTurnKind.QUESTION, f"{tpl.RESUMED} {tpl.ASK_DATES}")}


def list_appointments(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    rt = _rt(runtime)
    r = bt.get_my_appointments(_tool_ctx(rt))
    items = r.data.get("appointments", []) if r.ok else []
    if not items:
        text = tpl.LIST_EMPTY
    else:
        lines = [
            f"- {tpl.when(a['start_local'])}: {a['doctor_name']} ({a['status'].replace('_', ' ')})"
            for a in items
        ]
        text = tpl.LIST_ITEMS.format(items="\n".join(lines))
    # Listing ends the flow unless a booking/change was already in progress.
    done = state.get("action") in (None, "none", "list") or state.get("step") not in ACTIVE_STEPS
    upd: dict[str, Any] = {"reply": _reply(AgentTurnKind.INFO, text)}
    if done:
        upd.update(fresh_flow())
        upd["step"] = AgentStep.DONE.value
    return upd


def _apply_turn(state: BookingState, rt: AgentRuntime) -> dict[str, Any]:
    """Merge the slots this turn mentions into the flow (later turns override earlier ones)."""
    turn = _turn(state)
    text = state.get("input", "")
    upd: dict[str, Any] = {}
    if turn is None:
        return upd
    if turn.action in ("book", "reschedule", "cancel") and state.get("action") in (None, "none"):
        upd["action"] = turn.action
    if turn.appt_type:
        upd["appt_type"] = turn.appt_type
    if turn.concern_phrase:
        upd["concern"] = turn.concern_phrase
    today = _today(rt)
    phrase = turn.date_phrase or (text if turn.reply_kind in ("change", "answer") else None)
    dates = (
        resolve_dates(phrase, today, search_days=rt.settings.agent_search_days) if phrase else None
    )
    if dates:
        upd.update(
            date_from=dates.start.isoformat(),
            date_to=dates.end.isoformat(),
            date_label=dates.label,
            options=[],
            chosen=None,
        )
    tphrase = turn.time_phrase or (text if turn.reply_kind in ("change", "answer") else None)
    window = resolve_time(tphrase) if tphrase else None
    if window:
        upd.update(
            time_from=f"{window.start:%H:%M}",
            time_to=f"{window.end:%H:%M}",
            time_label=window.label,
            options=[],
            chosen=None,
            out_of_hours=window.out_of_hours,
        )
    if turn.doctor_mention or turn.specialty_hint:
        upd.update(_target(rt, state, turn, text))
        upd.update(options=[], chosen=None)
    return upd


def _target(rt: AgentRuntime, state: BookingState, turn: BookingTurn, text: str) -> dict[str, Any]:
    doctors = _doctors(rt)
    if turn.doctor_mention:
        m = match_doctor(turn.doctor_mention, [DoctorName(d["code"], d["name"]) for d in doctors])
        if m:
            d = next(x for x in doctors if x["code"] == m.code)
            return {"doctor_id": d["id"], "doctor_name": d["name"], "specialty": d["specialty"]}
    code = turn.specialty_hint
    if code:
        return {"doctor_id": None, "doctor_name": None, "specialty": code}
    return {}


def _resolve_patient(state: BookingState, rt: AgentRuntime) -> dict[str, Any] | str:
    """The patient this flow is for, or a question to ask."""
    if state.get("patient_id"):
        return {}
    choices = rt.patients
    if not choices:
        return tpl.SIGN_IN
    if len(choices) == 1:
        p = choices[0]
        return {"patient_id": p.id, "patient_name": p.name, "patient_is_minor": p.is_minor}
    turn = _turn(state)
    hint = ((turn.patient_mention if turn else None) or state.get("input", "")).lower()
    pick = None
    if any(w in hint.split() for w in ("me", "myself", "mine", "i")):
        pick = next((p for p in choices if p.is_self), None)
    if pick is None:
        named = [
            p
            for p in choices
            if any(part.lower() in hint for part in p.name.split() if len(part) > 2)
        ]
        if len(named) == 1:
            pick = named[0]
    if pick is None and any(w in hint for w in ("son", "daughter", "child", "kid", "baby")):
        minors = [p for p in choices if p.is_minor]
        if len(minors) == 1:
            pick = minors[0]
    if pick is None:
        return tpl.ASK_PATIENT.format(names=", ".join(p.name for p in choices) + "?")
    return {"patient_id": pick.id, "patient_name": pick.name, "patient_is_minor": pick.is_minor}


def _resolve_appointment(state: BookingState, rt: AgentRuntime) -> dict[str, Any] | str:
    if state.get("appointment"):
        return {}
    r = bt.get_my_appointments(_tool_ctx(rt), patient_id=state.get("patient_id"))
    items = r.data.get("appointments", []) if r.ok else []
    if not items:
        return tpl.NO_APPOINTMENTS
    labelled = [
        {**a, "n": i, "label": f"{tpl.short_when(a['start_local'])} with {a['doctor_name']}"}
        for i, a in enumerate(items, start=1)
    ]
    turn = _turn(state)
    text = (state.get("input", "") + " " + ((turn.appointment_ref or "") if turn else "")).lower()
    pick = None
    if len(labelled) == 1:
        pick = labelled[0]
    else:
        if turn and turn.option_choice and 1 <= turn.option_choice <= len(labelled):
            pick = labelled[turn.option_choice - 1]
        if pick is None:
            by_doc = [
                a for a in labelled if any(p.lower() in text for p in a["doctor_name"].split()[1:])
            ]
            if len(by_doc) == 1:
                pick = by_doc[0]
        if pick is None:
            dr = resolve_dates(text, _today(rt))
            if dr:
                by_day = [
                    a
                    for a in labelled
                    if dr.start <= datetime.fromisoformat(a["start_local"]).date() <= dr.end
                ]
                if len(by_day) == 1:
                    pick = by_day[0]
    if pick is None:
        items_text = "\n".join(f"{a['n']}. {a['label']}" for a in labelled)
        return {
            "_ask": tpl.ASK_APPOINTMENT.format(items=items_text),
            "appointment_choices": labelled,
        }
    return {
        "appointment": pick,
        "doctor_id": pick["doctor_id"],
        "doctor_name": pick["doctor_name"],
        "specialty": pick.get("specialty"),
        "appt_type": pick["type"],
        "appointment_choices": [],
    }


def collect(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    rt = _rt(runtime)
    upd = _apply_turn(state, rt)
    merged: dict[str, Any] = {**state, **upd}
    if merged.get("action") in (None, "none"):
        merged["action"] = upd["action"] = "book"
    prefix = tpl.EXPIRED if state.get("expired_note") else ""

    def ask(text: str, **extra: Any) -> dict[str, Any]:
        return {
            **upd,
            **extra,
            "step": AgentStep.COLLECT.value,
            "reply": _reply(AgentTurnKind.QUESTION, prefix + text),
        }

    who = _resolve_patient(merged, rt)  # type: ignore[arg-type]
    if isinstance(who, str):
        return ask(who)
    upd.update(who)
    merged.update(who)

    if merged["action"] in ("reschedule", "cancel"):
        appt = _resolve_appointment(merged, rt)  # type: ignore[arg-type]
        if isinstance(appt, str):
            return {
                **upd,
                **fresh_flow(),
                "step": AgentStep.DONE.value,
                "reply": _reply(AgentTurnKind.INFO, prefix + appt),
            }
        if "_ask" in appt:
            return ask(appt.pop("_ask"), **appt)
        upd.update(appt)
        merged.update(appt)
        if merged["action"] == "cancel":
            return {**upd, **_readback(merged, rt, None)}  # type: ignore[arg-type]

    if not merged.get("doctor_id") and not merged.get("specialty"):
        if merged.get("patient_is_minor"):
            upd["specialty"] = merged["specialty"] = "PEDIATRIC"
        elif merged.get("concern"):
            routed = SpecialistRouter(lambda code: _doctor_for(rt, code)).route(
                str(merged["concern"])
            )
            if routed.specialty:
                upd["specialty"] = merged["specialty"] = routed.specialty
        if not merged.get("specialty"):
            return ask(tpl.ASK_TARGET)

    if not merged.get("date_from"):
        if not merged.get("asked_dates"):
            return ask(tpl.ASK_DATES, asked_dates=True)
        today = _today(rt)  # asked once already: the earliest dates
        upd.update(
            date_from=today.isoformat(),
            date_to=(today + timedelta(days=rt.settings.agent_search_days - 1)).isoformat(),
            date_label="the next two weeks",
        )
        merged.update(upd)
    if upd.get("out_of_hours"):
        return ask(tpl.OUT_OF_HOURS, time_from=None, time_to=None, time_label=None)
    day_from = date.fromisoformat(str(merged["date_from"]))
    day_to = date.fromisoformat(str(merged["date_to"]))
    if day_from == day_to:
        why = explain_unavailable(day_from, _today(rt), rt.holidays)
        if why:
            nxt = _next_working_days(rt, max(day_from, _today(rt)))
            return ask(
                tpl.CLOSED_DAY.format(reason=why, suggestions=" or ".join(map(format_day, nxt))),
                date_from=None,
                date_to=None,
                date_label=None,
            )
    return {**upd, **_propose(merged, rt)}


def _doctor_for(rt: AgentRuntime, code: str) -> tuple[str, str] | None:
    docs = [d for d in _doctors(rt) if d["specialty"] == code]
    return (docs[0]["id"], docs[0]["name"]) if docs else None


def _next_working_days(rt: AgentRuntime, start: date, n: int = 2) -> list[date]:
    out: list[date] = []
    d = start + timedelta(days=1)
    while len(out) < n:
        if d.weekday() != 6 and d not in rt.holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def _spread(options: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Earliest first, then options at least an hour apart (so the patient gets a real choice)."""
    picked: list[dict[str, Any]] = []
    for o in options:
        start = datetime.fromisoformat(o["start_local"])
        if all(
            abs((start - datetime.fromisoformat(p["start_local"])).total_seconds()) >= 3600
            for p in picked
        ):
            picked.append(o)
        if len(picked) == n:
            break
    for o in options:  # top up if the range is too narrow for the spacing
        if len(picked) == n:
            break
        if o not in picked:
            picked.append(o)
    return sorted(picked, key=lambda o: o["start_local"])


def _propose(state: dict[str, Any], rt: AgentRuntime, prefix: str = "") -> dict[str, Any]:
    appt_type = AppointmentType(state.get("appt_type") or "routine")
    r = bt.get_availability(
        _tool_ctx(rt),
        doctor_id=state.get("doctor_id"),
        specialty=None if state.get("doctor_id") else state.get("specialty"),
        from_date=state["date_from"],
        to_date=state["date_to"],
        appt_type=appt_type,
    )
    found = r.data.get("options", []) if r.ok else []
    if state.get("action") == "reschedule" and state.get("appointment"):
        found = [o for o in found if o["start_local"] != state["appointment"]["start_local"]]
    if state.get("time_from"):
        lo, hi = time.fromisoformat(state["time_from"]), time.fromisoformat(state["time_to"])
        found = [o for o in found if lo <= datetime.fromisoformat(o["start_local"]).time() <= hi]
    context = f" for {state['date_label']}" if state.get("date_label") else ""
    if state.get("time_label"):
        context += f" ({state['time_label']})"
    if not found:
        return {
            "step": AgentStep.COLLECT.value,
            "options": [],
            "date_from": None,
            "date_to": None,
            "reply": _reply(
                AgentTurnKind.QUESTION, prefix + tpl.NO_OPTIONS.format(context=context)
            ),
        }
    options = []
    for o in _spread(found, rt.settings.agent_options_count):
        options.append(
            {
                "id": uuid.uuid4().hex[:8],
                "label": f"{tpl.short_when(o['start_local'])} · {o['doctor_name']}",
                **o,
            }
        )
    text = prefix + tpl.OPTIONS_INTRO.format(context=context)
    return {
        "step": AgentStep.PROPOSE.value,
        "options": options,
        "proposal_id": None,
        "proposed_at": rt.clock.now_utc().isoformat(),
        "chosen": None,
        "reply": _reply(AgentTurnKind.OPTIONS, text, options=options),
    }


def choose(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    rt = _rt(runtime)
    turn = _turn(state)
    text = state.get("input", "")
    idx = choose_option(text, turn, state.get("options") or [])
    if (
        idx is None
        and turn
        and (
            turn.reply_kind == "change"
            or turn.date_phrase
            or turn.time_phrase
            or turn.doctor_mention
            or turn.specialty_hint
        )
    ):
        return collect(state, runtime)
    if idx is None:
        return {
            "reply": _reply(
                AgentTurnKind.OPTIONS, tpl.PICK_AGAIN, options=state.get("options") or []
            )
        }
    return _readback(state, rt, state["options"][idx])


def _need(option: dict[str, Any] | None) -> dict[str, Any]:
    if option is None:  # a read-back of a booking/reschedule always has a chosen option
        raise ValueError("no option chosen")
    return option


def _readback(
    state: BookingState, rt: AgentRuntime, option: dict[str, Any] | None
) -> dict[str, Any]:
    action = state.get("action")
    appt_type = str(state.get("appt_type") or "routine")
    type_label = tpl.TYPE_LABELS.get(appt_type, appt_type)
    reasons: list[str] = []
    try:
        svc = BookingService(rt.session, rt.clock, rt.settings, None)
        if action == "cancel":
            a = state["appointment"] or {}
            start = datetime.fromisoformat(a["start_local"])
            within = start - rt.clock.now_utc().astimezone(rt.settings.tz) < timedelta(hours=24)
            reasons = ["within_24h_change"] if within and a.get("status") == "confirmed" else []
        else:
            # Same rule as BookingService.reschedule: a change is judged by the *current*
            # appointment's start (moving tomorrow's visit is a <24 h change).
            start = (
                datetime.fromisoformat((state.get("appointment") or {})["start_local"])
                if action == "reschedule"
                else datetime.fromisoformat(_need(option)["start_local"])
            )
            reasons = svc.preview_approval(
                rt.actor,
                str(state["patient_id"]),
                AppointmentType(appt_type),
                start,
                is_change=action == "reschedule",
            )
            if state.get("concern") and "post_red_flag" not in reasons:
                from netracare.guardrails.redflag_rules import detect, level_for

                lvl = level_for(detect(str(state["concern"])))
                if lvl is not None:
                    reasons.append("post_red_flag")
    except AppError as exc:
        log.info("agent.preview_failed", code=exc.code)
    note = tpl.approval_note(reasons)
    patient = state.get("patient_name") or "you"
    if action == "cancel":
        a = state["appointment"] or {}
        text = tpl.READBACK_CANCEL.format(
            type=type_label.lower(),
            doctor=a.get("doctor_name"),
            when=tpl.when(a["start_local"]),
            patient=patient,
            approval=note,
        )
    elif action == "reschedule":
        a = state["appointment"] or {}
        option = _need(option)
        text = tpl.READBACK_RESCHEDULE.format(
            type=type_label.lower(),
            doctor=option["doctor_name"],
            old_when=tpl.when(a["start_local"]),
            when=tpl.when(option["start_local"]),
            patient=patient,
            approval=note,
        )
    else:
        option = _need(option)
        text = tpl.READBACK.format(
            type=type_label,
            doctor=option["doctor_name"],
            specialty=_specialty_name(rt, option.get("specialty")),
            when=tpl.when(option["start_local"]),
            patient=patient,
            approval=note,
            verb="book",
        )
    readback = {"text": text, "needs_approval": bool(reasons), "reasons": reasons}
    return {
        "step": AgentStep.READBACK.value,
        "chosen": option,
        "proposal_id": uuid.uuid4().hex[:12],
        "approval_reasons": reasons,
        "readback_turn": state.get("turn", 0),
        "reply": _reply(AgentTurnKind.READBACK, text, readback=readback),
    }


def confirm(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    rt = _rt(runtime)
    text = state.get("input", "")
    if is_explicit_yes(text):  # code decides; the LLM's opinion is never used for writes
        proposed = state.get("proposed_at")
        ttl = timedelta(minutes=rt.settings.agent_proposal_ttl_minutes)
        if (
            state.get("action") != "cancel"
            and proposed
            and rt.clock.now_utc() - datetime.fromisoformat(proposed) > ttl
        ):
            return _propose(dict(state), rt, prefix=tpl.SLOT_RECHECK + " ")
        return execute(state, runtime)
    turn = _turn(state)
    if turn and (
        turn.reply_kind in ("change", "no")
        or turn.date_phrase
        or turn.time_phrase
        or turn.doctor_mention
    ):
        if turn.reply_kind == "no" and not (
            turn.date_phrase or turn.time_phrase or turn.doctor_mention
        ):
            if state.get("action") == "cancel":
                return abort(state, runtime)
            return {
                "step": AgentStep.PROPOSE.value,
                "chosen": None,
                "reply": _reply(
                    AgentTurnKind.OPTIONS, tpl.PICK_AGAIN, options=state.get("options") or []
                ),
            }
        return collect(state, runtime)
    return {
        "reply": _reply(
            AgentTurnKind.READBACK,
            tpl.REPEAT_READBACK,
            readback=state.get("reply", {}).get("readback"),
        )
    }


@traced(
    "agent.execute",
    tags=["agent", "booking"],
    inputs=lambda state, runtime: {"action": state.get("action")},
    outputs=lambda r: {"reply_kind": r.get("reply", {}).get("kind")},
)
def execute(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    rt = _rt(runtime)
    ctx = _tool_ctx(rt)
    action = state.get("action")
    option = state.get("chosen") or {}
    pid = str(state.get("proposal_id"))
    write = {
        "tool": f"{action}_appointment",
        "turn": state.get("turn", 0),
        "readback_turn": state.get("readback_turn", -1),
        "yes_text": state.get("input", ""),
    }

    # A fresh write transaction right before the write (BEGIN IMMEDIATE): earlier reads in
    # this turn can't make SQLite refuse the lock upgrade, and the request lock is not held
    # while the LLM ran.
    if not rt.session.in_nested_transaction():
        rt.session.commit()
        begin_write(rt.session)

    def call() -> bt.ToolResult:
        if action == "cancel":
            return bt.cancel_appointment(
                ctx,
                appointment_id=state["appointment"]["appointment_id"],  # type: ignore[index]
                reason="cancelled via chat",
            )
        if action == "reschedule":
            return bt.reschedule_appointment(
                ctx,
                appointment_id=state["appointment"]["appointment_id"],  # type: ignore[index]
                slot_ids=option["slot_ids"],
                proposal_id=pid,
                reason="rescheduled via chat",
            )
        return bt.book_appointment(
            ctx,
            patient_id=state["patient_id"],
            doctor_id=option["doctor_id"],
            slot_ids=option["slot_ids"],
            appt_type=state.get("appt_type") or "routine",
            reason=state.get("concern"),
            proposal_id=pid,
        )

    result = call()
    if not result.ok and result.error == "internal":
        result = call()  # one retry: idempotent, the savepoint rolled the first attempt back
    writes = [*(state.get("writes") or []), {**write, "ok": result.ok, "error": result.error}]
    if result.ok:
        return _outcome(state, rt, result, writes)
    if result.error in SLOT_GONE:
        return {"writes": writes, **_propose(dict(state), rt, prefix=tpl.SLOT_GONE + " ")}
    if result.error == "emergency_detected":
        script = result.details.get("message") or ""
        return {
            "writes": writes,
            "step": AgentStep.PAUSED_SAFETY.value,
            "step_before_pause": AgentStep.COLLECT.value,
            "reply": _reply(
                AgentTurnKind.INFO,
                f"{tpl.EMERGENCY_REASON}\n\n{script}\n\n{tpl.PAUSED}",
                safety={"level": "emergency", "alert_id": result.details.get("alert_id")},
            ),
        }
    failures = int(state.get("failures", 0)) + 1
    if failures >= rt.settings.agent_max_tool_failures or result.error == "internal":
        if rt.chat_session is not None:
            bt.escalate_to_human(ctx, rt.chat_session, reason=HandoffReason.USER_REQUEST.value)
        return {
            **fresh_flow(),
            "writes": writes,
            "step": AgentStep.ABORTED.value,
            "reply": _reply(AgentTurnKind.ERROR, tpl.ESCALATED),
        }
    msg = result.message or tpl.TOOL_FAILED
    return {
        "writes": writes,
        "failures": failures,
        "step": AgentStep.PROPOSE.value,
        "reply": _reply(
            AgentTurnKind.ERROR,
            f"Sorry, I couldn't do that: {msg}. {tpl.PICK_AGAIN}",
            options=state.get("options") or [],
        ),
    }


def _outcome(
    state: BookingState, rt: AgentRuntime, result: bt.ToolResult, writes: list[dict[str, Any]]
) -> dict[str, Any]:
    data = result.data
    action = state.get("action")
    option = state.get("chosen") or {}
    doctor = option.get("doctor_name") or (state.get("appointment") or {}).get("doctor_name")
    type_label = tpl.TYPE_LABELS.get(
        str(state.get("appt_type") or "routine"), "appointment"
    ).lower()
    pending = list(state.get("pending") or [])
    base = {**fresh_flow(), "writes": writes, "step": AgentStep.DONE.value}
    if data.get("needs_approval"):
        what_when = tpl.when(data["start_local"])
        what = (
            f"{action} with {doctor} on {what_when}"
            if action != "cancel"
            else f"cancel the visit on {what_when}"
        )
        if rt.chat_session is not None:  # link the request to this conversation (HITL resume)
            from netracare.db.models import ApprovalRequest

            req = rt.session.get(ApprovalRequest, data["approval_request_id"])
            if req is not None:
                req.source, req.chat_session_id = "chat", rt.chat_session.id
        pending.append(
            {
                "approval_request_id": data["approval_request_id"],
                "appointment_id": data["appointment_id"],
                "what": what,
                "action": action,
                "notified_overdue": False,
            }
        )
        return {
            **base,
            "pending": pending,
            "reply": _reply(
                AgentTurnKind.PENDING,
                tpl.PENDING.format(what=what),
                approval_request_id=data["approval_request_id"],
            ),
        }
    if action == "cancel":
        text = tpl.CANCELLED.format(doctor=doctor, when=tpl.when(data["start_local"]))
    elif action == "reschedule":
        text = tpl.RESCHEDULED.format(doctor=doctor, when=tpl.when(data["start_local"]))
    else:
        text = tpl.CONFIRMED.format(
            type=type_label, doctor=doctor, when=tpl.when(data["start_local"])
        )
    return {
        **base,
        "pending": pending,
        "reply": _reply(AgentTurnKind.CONFIRMED, text, appointment_id=data["appointment_id"]),
    }


def decided(state: BookingState, runtime: Runtime[AgentRuntime]) -> dict[str, Any]:
    """The front desk decided a pending request (HITL resume)."""
    ev = state.get("event") or {}
    pending = [
        p for p in (state.get("pending") or []) if p["approval_request_id"] != ev.get("request_id")
    ]
    item = next(
        (
            p
            for p in (state.get("pending") or [])
            if p["approval_request_id"] == ev.get("request_id")
        ),
        None,
    )
    what = item["what"] if item else "your request"
    decision = ev.get("decision")
    if decision == "changed":
        text = tpl.DECIDED_CHANGED.format(
            when=tpl.when(ev["start_local"]), doctor=ev.get("doctor_name")
        )
    elif decision == "rejected":
        note = f" Note from the front desk: {ev['note']}" if ev.get("note") else ""
        text = tpl.DECIDED_REJECTED.format(what=what, note=note)
    else:
        text = tpl.DECIDED_APPROVED.format(what=what[0].upper() + what[1:])
    return {
        "pending": pending,
        "event": None,
        "reply": _reply(AgentTurnKind.DECIDED, text, decision=decision),
    }


def build_graph(checkpointer: Any) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(BookingState, context_schema=AgentRuntime)
    for name, fn in (
        ("ingest", ingest),
        ("collect", collect),
        ("choose", choose),
        ("confirm", confirm),
        ("list", list_appointments),
        ("abort", abort),
        ("decided", decided),
        ("resume", resume),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_conditional_edges(
        "ingest", route, ["collect", "choose", "confirm", "list", "abort", "decided", "resume"]
    )
    for name in ("collect", "choose", "confirm", "list", "abort", "decided", "resume"):
        g.add_edge(name, END)
    return g.compile(checkpointer=checkpointer)
