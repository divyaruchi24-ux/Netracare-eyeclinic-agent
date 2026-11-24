"""Cited answers from retrieved chunks, checked by code before anything reaches the patient.

    retrieval -> chat_answer@v1 (strict JSON) -> post-checks -> [one repair] -> answer
                                         \\-> LLM down / still failing -> extractive answer

Post-checks (docs/phase-3/understanding.md 3.7): every `[n]` points at a provided chunk and
at least one is present; every number appears in a cited chunk; no medicine/dose/diagnosis/
treatment wording that the cited chunks do not contain; no prompt or other-patient leakage.
Disclaimers are added by code (safety policy section 10), never by the model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from netracare.guardrails.advice_filter import check_chat_answer, cited_numbers
from netracare.guardrails.scripts import DISCLAIMERS, EXTRACTIVE_INTRO
from netracare.llm.client import LLMClient
from netracare.llm.schemas import ChatAnswerOutput
from netracare.llm.types import LLMError
from netracare.log import get_logger
from netracare.rag.retriever import ChunkRecord, RetrievalResult

log = get_logger(__name__)

AnswerMode = Literal["llm", "extractive", "refused"]
EXCERPT_CHARS = 320
_MARKER = re.compile(r"\[(\d{1,2})\]")


@dataclass(frozen=True)
class Citation:
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

    def to_json(self) -> dict[str, object]:
        return {
            "n": self.n,
            "chunk_id": self.chunk_id,
            "slug": self.slug,
            "title": self.title,
            "version": self.version,
            "effective_date": self.effective_date.isoformat(),
            "heading_path": self.heading_path,
            "doc_type": self.doc_type,
            "excerpt": self.excerpt,
            "is_current": self.is_current,
        }


@dataclass
class Answer:
    text: str
    mode: AnswerMode
    citations: list[Citation] = field(default_factory=list)
    disclaimers: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    llm_call_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    t = " ".join(text.split()) if not text.lstrip().startswith("|") else text.strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0]).rstrip() + " …"


def format_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


def format_chunks(chunks: Sequence[ChunkRecord]) -> str:
    """Numbered extracts for the prompt: `[n] title > heading (effective date)` + text."""
    parts = []
    for n, c in enumerate(chunks, start=1):
        parts.append(
            f"[{n}] {c.title} › {c.heading_path} (effective {c.effective_date.isoformat()})\n"
            f"{c.text}"
        )
    return "\n\n".join(parts)


def postcheck(text: str, chunks: Sequence[ChunkRecord], *, answerable: bool) -> list[str]:
    flags: list[str] = []
    used = cited_numbers(text)
    if any(n < 1 or n > len(chunks) for n in used):
        flags.append("invalid_citation")
    valid = sorted({n for n in used if 1 <= n <= len(chunks)})
    if answerable and not valid:
        flags.append("no_citation")
    sources = "\n".join(chunks[n - 1].text for n in valid) if valid else ""
    flags += check_chat_answer(text, sources)
    return flags


def with_markers(text: str, citations: Sequence[int]) -> str:
    """gpt-oss often lists its sources in the `citations` field but writes no inline `[n]`.
    Then the markers are appended to the answer, so the same post-checks (valid numbers,
    numbers grounded in the cited chunks) still apply; invalid numbers stay invalid."""
    if cited_numbers(text) or not citations:
        return text
    marks = "".join(f"[{n}]" for n in dict.fromkeys(citations))
    return f"{text.rstrip()} {marks}"


def renumber(text: str, chunks: Sequence[ChunkRecord]) -> tuple[str, list[Citation]]:
    """Rewrite `[n]` markers to 1..m in order of first use; return the matching citations."""
    order: list[int] = []
    for n in cited_numbers(text):
        if 1 <= n <= len(chunks) and n not in order:
            order.append(n)
    mapping = {old: new for new, old in enumerate(order, start=1)}

    def sub(m: re.Match[str]) -> str:
        old = int(m.group(1))
        return f"[{mapping[old]}]" if old in mapping else ""

    new_text = re.sub(r"(\[\d+\])(?=\1)", "", _MARKER.sub(sub, text))  # "[1][1]" -> "[1]"
    citations = [_citation(new, chunks[old - 1]) for old, new in mapping.items()]
    return new_text.strip(), citations


def _citation(n: int, c: ChunkRecord) -> Citation:
    return Citation(
        n=n,
        chunk_id=c.id,
        slug=c.slug,
        title=c.title,
        version=c.version,
        effective_date=c.effective_date,
        heading_path=c.heading_path,
        doc_type=c.doc_type,
        excerpt=excerpt(c.text),
        is_current=c.is_current,
    )


def disclaimers_for(citations: Sequence[Citation]) -> list[str]:
    out = [DISCLAIMERS["kb_answer"]]
    prices = [c for c in citations if c.doc_type == "price_list"]
    if prices:
        latest = max(c.effective_date for c in prices)
        out.append(DISCLAIMERS["price"].format(date=format_date(latest)))
    return out


def extractive(chunks: Sequence[ChunkRecord], n: int = 2, *, reason: str = "") -> Answer:
    """The KB text itself, cited: used when the LLM is unavailable or fails the checks."""
    picked = list(chunks[:n])
    if not picked:
        return Answer(text="", mode="refused", flags=[reason] if reason else [])
    lines = [EXTRACTIVE_INTRO]
    for i, c in enumerate(picked, start=1):
        body = c.text if c.text.lstrip().startswith("|") else excerpt(c.text, 600)
        lines.append(f"**{c.title} › {c.heading_path}** [{i}]\n\n{body}")
    citations = [_citation(i, c) for i, c in enumerate(picked, start=1)]
    return Answer(
        text="\n\n".join(lines),
        mode="extractive",
        citations=citations,
        disclaimers=disclaimers_for(citations),
        flags=[reason] if reason else [],
    )


class AnswerGenerator:
    def __init__(self, llm: LLMClient | None, *, purpose: str = "chat_answer") -> None:
        self._llm = llm
        self._purpose = purpose

    def generate(
        self,
        question: str,
        retrieval: RetrievalResult,
        *,
        history: str = "",
        actor_user_id: str | None = None,
        redact_names: Sequence[str] = (),
    ) -> Answer:
        chunks = [c.chunk for c in retrieval.chunks]
        if not chunks:
            return Answer(text="", mode="refused", flags=["no_chunks"])
        if self._llm is None or not self._llm.available:
            return extractive(chunks, reason="llm:llm_disabled")

        call_ids: list[str] = []
        cost = 0.0
        feedback = "none"
        flags: list[str] = []
        for attempt in (1, 2):
            try:
                result = self._llm.generate(
                    "chat_answer",
                    {
                        "question": question,
                        "chunks": format_chunks(chunks),
                        "history": history or "(none)",
                        "feedback": feedback,
                    },
                    purpose=self._purpose,
                    schema=ChatAnswerOutput,
                    actor_user_id=actor_user_id,
                    redact_names=redact_names,
                )
            except LLMError as exc:
                log.warning("rag.answer_llm_failed", code=exc.code, attempt=attempt)
                ans = extractive(chunks, reason=f"llm:{exc.code}")
                ans.llm_call_ids, ans.cost_usd = call_ids, cost
                return ans
            call_ids.append(result.call_id)
            cost += float(result.cost_usd)
            out: ChatAnswerOutput = result.value
            if not out.answerable:
                return Answer(
                    text="", mode="refused", flags=["not_answerable"],
                    llm_call_ids=call_ids, cost_usd=cost,
                )  # fmt: skip
            answer_text = with_markers(out.answer, out.citations)
            flags = postcheck(answer_text, chunks, answerable=True)
            if not flags:
                text, citations = renumber(answer_text, chunks)
                return Answer(
                    text=text,
                    mode="llm",
                    citations=citations,
                    disclaimers=disclaimers_for(citations),
                    llm_call_ids=call_ids,
                    cost_usd=cost,
                    flags=["repaired"] if attempt == 2 else [],
                )
            log.info("rag.answer_postcheck_failed", flags=flags, attempt=attempt)
            feedback = (
                "Your previous answer was rejected because: "
                + "; ".join(flags)
                + (". Use only facts, numbers and citations from the extracts.")
            )
        ans = extractive(chunks, reason="postcheck_failed")
        ans.flags += flags
        ans.llm_call_ids, ans.cost_usd = call_ids, cost
        return ans
