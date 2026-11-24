"""Patient chat: one turn = layer-1 safety rules -> LLM router -> handler -> persisted reply.

    message ─► limits (length 422, ended session 409, turn limit, session budget -> no-LLM mode)
            ─► layer 1 (rules): emergency/urgent ─► TriageService (verbatim script + deduped alert)
                                medical / lab report ─► 5.3 refusal + section 6 routing
                                human request ─► handoff · empty / small talk / spam ─► fixed reply
            ─► layer 2 (LLM router + follow-up rewrite; keyword fallback if the LLM is down)
                 symptom / concern / "emergency" ─► TriageService (rules + LLM classifier) first
                 booking ─► specialist routing (one clarifying question) + Book page action
                 faq / pricing / doctor ─► hybrid retrieval ─► confidence gate ─► cited answer
                 chit-chat / off-topic ─► fixed reply

Every reply that is not a cited KB answer is a fixed constant (guardrails/scripts.py). LLM
calls run with no DB transaction open (`outside_transaction`); all writes happen at the end.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.agents.router import RouteDecision, Router
from netracare.agents.router_rules import Layer1Decision, layer1
from netracare.agents.specialist import RoutingOutcome, SpecialistRouter
from netracare.config import Settings
from netracare.db.models import ChatHandoff, ChatMessage, ChatSession, Patient
from netracare.db.session import outside_transaction
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import (
    ChatIntent,
    ChatRole,
    ChatSessionStatus,
    HandoffReason,
    RedFlagLevel,
    Role,
)
from netracare.errors import ConflictError, NotFoundError, ValidationAppError
from netracare.guardrails import scripts
from netracare.guardrails.redflag_rules import looks_clinical
from netracare.llm.client import LLMClient
from netracare.log import get_logger
from netracare.observability.tracing import traced
from netracare.rag.answer import Answer, AnswerGenerator, Citation, disclaimers_for
from netracare.rag.retriever import RetrievalFilters, RetrievalIndex, RetrievalResult
from netracare.services.directory_service import DirectoryService
from netracare.services.handoff_service import HandoffService
from netracare.services.triage_service import TriageResult, TriageService, alert_dedup_key

log = get_logger(__name__)

_NEGATIVE = re.compile(
    r"\b(?:useless|not helpful|unhelpful|you(?:'re| are) wrong|that'?s wrong|stupid|dumb"
    r"|you don'?t understand|not what i asked|annoying|waste of time|terrible|ugh+)\b",
    re.IGNORECASE,
)
FRUSTRATION_TURNS = 2
_CHIT_CHAT_REPLIES = {
    "empty": scripts.EMPTY_REPLY,
    "greeting": scripts.GREETING_REPLY,
    "thanks": scripts.THANKS_REPLY,
    "identity": scripts.IDENTITY_REPLY,
}
_RAG_INTENTS = (ChatIntent.FAQ_RAG, ChatIntent.PRICING, ChatIntent.DOCTOR_LOOKUP)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class ChatTurn:
    reply: str
    intent: ChatIntent
    mode: str  # llm | extractive | refused | script | fixed | rules
    citations: list[Citation] = field(default_factory=list)
    disclaimers: list[str] = field(default_factory=list)
    safety: dict[str, Any] = field(default_factory=dict)
    handoff_id: str | None = None
    handoff_offer: bool = False
    suggested_action: dict[str, Any] | None = None
    session_status: ChatSessionStatus = ChatSessionStatus.ACTIVE
    message_id: str | None = None
    llm_call_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    flags: list[str] = field(default_factory=list)


@dataclass
class _Ctx:
    chat: ChatSession
    actor: CurrentUser | None
    text: str
    history: str
    names: list[str]
    llm: LLMClient | None  # None when the session is over budget (no-LLM mode)
    pending: dict[str, Any] | None
    call_ids: list[str] = field(default_factory=list)
    cost: float = 0.0
    handoff: ChatHandoff | None = None
    new_pending: dict[str, Any] | None = None
    clear_pending: bool = False
    end_session: bool = False
    negative: bool = False
    flags: list[str] = field(default_factory=list)


def _turn_inputs(_: object, session_id: str, text: str, **kw: Any) -> dict[str, Any]:
    return {"chars": len(text), "anonymous": kw.get("actor") is None}


def _turn_outputs(t: ChatTurn) -> dict[str, Any]:
    return {
        "intent": t.intent.value,
        "mode": t.mode,
        "citations": [c.chunk_id for c in t.citations],
        "safety_level": t.safety.get("level"),
        "handoff": t.handoff_id is not None,
        "flags": t.flags,
        "cost_usd": round(t.cost_usd, 6),
    }


class ChatService:
    def __init__(
        self,
        session: Session,
        clock: Clock,
        settings: Settings,
        llm: LLMClient | None,
        index: RetrievalIndex,
    ) -> None:
        self.s = session
        self.clock = clock
        self.settings = settings
        self.llm = llm
        self.index = index
        self.triage = TriageService(session, clock, settings, llm)
        self.directory = DirectoryService(session, clock)
        self.handoffs = HandoffService(session, clock)

    # -- sessions -----------------------------------------------------------------------
    def create_session(self, actor: CurrentUser | None) -> tuple[ChatSession, str | None]:
        """A new conversation. Anonymous callers get a one-time token (only its hash is kept)."""
        now = self.clock.now_utc()
        token = None if actor else secrets.token_urlsafe(24)
        chat = ChatSession(
            user_id=actor.id if actor else None,
            patient_id=self._own_patient_id(actor),
            anon_token_hash=hash_token(token) if token else None,
            created_at=now,
            last_active_at=now,
        )
        self.s.add(chat)
        self.s.flush()
        return chat, token

    def get_session(
        self, session_id: str, actor: CurrentUser | None, anon_token: str | None
    ) -> ChatSession:
        """The caller's own session; anything else is 404 (no existence leak)."""
        chat = self.s.get(ChatSession, session_id)
        if chat is None:
            raise NotFoundError("chat_not_found", "conversation not found")
        if chat.user_id is not None:
            if actor is None or actor.id != chat.user_id:
                raise NotFoundError("chat_not_found", "conversation not found")
        elif not anon_token or chat.anon_token_hash != hash_token(anon_token):
            raise NotFoundError("chat_not_found", "conversation not found")
        return chat

    def messages(self, chat: ChatSession) -> list[ChatMessage]:
        return list(
            self.s.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == chat.id)
                .order_by(ChatMessage.seq)
            )
        )

    def request_handoff(self, chat: ChatSession, actor: CurrentUser | None) -> ChatTurn:
        """The "Talk to a person" button."""
        return self._handoff_turn(chat, actor, HandoffReason.USER_REQUEST)

    def _own_patient_id(self, actor: CurrentUser | None) -> str | None:
        if actor is None or actor.role is not Role.PATIENT:
            return None
        return self.s.scalar(select(Patient.id).where(Patient.user_id == actor.id))

    # -- one turn -------------------------------------------------------------------------
    @traced("chat.turn", tags=["chat"], inputs=_turn_inputs, outputs=_turn_outputs)
    def post_message(
        self,
        session_id: str,
        text: str,
        *,
        actor: CurrentUser | None,
        anon_token: str | None = None,
    ) -> ChatTurn:
        if len(text) > self.settings.chat_max_chars:
            raise ValidationAppError(
                "message_too_long",
                f"please keep it under {self.settings.chat_max_chars} characters",
            )
        chat = self.get_session(session_id, actor, anon_token)
        if chat.status is ChatSessionStatus.ENDED:
            raise ConflictError("session_ended", scripts.SESSION_ENDED)

        previous = self.messages(chat)
        if chat.turns >= self.settings.chat_max_turns:
            ctx = self._ctx(chat, actor, text, previous)
            ctx.end_session = True
            return self._finish(
                ctx, ChatTurn(scripts.TURN_LIMIT_END, ChatIntent.OFF_TOPIC, "fixed")
            )
        ctx = self._ctx(chat, actor, text, previous)
        prev_user = [m.text for m in previous if m.role is ChatRole.USER]
        d1 = layer1(text, prev_user)
        if d1.injection:
            log.warning("prompt_injection_suspected", patterns=list(d1.injection))
            ctx.flags.append("prompt_injection_suspected")
        # Counted here, written in _finish: nothing may be modified before the LLM calls.
        ctx.negative = bool(_NEGATIVE.search(text))

        turn = self._dispatch(ctx, d1)
        if (
            chat.negative_turns + int(ctx.negative) >= FRUSTRATION_TURNS
            and not any(f.startswith("handoff:") for f in ctx.flags)
            and turn.intent is not ChatIntent.EMERGENCY
        ):
            turn = self._frustration(ctx, turn)
        return self._finish(ctx, turn)

    def _ctx(
        self, chat: ChatSession, actor: CurrentUser | None, text: str, previous: list[ChatMessage]
    ) -> _Ctx:
        over_budget = float(chat.cost_usd) >= self.settings.conversation_budget_usd
        if over_budget:
            log.info("chat.session_budget_reached", session_id=chat.id)
        return _Ctx(
            chat=chat,
            actor=actor,
            text=text,
            history=self._history(previous),
            names=self.handoffs._names(chat),
            llm=None if over_budget else self.llm,
            pending=chat.pending_clarification,
            flags=["session_budget_reached"] if over_budget else [],
        )

    def _history(self, previous: list[ChatMessage]) -> str:
        n = self.settings.chat_history_turns * 2
        lines = []
        for m in previous[-n:] if n else []:
            who = "user" if m.role is ChatRole.USER else "assistant"
            text = re.sub(r"\s+", " ", m.text)[:400]
            lines.append(f"{who}: {text}")
        return "\n".join(lines)

    # -- dispatch ---------------------------------------------------------------------------
    def _dispatch(self, ctx: _Ctx, d1: Layer1Decision) -> ChatTurn:
        if d1.intent is ChatIntent.EMERGENCY:
            return self._triage_turn(ctx, ctx.text)
        if d1.intent is ChatIntent.MEDICAL_QUESTION:
            return self._medical(ctx, d1.topic or "your symptoms", None, None)
        if d1.intent is ChatIntent.HUMAN_HANDOFF:
            return self._handoff_turn(ctx.chat, ctx.actor, HandoffReason.USER_REQUEST, ctx)
        if d1.intent is ChatIntent.OFF_TOPIC and d1.reason == "spam":
            return self._spam(ctx)
        if d1.intent is ChatIntent.CHIT_CHAT:
            return ChatTurn(
                _CHIT_CHAT_REPLIES[d1.reply_key or "greeting"], ChatIntent.CHIT_CHAT, "fixed"
            )

        route = self._route(ctx)
        # A symptom, a concern or a suspected emergency gets the full Phase 2 triage first
        # (rules + LLM classifier): any escalation overrides the router's intent.
        symptom = looks_clinical(ctx.text) and (
            bool(route.concern_phrase)
            or route.intent in (ChatIntent.MEDICAL_QUESTION, ChatIntent.BOOKING)
        )
        if route.intent is ChatIntent.EMERGENCY or symptom:
            floor = RedFlagLevel.URGENT if route.intent is ChatIntent.EMERGENCY else None
            result = self._classify(ctx, ctx.text, floor)
            if result.level in (RedFlagLevel.EMERGENCY, RedFlagLevel.URGENT):
                return self._script_turn(ctx, result)
            if result.is_medical_question and route.intent is not ChatIntent.BOOKING:
                return self._medical(ctx, "your symptoms", route, result)

        if ctx.pending and route.intent not in (*_RAG_INTENTS, ChatIntent.CHIT_CHAT):
            return self._booking(ctx, route)
        ctx.clear_pending = True

        if route.intent is ChatIntent.BOOKING:
            return self._booking(ctx, route)
        if route.intent is ChatIntent.MEDICAL_QUESTION:
            return self._medical(ctx, "your symptoms", route, None)
        if route.intent is ChatIntent.HUMAN_HANDOFF:
            return self._handoff_turn(ctx.chat, ctx.actor, HandoffReason.USER_REQUEST, ctx)
        if route.intent is ChatIntent.CHIT_CHAT:
            return ChatTurn(scripts.GREETING_REPLY, ChatIntent.CHIT_CHAT, "fixed")
        if route.intent is ChatIntent.OFF_TOPIC:
            return ChatTurn(scripts.OFF_TOPIC_REPLY, ChatIntent.OFF_TOPIC, "fixed")
        return self._rag(ctx, route)

    def _route(self, ctx: _Ctx) -> RouteDecision:
        router = Router(ctx.llm, self.index.doctors)
        with outside_transaction(self.s):
            route = router.route(
                ctx.text,
                ctx.history,
                actor_user_id=ctx.actor.id if ctx.actor else None,
                redact_names=ctx.names,
            )
        ctx.call_ids += list(route.llm_call_ids)
        ctx.cost += route.cost_usd
        if route.source != "llm":
            ctx.flags.append(f"router:{route.source}")
        return route

    # -- safety -----------------------------------------------------------------------------
    def _classify(self, ctx: _Ctx, text: str, floor: RedFlagLevel | None = None) -> TriageResult:
        from netracare.guardrails.redflag_rules import detect

        owner = f"patient:{ctx.chat.patient_id}" if ctx.chat.patient_id else f"chat:{ctx.chat.id}"
        key = alert_dedup_key(owner, sorted({m.code for m in detect(text)}))
        triage = (
            self.triage
            if ctx.llm is self.llm
            else TriageService(self.s, self.clock, self.settings, ctx.llm)
        )
        return triage.classify(
            text,
            actor=ctx.actor,
            patient_id=ctx.chat.patient_id,
            dedup_key=key,
            chat_session_id=ctx.chat.id,
            min_level=floor,
        )

    def _triage_turn(self, ctx: _Ctx, text: str) -> ChatTurn:
        return self._script_turn(ctx, self._classify(ctx, text))

    def _script_turn(self, ctx: _Ctx, result: TriageResult) -> ChatTurn:
        """Emergency / urgent: the verbatim script; urgent also gets the routing sentence and a
        booking action (it will need staff approval). Emergencies offer no booking (5.1)."""
        ctx.clear_pending = True
        safety = {
            "level": result.level.value,
            "codes": result.codes,
            "alert_id": result.alert_id,
            "deduplicated": bool(result.meta.get("alert_deduplicated")),
        }
        if result.level is RedFlagLevel.EMERGENCY:
            return ChatTurn(result.message, ChatIntent.EMERGENCY, "script", safety=safety)
        outcome = self._specialist().route(ctx.text, codes=result.codes)
        reply = result.message
        action = None
        if outcome.sentence and outcome.kind != "clarify":
            reply = f"{reply}\n\n{outcome.sentence}"
            action = self._book_action(outcome)
        return ChatTurn(
            reply, ChatIntent.EMERGENCY, "script", safety=safety, suggested_action=action
        )

    def _medical(
        self,
        ctx: _Ctx,
        topic: str,
        route: RouteDecision | None,
        result: TriageResult | None,
    ) -> ChatTurn:
        """Section 5.3 refusal + a section 6 routing sentence (never advice)."""
        ctx.clear_pending = True
        outcome = self._specialist().route(
            ctx.text,
            codes=result.codes if result else (),
            llm_hint=route.specialty_hint if route else None,
            concern_phrase=route.concern_phrase if route else None,
        )
        sentence = outcome.sentence if outcome.kind not in ("clarify", "none") else None
        sentence = sentence or scripts.ROUTING_TEMPLATES["general"]
        reply = scripts.MEDICAL_REFUSAL.format(topic=topic, routing_sentence=sentence)
        action = self._book_action(outcome) or self._book_action(self._specialist_general())
        return ChatTurn(reply, ChatIntent.MEDICAL_QUESTION, "script", suggested_action=action)

    # -- booking (Phase 4 agent handoff) -----------------------------------------------------
    def _booking(self, ctx: _Ctx, route: RouteDecision) -> ChatTurn:
        outcome = self._specialist().route(
            ctx.text,
            llm_hint=route.specialty_hint,
            concern_phrase=route.concern_phrase,
            pending=ctx.pending,
        )
        if outcome.kind == "clarify":
            ctx.new_pending = outcome.pending
            return ChatTurn(outcome.sentence or "", ChatIntent.CLARIFY, "fixed")
        ctx.clear_pending = True
        action = self._book_action(outcome)
        if outcome.sentence:
            reply = f"{outcome.sentence} {scripts.BOOKING_HANDOFF}"
        else:
            reply = scripts.BOOKING_HANDOFF_GENERIC
            action = {"type": "book", "specialty": None, "doctor_id": None, "doctor_name": None}
        return ChatTurn(reply, ChatIntent.BOOKING, "fixed", suggested_action=action)

    def _book_action(self, outcome: RoutingOutcome) -> dict[str, Any] | None:
        if outcome.doctor_id is None:
            return None
        return {
            "type": "book",
            "specialty": outcome.specialty,
            "doctor_id": outcome.doctor_id,
            "doctor_name": outcome.doctor_name,
        }

    def _specialist(self) -> SpecialistRouter:
        def doctor_for(code: str) -> tuple[str, str] | None:
            docs = self.directory.list_doctors(code)
            return (docs[0].id, docs[0].full_name) if docs else None

        return SpecialistRouter(doctor_for)

    def _specialist_general(self) -> RoutingOutcome:
        return self._specialist().route("", pending={"kind": "specialty"})

    # -- knowledge base -------------------------------------------------------------------------
    def _rag(self, ctx: _Ctx, route: RouteDecision) -> ChatTurn:
        intent = route.intent if route.intent in _RAG_INTENTS else ChatIntent.FAQ_RAG
        filters = RetrievalFilters(
            boost_doc_types=("price_list",) if intent is ChatIntent.PRICING else ()
        )
        query = route.standalone_query
        retrieval = self.index.retrieve(query, filters)
        if (
            retrieval.confidence < self.settings.rag_min_confidence
            and not ctx.history
            and query.strip().lower() != ctx.text.strip().lower()
        ):
            # The router's rewrite can phrase a first message slightly worse than the patient
            # did ("opening hours" -> "clinic's operating hours"): try the original words too.
            original = self.index.retrieve(ctx.text, filters)
            if original.confidence > retrieval.confidence:
                query, retrieval = ctx.text, original
        if retrieval.confidence < self.settings.rag_min_confidence or not retrieval.chunks:
            return self._low_confidence(ctx, intent, retrieval)
        generator = AnswerGenerator(ctx.llm)
        with outside_transaction(self.s):
            answer = generator.generate(
                query,
                retrieval,
                history=ctx.history,
                actor_user_id=ctx.actor.id if ctx.actor else None,
                redact_names=ctx.names,
            )
        ctx.call_ids += answer.llm_call_ids
        ctx.cost += answer.cost_usd
        if answer.mode == "refused":
            return self._low_confidence(ctx, intent, retrieval, answer)
        return ChatTurn(
            answer.text,
            intent,
            answer.mode,
            citations=answer.citations,
            disclaimers=answer.disclaimers or disclaimers_for(answer.citations),
            flags=answer.flags,
        )

    def _low_confidence(
        self,
        ctx: _Ctx,
        intent: ChatIntent,
        retrieval: RetrievalResult,
        answer: Answer | None = None,
    ) -> ChatTurn:
        """Section 5.4, verbatim. No answer LLM call is made below the threshold."""
        flags = [f"low_confidence:{retrieval.confidence:.3f}"]
        if answer is not None:
            flags += answer.flags
        reply = scripts.LOW_CONFIDENCE
        if ctx.actor is None:
            reply = f"{reply}\n\n{scripts.ANON_HANDOFF}"
        return ChatTurn(reply, intent, "refused", handoff_offer=ctx.actor is not None, flags=flags)

    # -- handoff / abuse ------------------------------------------------------------------------
    def _handoff_turn(
        self,
        chat: ChatSession,
        actor: CurrentUser | None,
        reason: HandoffReason,
        ctx: _Ctx | None = None,
    ) -> ChatTurn:
        if actor is None:  # anonymous: nobody to call back -> the front-desk number
            return ChatTurn(scripts.ANON_HANDOFF, ChatIntent.HUMAN_HANDOFF, "fixed")
        if ctx is not None:
            ctx.clear_pending = True
            ctx.handoff = None  # created in _finish, after the user message is stored
            ctx.flags.append(f"handoff:{reason.value}")
            return ChatTurn(scripts.HANDOFF_CREATED, ChatIntent.HUMAN_HANDOFF, "fixed")
        handoff = self.handoffs.create(chat, reason, actor)
        return ChatTurn(
            scripts.HANDOFF_CREATED,
            ChatIntent.HUMAN_HANDOFF,
            "fixed",
            handoff_id=handoff.id,
            session_status=chat.status,
        )

    def _frustration(self, ctx: _Ctx, turn: ChatTurn) -> ChatTurn:
        ctx.flags.append("handoff:frustration")
        if ctx.actor is None:
            turn.reply = f"{turn.reply}\n\n{scripts.FRUSTRATION_OFFER}\n\n{scripts.ANON_HANDOFF}"
        else:
            turn.reply = f"{turn.reply}\n\n{scripts.HANDOFF_CREATED}"
        return turn

    def _spam(self, ctx: _Ctx) -> ChatTurn:
        ctx.end_session = True
        ctx.flags.append("handoff:spam")
        return ChatTurn(scripts.SPAM_END, ChatIntent.OFF_TOPIC, "fixed")

    # -- persistence ------------------------------------------------------------------------
    def _finish(self, ctx: _Ctx, turn: ChatTurn) -> ChatTurn:
        chat = ctx.chat
        now = self.clock.now_utc()
        seq = (
            self.s.scalar(
                select(func.max(ChatMessage.seq)).where(ChatMessage.session_id == chat.id)
            )
            or 0
        )
        self.s.add(
            ChatMessage(
                session_id=chat.id, seq=seq + 1, role=ChatRole.USER, text=ctx.text, created_at=now
            )
        )
        turn.flags = list(dict.fromkeys(ctx.flags + turn.flags))
        turn.llm_call_ids = ctx.call_ids
        turn.cost_usd = ctx.cost
        assistant = ChatMessage(
            session_id=chat.id,
            seq=seq + 2,
            role=ChatRole.ASSISTANT,
            text=turn.reply,
            intent=turn.intent.value,
            mode=turn.mode,
            citations=[c.to_json() for c in turn.citations],
            safety=turn.safety,
            llm_call_ids=ctx.call_ids,
            created_at=now,
        )
        self.s.add(assistant)
        chat.turns += 1
        if ctx.negative:
            chat.negative_turns += 1
        chat.cost_usd = Decimal(str(chat.cost_usd)) + Decimal(str(round(ctx.cost, 6)))
        chat.last_active_at = now
        if ctx.new_pending is not None:
            chat.pending_clarification = ctx.new_pending
        elif ctx.clear_pending:
            chat.pending_clarification = None
        if ctx.end_session:
            chat.status = ChatSessionStatus.ENDED
        self.s.flush()

        handoff_reason = next(
            (HandoffReason(f.split(":", 1)[1]) for f in turn.flags if f.startswith("handoff:")),
            None,
        )
        if handoff_reason is not None and (
            ctx.actor is not None or handoff_reason is HandoffReason.SPAM
        ):
            handoff = self.handoffs.create(chat, handoff_reason, ctx.actor)
            turn.handoff_id = handoff.id
        if ctx.end_session:
            chat.status = ChatSessionStatus.ENDED
            self.s.flush()
        turn.session_status = chat.status
        turn.message_id = assistant.id
        return turn
