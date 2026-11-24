"""Patient chat (public: signed-in users own their sessions; anonymous ones use a token)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, status

from netracare.api.deps import ClockDep, DbSession, IndexDep, LLMDep, SettingsDep
from netracare.api.schemas.chat import (
    ChatMessageIn,
    ChatMessageOut,
    ChatSessionOut,
    ChatTurnOut,
    CitationOut,
    SuggestedActionOut,
)
from netracare.api.security import OptionalUserDep
from netracare.db.models import ChatMessage, ChatSession
from netracare.domain.actor import CurrentUser
from netracare.guardrails.scripts import CHAT_FOOTER
from netracare.rag.answer import Citation, disclaimers_for
from netracare.security.rate_limit import SlidingWindowLimiter
from netracare.services.chat_service import ChatService, ChatTurn

router = APIRouter(prefix="/chat/sessions", tags=["chat"])

ChatToken = Annotated[str | None, Header(alias="X-Chat-Token")]


def _citation_out(c: Citation) -> CitationOut:
    return CitationOut(
        n=c.n,
        chunk_id=c.chunk_id,
        slug=c.slug,
        title=c.title,
        version=c.version,
        effective_date=c.effective_date,
        heading_path=c.heading_path,
        doc_type=c.doc_type,
        excerpt=c.excerpt,
        is_current=c.is_current,
    )


def turn_out(t: ChatTurn) -> ChatTurnOut:
    return ChatTurnOut(
        reply=t.reply,
        intent=t.intent,
        mode=t.mode,
        citations=[_citation_out(c) for c in t.citations],
        disclaimers=t.disclaimers,
        footer=CHAT_FOOTER,
        safety=t.safety,
        handoff_id=t.handoff_id,
        handoff_offer=t.handoff_offer,
        suggested_action=SuggestedActionOut(**t.suggested_action) if t.suggested_action else None,
        session_status=t.session_status,
        message_id=t.message_id,
    )


def message_out(m: ChatMessage) -> ChatMessageOut:
    citations = [CitationOut(**c) for c in m.citations]
    return ChatMessageOut(
        id=m.id,
        seq=m.seq,
        role=m.role.value,
        text=m.text,
        intent=m.intent,
        mode=m.mode,
        citations=citations,
        disclaimers=disclaimers_for(citations) if citations else [],  # type: ignore[arg-type]
        safety=m.safety,
        created_at=m.created_at,
    )


def _session_out(
    chat: ChatSession, messages: list[ChatMessage], token: str | None = None
) -> ChatSessionOut:
    return ChatSessionOut(
        id=chat.id,
        status=chat.status,
        turns=chat.turns,
        created_at=chat.created_at,
        session_token=token,
        footer=CHAT_FOOTER,
        messages=[message_out(m) for m in messages],
    )


def _limit(request: Request, user: CurrentUser | None, clock: ClockDep) -> None:
    state = request.app.state
    if user is not None:
        limiter: SlidingWindowLimiter = state.chat_limiter
        limiter.hit(f"user:{user.id}", clock.now_utc())
    else:
        ip = request.client.host if request.client else "unknown"
        anon: SlidingWindowLimiter = state.chat_anon_limiter
        anon.hit(f"ip:{ip}", clock.now_utc())


def _svc(
    db: DbSession, clock: ClockDep, settings: SettingsDep, llm: LLMDep, index: IndexDep
) -> ChatService:
    return ChatService(db, clock, settings, llm, index)


@router.post("", response_model=ChatSessionOut, status_code=status.HTTP_201_CREATED)
def create_session(
    request: Request,
    user: OptionalUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    llm: LLMDep,
    index: IndexDep,
) -> ChatSessionOut:
    _limit(request, user, clock)
    chat, token = _svc(db, clock, settings, llm, index).create_session(user)
    return _session_out(chat, [], token)


@router.get("/{session_id}", response_model=ChatSessionOut)
def get_session(
    session_id: str,
    user: OptionalUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    llm: LLMDep,
    index: IndexDep,
    x_chat_token: ChatToken = None,
) -> ChatSessionOut:
    svc = _svc(db, clock, settings, llm, index)
    chat = svc.get_session(session_id, user, x_chat_token)
    return _session_out(chat, svc.messages(chat))


@router.post("/{session_id}/messages", response_model=ChatTurnOut)
def post_message(
    session_id: str,
    body: ChatMessageIn,
    request: Request,
    user: OptionalUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    llm: LLMDep,
    index: IndexDep,
    x_chat_token: ChatToken = None,
) -> ChatTurnOut:
    _limit(request, user, clock)
    turn = _svc(db, clock, settings, llm, index).post_message(
        session_id, body.text, actor=user, anon_token=x_chat_token
    )
    return turn_out(turn)


@router.post("/{session_id}/handoff", response_model=ChatTurnOut)
def request_handoff(
    session_id: str,
    user: OptionalUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    llm: LLMDep,
    index: IndexDep,
    x_chat_token: ChatToken = None,
) -> ChatTurnOut:
    svc = _svc(db, clock, settings, llm, index)
    chat = svc.get_session(session_id, user, x_chat_token)
    return turn_out(svc.request_handoff(chat, user))
