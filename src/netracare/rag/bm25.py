"""BM25 keyword index over `kb_chunks`, built in memory (about 100 chunks: milliseconds).

The tokenizer is applied identically to documents and queries:
lowercase -> typo map -> synonym folding -> stopwords out -> light plural stripping.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from netracare.guardrails.redflag_rules import _TYPOS
from netracare.rag.vector_store import Scalar, Where, _matches

# Common misspellings of clinic vocabulary (extends the Phase 2 red-flag typo map).
TYPO_MAP: dict[str, str] = {
    **_TYPOS,
    "lasic": "lasik",
    "lazik": "lasik",
    "lasek": "lasik",
    "lasix": "lasik",
    "catract": "cataract",
    "cataracts": "cataract",
    "catarct": "cataract",
    "cateract": "cataract",
    "glacoma": "glaucoma",
    "gluacoma": "glaucoma",
    "glucoma": "glaucoma",
    "glaucomma": "glaucoma",
    "retna": "retina",
    "retinal": "retina",
    "opthalmologist": "ophthalmologist",
    "ophthalmologyst": "ophthalmologist",
    "opthalmology": "ophthalmology",
    "diabetis": "diabetic",
    "diabetes": "diabetic",
    "diabeties": "diabetic",
    "insurence": "insurance",
    "insurace": "insurance",
    "apointment": "appointment",
    "appoinment": "appointment",
    "appointmnet": "appointment",
    "cancelation": "cancellation",
    "cancell": "cancel",
    "resheduling": "rescheduling",
    "reshedule": "reschedule",
    "dilatation": "dilation",
    "dialation": "dilation",
    "dilating": "dilation",
    "dilated": "dilation",
    "squnit": "squint",
    "ptossis": "ptosis",
    "pediatric": "paediatric",
    "pedeatric": "paediatric",
    "timings": "timing",
    "parkng": "parking",
    "contacts": "contact",
}

# Folded to one token so "fee", "cost" and "kitna" (Hinglish "how much") all hit the price list.
SYNONYMS: dict[str, str] = {
    "cost": "price",
    "costs": "price",
    "fee": "price",
    "fees": "price",
    "charge": "price",
    "charges": "price",
    "rate": "price",
    "rates": "price",
    "pricing": "price",
    "prices": "price",
    "rupees": "price",
    "inr": "price",
    "rs": "price",
    "kitna": "price",
    "kitne": "price",
    "keemat": "price",
    "daam": "price",
    "paisa": "price",
    "doctor": "dr",
    "doc": "dr",
    "doctors": "dr",
    "hours": "timing",
    "open": "timing",
    "opd": "timing",
    "kids": "child",
    "kid": "child",
    "children": "child",
    "son": "child",
    "daughter": "child",
    "paediatric": "child",
    "squinting": "squint",
    "crossed": "squint",
}

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "am",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "it",
        "its",
        "of",
        "to",
        "in",
        "on",
        "for",
        "at",
        "by",
        "with",
        "and",
        "or",
        "but",
        "if",
        "then",
        "so",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "please",
        "tell",
        "know",
        "want",
        "need",
        "get",
        "got",
        "about",
        "any",
        "some",
        "ka",
        "ki",
        "ke",
        "hai",
        "hain",
        "kya",
        "mujhe",
        "ko",
    ]
)

_WORD = re.compile(r"[a-z0-9]+")
_POSSESSIVE = re.compile(r"'s\b")


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    cleaned = _POSSESSIVE.sub("", text.lower().replace("’", "'"))
    for raw in _WORD.findall(cleaned):
        word = TYPO_MAP.get(raw, raw)
        word = SYNONYMS.get(word, word)
        if word in STOPWORDS:
            continue
        out.append(_stem(word))
    return out


@dataclass(frozen=True)
class BM25Doc:
    id: str
    text: str
    meta: Mapping[str, Scalar | None]


class BM25Index:
    def __init__(self, docs: Sequence[BM25Doc]) -> None:
        self._docs = list(docs)
        self._tokens = [tokenize(d.text) for d in self._docs]
        # BM25Okapi divides by the corpus size; keep an empty index usable.
        self._bm25 = BM25Okapi(self._tokens) if self._docs else None

    def __len__(self) -> int:
        return len(self._docs)

    @property
    def vocabulary(self) -> set[str]:
        return {t for tokens in self._tokens for t in tokens}

    def search(self, query: str, k: int, where: Where | None = None) -> list[tuple[str, float]]:
        """Top-k `(id, bm25 score)` with score > 0, best first; filters apply before ranking."""
        q = tokenize(query)
        if not q or self._bm25 is None:
            return []
        scores = self._bm25.get_scores(q)
        ranked = [
            (d.id, float(s))
            for d, s in zip(self._docs, scores, strict=True)
            if s > 0 and _matches(d.meta, where)
        ]
        ranked.sort(key=lambda x: (-x[1], x[0]))
        return ranked[:k]
