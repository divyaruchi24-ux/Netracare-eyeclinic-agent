"""Pydantic output schemas for structured LLM calls, and a strict-JSON-schema converter."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from netracare.domain.enums import RedFlagLevel

RedFlagCode = Literal[
    "E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "E10",
    "U1", "U2", "U3", "U4", "U5", "U6",
]  # fmt: skip
SpecialtyCode = Literal["CORNEA", "RETINA", "GLAUCOMA", "PEDIATRIC", "GENERAL"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriageLLMOutput(_Strict):
    level: RedFlagLevel
    matched_codes: list[RedFlagCode]
    suggested_specialty: SpecialtyCode | None
    confidence: float = Field(ge=0, le=1)
    concern_phrase: str = Field(max_length=80)
    is_medical_question: bool


class EyeFactOutput(_Strict):
    fact: str = Field(min_length=10, max_length=400)
    topic: str = Field(max_length=64)


class MedicineMention(_Strict):
    name: str = Field(max_length=120)
    instructions_verbatim: str = Field(max_length=400)


class VisitSummaryDraft(_Strict):
    what_we_checked: list[str]
    what_we_found: str = Field(max_length=2000)
    next_steps: list[str]
    medicines_mentioned: list[MedicineMention]
    follow_up: str | None
    unclear_in_notes: list[str]


RouterIntent = Literal[
    "faq_rag",
    "doctor_lookup",
    "pricing",
    "booking",
    "medical_question",
    "emergency",
    "human_handoff",
    "chit_chat",
    "off_topic",
]


class ChatAnswerOutput(_Strict):
    """A KB-grounded answer. `citations` are the chunk numbers used; `answerable=false` when
    the provided chunks do not answer the question (the app then hands off)."""

    answer: str = Field(max_length=1200)
    citations: list[int]
    answerable: bool


class RouterOutput(_Strict):
    intent: RouterIntent
    confidence: float = Field(ge=0, le=1)
    standalone_query: str = Field(max_length=300)
    specialty_hint: SpecialtyCode | None
    doctor_mention: str | None = Field(max_length=80)
    concern_phrase: str | None = Field(max_length=80)


SCHEMAS: dict[str, type[BaseModel]] = {
    "TriageLLMOutput": TriageLLMOutput,
    "EyeFactOutput": EyeFactOutput,
    "VisitSummaryDraft": VisitSummaryDraft,
    "ChatAnswerOutput": ChatAnswerOutput,
    "RouterOutput": RouterOutput,
}

# Keywords strict mode (Groq / OpenAI structured outputs) does not accept.
_DROP = frozenset({"title", "default", "maxLength", "minLength", "minimum", "maximum"})


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> strict-mode schema: `$ref`s inlined, every property required,
    `additionalProperties: false`. Range/length limits are dropped here and still enforced by
    Pydantic validation after the call."""
    raw = model.model_json_schema()
    defs: dict[str, Any] = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
        out = {k: walk(v) for k, v in node.items() if k not in _DROP}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    schema: dict[str, Any] = walk(raw)
    return schema
