"""Front-desk chat handoffs (front desk and admin only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from netracare.api.deps import ClockDep, DbSession
from netracare.api.routers.chat import message_out
from netracare.api.schemas.chat import (
    HandoffCloseIn,
    HandoffConversationOut,
    HandoffList,
    HandoffOut,
    HandoffVersionIn,
)
from netracare.api.security import StaffUserDep
from netracare.db.models import ChatHandoff, ChatSession, Patient
from netracare.domain.enums import HandoffStatus
from netracare.services.handoff_service import HandoffService

router = APIRouter(prefix="/chat/handoffs", tags=["chat"])


def handoff_out(db: DbSession, h: ChatHandoff) -> HandoffOut:
    chat = db.get(ChatSession, h.session_id)
    patient = db.get(Patient, chat.patient_id) if chat and chat.patient_id else None
    return HandoffOut(
        id=h.id,
        session_id=h.session_id,
        reason=h.reason,
        summary_redacted=h.summary_redacted,
        status=h.status,
        patient_id=patient.id if patient else None,
        patient_name=patient.full_name if patient else None,
        anonymous=chat is None or chat.user_id is None,
        claimed_by=h.claimed_by,
        claimed_at=h.claimed_at,
        closed_by=h.closed_by,
        closed_at=h.closed_at,
        close_note=h.close_note,
        created_at=h.created_at,
        version=h.version,
    )


@router.get("", response_model=HandoffList)
def list_handoffs(
    _: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    status: HandoffStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> HandoffList:
    svc = HandoffService(db, clock)
    items = svc.list_handoffs(status, limit)
    open_total = len(svc.list_handoffs(HandoffStatus.OPEN, 1000))
    return HandoffList(items=[handoff_out(db, h) for h in items], open_total=open_total)


@router.post("/{handoff_id}/claim", response_model=HandoffOut)
def claim(
    handoff_id: str, body: HandoffVersionIn, user: StaffUserDep, db: DbSession, clock: ClockDep
) -> HandoffOut:
    return handoff_out(db, HandoffService(db, clock).claim(user, handoff_id, body.version))


@router.post("/{handoff_id}/close", response_model=HandoffOut)
def close(
    handoff_id: str, body: HandoffCloseIn, user: StaffUserDep, db: DbSession, clock: ClockDep
) -> HandoffOut:
    return handoff_out(
        db, HandoffService(db, clock).close(user, handoff_id, body.note, body.version)
    )


@router.get("/{handoff_id}/conversation", response_model=HandoffConversationOut)
def conversation(
    handoff_id: str, _: StaffUserDep, db: DbSession, clock: ClockDep
) -> HandoffConversationOut:
    h, _chat, msgs = HandoffService(db, clock).conversation(handoff_id)
    return HandoffConversationOut(
        handoff=handoff_out(db, h), messages=[message_out(m) for m in msgs]
    )
