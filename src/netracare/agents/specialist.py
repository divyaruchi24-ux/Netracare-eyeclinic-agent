"""Specialist routing for chat: concern -> specialty -> doctor, in non-diagnostic words.

Order (docs/phase-3/understanding.md 3.9): a child mention or a pediatric red-flag code ->
PEDIATRIC; other red-flag codes -> their specialty; then the concern lexicon
(`config/specialty_routing.yaml`); then the LLM router's specialty hint.

Ambiguity (two or more specialties match, or the lexicon and the LLM disagree): ask ONE
clarifying question; if the next answer is still ambiguous, route to General (D5) with the
section 6 "more than one area" sentence. Never guess silently.

The doctor's name always comes from the directory (DB), never from the LLM, and the sentence
is always a section 6 template.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from netracare.guardrails import scripts
from netracare.guardrails.advice_filter import safe_concern
from netracare.guardrails.redflag_rules import (
    CODE_CONCERNS,
    mentions_child,
    normalize,
    specialty_for,
)

DoctorLookup = Callable[[str], tuple[str, str] | None]  # specialty code -> (doctor_id, name)
OutcomeKind = Literal["specialty", "pediatric", "general", "clarify", "none"]
SPECIALTY_ORDER = ("CORNEA", "RETINA", "GLAUCOMA", "PEDIATRIC", "GENERAL")


@dataclass(frozen=True)
class SpecialtyEntry:
    code: str
    concern: str
    clarify: str
    phrases: tuple[str, ...]
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Lexicon:
    version: int
    entries: tuple[SpecialtyEntry, ...]

    def matches(self, text: str) -> dict[str, str]:
        """{specialty code: first matched phrase} on the normalised text."""
        t = normalize(text)
        out: dict[str, str] = {}
        for e in self.entries:
            m = e.pattern.search(t)
            if m:
                out[e.code] = m.group(0)
        return out

    def entry(self, code: str) -> SpecialtyEntry | None:
        return next((e for e in self.entries if e.code == code), None)


def _compile(phrases: Sequence[str]) -> re.Pattern[str]:
    alts = sorted((re.escape(p.lower()) for p in phrases), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b")


def parse_lexicon(data: dict[str, Any]) -> Lexicon:
    entries = []
    for code, spec in (data.get("specialties") or {}).items():
        if code not in SPECIALTY_ORDER:
            raise ValueError(f"unknown specialty code in lexicon: {code}")
        phrases = tuple(str(p) for p in spec.get("phrases") or ())
        if not phrases:
            raise ValueError(f"lexicon entry {code} has no phrases")
        entries.append(
            SpecialtyEntry(
                code, str(spec["concern"]), str(spec["clarify"]), phrases, _compile(phrases)
            )
        )
    entries.sort(key=lambda e: SPECIALTY_ORDER.index(e.code))
    return Lexicon(int(data.get("version", 1)), tuple(entries))


@lru_cache(maxsize=4)
def load_lexicon(path: str = "config/specialty_routing.yaml") -> Lexicon:
    return parse_lexicon(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class RoutingOutcome:
    kind: OutcomeKind
    specialty: str | None = None
    doctor_id: str | None = None
    doctor_name: str | None = None
    sentence: str | None = None
    clarify_options: tuple[str, ...] = field(default_factory=tuple)

    @property
    def pending(self) -> dict[str, Any] | None:
        """What to store on the session after a clarifying question."""
        if self.kind != "clarify":
            return None
        return {"kind": "specialty", "options": list(self.clarify_options)}


def _join_options(options: Sequence[str]) -> str:
    """'**A**, or about **B**' (each option is itself a list, so plain commas would blur them)."""
    return ", or about ".join(f"**{o}**" for o in options)


class SpecialistRouter:
    def __init__(self, doctor_for: DoctorLookup, lexicon: Lexicon | None = None) -> None:
        self._doctor_for = doctor_for
        self._lexicon = lexicon or load_lexicon()

    def route(
        self,
        text: str,
        *,
        codes: Sequence[str] = (),
        llm_hint: str | None = None,
        concern_phrase: str | None = None,
        pending: dict[str, Any] | None = None,
    ) -> RoutingOutcome:
        if mentions_child(text) or any(c in ("U4", "U5") for c in codes):
            return self._outcome("PEDIATRIC", None)
        if codes:
            code = specialty_for(codes, text) or "GENERAL"
            concern = next((CODE_CONCERNS[c] for c in codes if c in CODE_CONCERNS), None)
            return self._outcome(code, concern)

        found = self._lexicon.matches(text)
        hint = llm_hint if llm_hint in SPECIALTY_ORDER and llm_hint != "GENERAL" else None
        candidates = list(found)
        if hint and hint not in found and found:
            candidates.append(hint)  # lexicon and LLM disagree -> ambiguous
        if not candidates and hint:
            return self._outcome(hint, safe_concern(concern_phrase))
        if not candidates:
            return RoutingOutcome("none") if not pending else self._general()
        if len(candidates) == 1:
            code = candidates[0]
            return self._outcome(code, self._concern(code, concern_phrase))
        if pending:
            return self._general()
        options = tuple(e.clarify for c in candidates if (e := self._lexicon.entry(c)) is not None)
        return RoutingOutcome(
            "clarify",
            clarify_options=options,
            sentence=scripts.CLARIFY_TEMPLATE.format(options=_join_options(options)),
        )

    def _concern(self, code: str, llm_concern: str | None) -> str | None:
        """The LLM's (sanitised) phrase in the patient's words, else the lexicon's noun phrase.
        The raw matched keyword is not used: "gritty" or "family history" read badly."""
        ok = safe_concern(llm_concern)
        if ok:
            return ok
        entry = self._lexicon.entry(code)
        return entry.concern if entry else None

    def _general(self) -> RoutingOutcome:
        doc = self._doctor_for("GENERAL")
        return RoutingOutcome(
            "general",
            "GENERAL",
            doc[0] if doc else None,
            doc[1] if doc else None,
            scripts.ROUTING_TEMPLATES["general"],
        )

    def _outcome(self, code: str, concern: str | None) -> RoutingOutcome:
        if code == "PEDIATRIC":
            doc = self._doctor_for("PEDIATRIC")
            return RoutingOutcome(
                "pediatric",
                "PEDIATRIC",
                doc[0] if doc else None,
                doc[1] if doc else None,
                scripts.ROUTING_TEMPLATES["pediatric"],
            )
        doc = self._doctor_for(code)
        if code == "GENERAL" or doc is None or not concern:
            if code == "GENERAL" and doc is not None and concern:
                # A concern that clearly belongs to D5 (cataract, eyelids): name it.
                return RoutingOutcome(
                    "specialty",
                    "GENERAL",
                    doc[0],
                    doc[1],
                    scripts.specialty_sentence(scripts.GENERAL_SPECIALTY_LABEL, concern, doc[1]),
                )
            return self._general()
        return RoutingOutcome(
            "specialty",
            code,
            doc[0],
            doc[1],
            scripts.routing_sentence(code, concern, doc[1]),
        )
