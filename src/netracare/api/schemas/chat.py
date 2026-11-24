"""Chat, knowledge-base and handoff API schemas (Phase 3)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from netracare.api.schemas.common import Schema
from netracare.domain.enums import ChatIntent, ChatSessionStatus, HandoffReason, HandoffStatus

ChatText = Annotated[str, StringConstraints(max_length=2000)]
CloseNote = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class CitationOut(Schema):
    n: int
    chunk_id: str
    slug: str
    title: str
    version: int
    effective_date: date
    heading_path: str
    doc_type: str
    excerpt: str
    is_current: bool = True


class SuggestedActionOut(Schema):
    type: str  # "book"
    specialty: str | None
    doctor_id: str | None
    doctor_name: str | None


class ChatMessageIn(Schema):
    text: ChatText


class ChatTurnOut(Schema):
    reply: str
    intent: ChatIntent
    mode: str
    citations: list[CitationOut]
    disclaimers: list[str]
    footer: str
    safety: dict[str, Any]
    handoff_id: str | None
    handoff_offer: bool
    suggested_action: SuggestedActionOut | None
    session_status: ChatSessionStatus
    message_id: str | None


class ChatMessageOut(Schema):
    id: str
    seq: int
    role: str
    text: str
    intent: str | None
    mode: str | None
    citations: list[CitationOut]
    disclaimers: list[str] = Field(default_factory=list)
    safety: dict[str, Any]
    created_at: datetime


class ChatSessionOut(Schema):
    id: str
    status: ChatSessionStatus
    turns: int
    created_at: datetime
    session_token: str | None = None  # returned once, on creation, to anonymous callers
    footer: str
    messages: list[ChatMessageOut] = Field(default_factory=list)


class HandoffOut(Schema):
    id: str
    session_id: str
    reason: HandoffReason
    summary_redacted: str
    status: HandoffStatus
    patient_id: str | None
    patient_name: str | None
    anonymous: bool
    claimed_by: str | None
    claimed_at: datetime | None
    closed_by: str | None
    closed_at: datetime | None
    close_note: str | None
    created_at: datetime
    version: int


class HandoffList(Schema):
    items: list[HandoffOut]
    open_total: int


class HandoffVersionIn(Schema):
    version: int | None = None


class HandoffCloseIn(Schema):
    note: CloseNote
    version: int | None = None


class HandoffConversationOut(Schema):
    handoff: HandoffOut
    messages: list[ChatMessageOut]


class KbChunkOut(Schema):
    id: str
    slug: str
    title: str
    version: int
    effective_date: date
    doc_type: str
    heading_path: str
    text: str
    is_current: bool


class KbDocOut(Schema):
    id: str
    slug: str
    title: str
    doc_type: str
    version: int
    effective_date: date
    is_current: bool
    source_path: str
    n_chunks: int


class KbStatusOut(Schema):
    fingerprint: str | None
    embedding_model: str | None
    chunker_version: str | None
    n_docs: int
    n_chunks: int
    built_at: datetime | None
    vectors_ok: bool
    report: dict[str, Any]
    docs: list[KbDocOut]


class KbSearchHit(Schema):
    rank: int
    chunk_id: str
    slug: str
    version: int
    heading_path: str
    is_current: bool
    rrf_score: float
    bm25_rank: int | None
    vector_rank: int | None
    cosine: float | None
    rerank_score: float | None


class KbSearchOut(Schema):
    query: str
    normalized_query: str
    confidence: float
    threshold: float
    answerable: bool
    mode: str
    doctor: str | None
    hits: list[KbSearchHit]
    latency_ms: int
