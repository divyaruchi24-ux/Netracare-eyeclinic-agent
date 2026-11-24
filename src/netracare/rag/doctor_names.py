"""Fuzzy doctor-name matching ("Dr Shetti" -> Dr. Vikram Shetty) using stdlib difflib."""

from __future__ import annotations

import difflib
import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass

_HONORIFIC = re.compile(r"^(dr\.?|doctor)$", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class DoctorName:
    code: str
    full_name: str  # "Dr. Vikram Shetty"

    @property
    def parts(self) -> list[str]:
        return [w.lower() for w in _WORD.findall(self.full_name) if not _HONORIFIC.match(w)]


@dataclass(frozen=True)
class DoctorMatch:
    code: str
    full_name: str
    matched: str  # the query word(s) that matched
    score: float


def match_doctor(
    text: str, doctors: Sequence[DoctorName], cutoff: float = 0.8
) -> DoctorMatch | None:
    """The doctor whose first name or surname a query word resembles most (None if no match).

    Single query words are compared with each doctor's name parts; consecutive word pairs are
    compared with "first last" so "vikram shety" also matches. Words shorter than 4 letters are
    ignored to avoid matching "eye" or "dry".
    """
    raw = _WORD.findall(text)
    words: list[str] = []
    after_title: set[int] = set()  # indexes of words right after "Dr" / "doctor"
    for w in raw:
        if _HONORIFIC.match(w):
            after_title.add(len(words))
        else:
            words.append(w.lower())
    # (candidate, cutoff, surname_only): right after an honorific a looser match against the
    # surname is safe ("Dr. Nayar" -> Nair, "Dr Iyar" -> Iyer).
    candidates: list[tuple[str, float, bool]] = []
    for i, w in enumerate(words):
        if len(w) >= 4:
            candidates.append((w, cutoff, False))
        elif len(w) == 3:
            candidates.append((w, 1.0, False))  # short surnames ("rao") need an exact match
        if i in after_title and len(w) >= 3:
            candidates.append((w, min(cutoff, 0.66), True))
    candidates += [(f"{a} {b}", cutoff, False) for a, b in itertools.pairwise(words)]
    best: DoctorMatch | None = None
    for doc in doctors:
        parts = doc.parts
        full = " ".join(parts[-2:])
        for cand, need, surname_only in candidates:
            if surname_only:
                names = parts[-1:]
            elif " " in cand:
                names = [full] if len(parts) >= 2 else []
            else:
                names = parts
            for name in names:
                score = difflib.SequenceMatcher(None, cand, name).ratio()
                if score >= need and (best is None or score > best.score):
                    best = DoctorMatch(doc.code, doc.full_name, cand, round(score, 3))
    return best
