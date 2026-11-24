"""Layer 2 of the chat router: the LLM intent classifier (`intent_router@v1`, fast model).

It also rewrites follow-ups into a standalone query ("and is it covered?" -> "Is LASIK covered
by insurance?"), so retrieval needs no extra call. When the LLM is unavailable, over budget or
returns garbage, a keyword fallback keeps chat working (`source="keyword_fallback"`).
Low confidence never fails silently: the caller treats it as `faq_rag` gated by retrieval
confidence, and hands off when retrieval is not confident either (section 12).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from netracare.domain.enums import ChatIntent
from netracare.guardrails.advice_filter import safe_concern
from netracare.llm.client import LLMClient
from netracare.llm.schemas import RouterOutput
from netracare.llm.types import LLMError
from netracare.log import get_logger
from netracare.rag.doctor_names import DoctorName, match_doctor

log = get_logger(__name__)

_PRICE = re.compile(
    r"\b(?:price|prices|cost|costs|fee|fees|charge|charges|how much|rate|rates|rupees|inr|rs"
    r"|kitna|kitne|expensive|cheap|afford)\b",
    re.IGNORECASE,
)
_BOOKING = re.compile(
    r"\b(?:book|booking|appointment|appointments|reschedule|cancel|slot|slots|available"
    r"|availability|come in|visit tomorrow|see the doctor)\b",
    re.IGNORECASE,
)
_DOCTOR = re.compile(r"\b(?:dr\.?|doctor|doctors|specialist|surgeon)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RouteDecision:
    intent: ChatIntent
    confidence: float
    standalone_query: str
    source: str  # "llm" | "keyword_fallback"
    specialty_hint: str | None = None
    doctor_mention: str | None = None
    concern_phrase: str | None = None
    llm_call_ids: tuple[str, ...] = field(default_factory=tuple)
    cost_usd: float = 0.0
    error: str | None = None


def keyword_fallback(text: str, doctors: Sequence[DoctorName] = ()) -> ChatIntent:
    if _BOOKING.search(text):
        return ChatIntent.BOOKING
    if _PRICE.search(text):
        return ChatIntent.PRICING
    if match_doctor(text, doctors) or _DOCTOR.search(text):
        return ChatIntent.DOCTOR_LOOKUP
    return ChatIntent.FAQ_RAG


class Router:
    def __init__(self, llm: LLMClient | None, doctors: Sequence[DoctorName] = ()) -> None:
        self._llm = llm
        self._doctors = list(doctors)

    def route(
        self,
        text: str,
        history: str = "",
        *,
        actor_user_id: str | None = None,
        redact_names: Sequence[str] = (),
    ) -> RouteDecision:
        if self._llm is None or not self._llm.available:
            return self._fallback(text, "llm_disabled")
        try:
            result = self._llm.generate(
                "intent_router",
                {"message": text, "history": history or "(none)"},
                purpose="chat_router",
                schema=RouterOutput,
                actor_user_id=actor_user_id,
                redact_names=redact_names,
            )
        except LLMError as exc:
            log.warning("chat.router_failed", code=exc.code)
            return self._fallback(text, exc.code)
        out: RouterOutput = result.value
        standalone = out.standalone_query.strip() or text
        return RouteDecision(
            intent=ChatIntent(out.intent),
            confidence=out.confidence,
            standalone_query=standalone,
            source="llm",
            specialty_hint=out.specialty_hint,
            doctor_mention=out.doctor_mention,
            concern_phrase=safe_concern(out.concern_phrase),
            llm_call_ids=(result.call_id,),
            cost_usd=float(result.cost_usd),
        )

    def _fallback(self, text: str, error: str) -> RouteDecision:
        return RouteDecision(
            intent=keyword_fallback(text, self._doctors),
            confidence=0.5,
            standalone_query=text,
            source="keyword_fallback",
            error=error,
        )
