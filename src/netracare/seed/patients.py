"""Generate fictional patients (Faker en_IN, fixed seed)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from faker import Faker


@dataclass(frozen=True)
class GuardianSeed:
    full_name: str
    email: str
    relationship: str


@dataclass(frozen=True)
class PatientSeed:
    key: str
    full_name: str
    dob: date
    phone: str
    email: str | None
    no_show_count: int = 0
    has_own_account: bool = True
    guardian: GuardianSeed | None = None
    tags: frozenset[str] = field(default_factory=frozenset)


def _dob_for_age(rng: random.Random, today: date, age: int) -> date:
    """A DOB such that the person is exactly `age` today (birthday not on today)."""
    latest = date(today.year - age, today.month, min(today.day, 28)) - timedelta(days=1)
    return latest - timedelta(days=rng.randint(0, 300))


def _email(first: str, last: str, i: int) -> str:
    clean = "".join(ch for ch in f"{first}.{last}".lower() if ch.isalnum() or ch == ".")
    return f"{clean}.{i}@example.com"


def make_patients(today: date, n: int = 30, seed: int = 42) -> list[PatientSeed]:
    """Return `n` patients: 5 minors with parent guardians, 1 elderly patient managed by an
    adult child, 3 patients with >=3 no-shows, the rest ordinary adults."""
    if n < 9:
        raise ValueError("n must be at least 9 to include all special cases")
    fake = Faker("en_IN")
    fake.seed_instance(seed)
    rng = random.Random(seed)
    out: list[PatientSeed] = []

    for i in range(n):
        first, last = fake.first_name(), fake.last_name()
        phone = f"+91 90000 {i:05d}"
        key = f"P{i:03d}"
        if i < 5:  # minors
            age = rng.randint(3, 15)
            g_first = fake.first_name()
            guardian = GuardianSeed(
                full_name=f"{g_first} {last}",
                email=_email(g_first, last, 100 + i),
                relationship=rng.choice(["mother", "father"]),
            )
            out.append(
                PatientSeed(
                    key=key,
                    full_name=f"{first} {last}",
                    dob=_dob_for_age(rng, today, age),
                    phone=phone,
                    email=None,
                    has_own_account=False,
                    guardian=guardian,
                    tags=frozenset({"minor"}),
                )
            )
        elif i == 5:  # elderly, managed by adult child
            c_first = fake.first_name()
            out.append(
                PatientSeed(
                    key=key,
                    full_name=f"{first} {last}",
                    dob=_dob_for_age(rng, today, 78),
                    phone=phone,
                    email=None,
                    has_own_account=False,
                    guardian=GuardianSeed(
                        full_name=f"{c_first} {last}",
                        email=_email(c_first, last, 100 + i),
                        relationship="adult_child",
                    ),
                    tags=frozenset({"elderly"}),
                )
            )
        else:
            no_shows = (3, 4, 5)[i - 6] if 6 <= i <= 8 else rng.choice([0, 0, 0, 1])
            tags = {"no_show_flagged"} if no_shows >= 3 else set()
            out.append(
                PatientSeed(
                    key=key,
                    full_name=f"{first} {last}",
                    dob=_dob_for_age(rng, today, rng.randint(19, 70)),
                    phone=phone,
                    email=_email(first, last, i),
                    no_show_count=no_shows,
                    tags=frozenset(tags),
                )
            )
    return out
