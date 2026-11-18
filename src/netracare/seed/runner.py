"""Idempotent seeding of reference + demo data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.base import utcnow
from netracare.db.models import (
    Doctor,
    Holiday,
    Leave,
    Patient,
    PatientGuardian,
    ScheduleTemplate,
    Specialty,
    StaffUser,
    User,
)
from netracare.domain.enums import ActorType, Role
from netracare.domain.policies import should_flag_for_no_shows
from netracare.domain.slot_generation import WorkBlock, generate_slots
from netracare.repositories.audit_repo import AuditRepository
from netracare.repositories.slot_repo import SlotRepository
from netracare.security.passwords import hash_password
from netracare.seed.ids import seed_id
from netracare.seed.kb import upsert_kb
from netracare.seed.patients import make_patients


@dataclass
class SeedResult:
    inserted: dict[str, int] = field(default_factory=dict)

    def bump(self, kind: str, n: int = 1) -> None:
        self.inserted[kind] = self.inserted.get(kind, 0) + n

    @property
    def total(self) -> int:
        return sum(self.inserted.values())


def _load(data_dir: Path, name: str) -> dict[str, Any]:
    with (data_dir / name).open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{name}: expected a mapping")
    return loaded


def _hhmm(value: str) -> time:
    return time.fromisoformat(value)


class Seeder:
    def __init__(
        self,
        session: Session,
        *,
        data_dir: Path,
        kb_dir: Path,
        root: Path,
        today: date,
        tz: ZoneInfo,
        password: str,
        weeks: int = 8,
    ) -> None:
        self.s = session
        self.data_dir = data_dir
        self.kb_dir = kb_dir
        self.root = root
        self.today = today
        self.tz = tz
        self.weeks = weeks
        self._password = password
        self._pw_hash: str | None = None
        self.result = SeedResult()

    # -- helpers ---------------------------------------------------------------------------
    def _password_hash(self) -> str:
        if self._pw_hash is None:
            self._pw_hash = hash_password(self._password)
        return self._pw_hash

    def _get_or_add(self, model: type[Any], id_: Any, kind: str, **fields: Any) -> Any:
        obj = self.s.get(model, id_)
        if obj is None:
            pk = "day" if model is Holiday else "id"
            obj = model(**{pk: id_}, **fields)
            self.s.add(obj)
            self.s.flush()
            self.result.bump(kind)
        return obj

    def _user(self, email: str, role: Role) -> User:
        email = email.lower()
        user: User = self._get_or_add(
            User,
            seed_id("user", email),
            "users",
            email=email,
            role=role,
            password_hash=self._password_hash(),
        )
        return user

    # -- steps -----------------------------------------------------------------------------
    def run(self) -> SeedResult:
        self.seed_specialties_and_doctors()
        self.seed_staff()
        self.seed_holidays()
        self.seed_leaves()
        self.seed_slots()
        self.seed_patients()
        n_kb = upsert_kb(self.s, self.kb_dir, self.root)
        if n_kb:
            self.result.bump("knowledge_docs", n_kb)
        if self.result.total:
            AuditRepository(self.s).append(
                action="seed.run",
                entity_type="system",
                entity_id=None,
                actor_type=ActorType.SYSTEM,
                after={"inserted": self.result.inserted, "today": self.today.isoformat()},
            )
        return self.result

    def seed_specialties_and_doctors(self) -> None:
        for sp in _load(self.data_dir, "specialties.yaml")["specialties"]:
            self._get_or_add(
                Specialty,
                seed_id("specialty", sp["code"]),
                "specialties",
                code=sp["code"],
                name=sp["name"],
                description=sp["description"],
            )
        for d in _load(self.data_dir, "doctors.yaml")["doctors"]:
            doctor = self._get_or_add(
                Doctor,
                seed_id("doctor", d["code"]),
                "doctors",
                code=d["code"],
                full_name=d["full_name"],
                specialty_id=seed_id("specialty", d["specialty"]),
                bio_slug=d["bio_slug"],
            )
            user = self._user(d["email"], Role.DOCTOR)
            self._get_or_add(
                StaffUser,
                seed_id("staff", d["email"]),
                "staff_users",
                user_id=user.id,
                display_name=d["full_name"],
                doctor_id=doctor.id,
            )
            for b in d["blocks"]:
                self._get_or_add(
                    ScheduleTemplate,
                    seed_id("template", f"{d['code']}:{b['weekday']}:{b['start']}"),
                    "schedule_templates",
                    doctor_id=doctor.id,
                    weekday=b["weekday"],
                    start_local=_hhmm(b["start"]),
                    end_local=_hhmm(b["end"]),
                    valid_from=date(2026, 1, 1),
                )

    def seed_staff(self) -> None:
        for st in _load(self.data_dir, "staff.yaml")["staff"]:
            user = self._user(st["email"], Role(st["role"]))
            self._get_or_add(
                StaffUser,
                seed_id("staff", st["email"]),
                "staff_users",
                user_id=user.id,
                display_name=st["display_name"],
            )

    def seed_holidays(self) -> None:
        for h in _load(self.data_dir, "holidays.yaml")["holidays"]:
            self._get_or_add(Holiday, h["date"], "holidays", name=h["name"])

    def seed_leaves(self) -> None:
        for lv in _load(self.data_dir, "leaves.yaml")["leaves"]:
            start_day = self.today + timedelta(days=lv["start_offset_days"])
            end_day = self.today + timedelta(days=lv["end_offset_days"])
            start_local = datetime.combine(start_day, _hhmm(lv.get("start_time", "00:00")), self.tz)
            if "end_time" in lv:
                end_local = datetime.combine(end_day, _hhmm(lv["end_time"]), self.tz)
            else:
                end_local = datetime.combine(end_day + timedelta(days=1), time(0), self.tz)
            doctor_id = seed_id("doctor", lv["doctor"])
            key = f"{lv['doctor']}:{start_local.isoformat()}"
            self._get_or_add(
                Leave,
                seed_id("leave", key),
                "leaves",
                doctor_id=doctor_id,
                start_utc=start_local,
                end_utc=end_local,
                reason=lv["reason"],
            )
            SlotRepository(self.s).block_range(doctor_id, start_local, end_local)

    def seed_slots(self) -> None:
        holidays = set(self.s.scalars(select(Holiday.day)))
        slot_repo = SlotRepository(self.s)
        for doctor in self.s.scalars(select(Doctor).order_by(Doctor.code)):
            templates = self.s.scalars(
                select(ScheduleTemplate).where(ScheduleTemplate.doctor_id == doctor.id)
            )
            blocks = [WorkBlock(t.weekday, t.start_local, t.end_local) for t in templates]
            leaves = [
                (lv.start_utc, lv.end_utc)
                for lv in self.s.scalars(select(Leave).where(Leave.doctor_id == doctor.id))
            ]
            specs = generate_slots(
                doctor.id, blocks, leaves, holidays, self.today, self.weeks * 7, self.tz
            )
            n = slot_repo.upsert_many(specs)
            if n:
                self.result.bump("slots", n)

    def seed_patients(self) -> None:
        now = utcnow()
        for p in make_patients(self.today):
            user_id = (
                self._user(p.email, Role.PATIENT).id if p.has_own_account and p.email else None
            )
            patient = self._get_or_add(
                Patient,
                seed_id("patient", p.key),
                "patients",
                user_id=user_id,
                full_name=p.full_name,
                dob=p.dob,
                phone=p.phone,
                email=p.email,
                no_show_count=p.no_show_count,
                flagged=should_flag_for_no_shows(p.no_show_count),
                consent_captured_at=now,
            )
            if p.guardian is not None:
                guardian = self._user(p.guardian.email, Role.PATIENT)
                if self.s.get(PatientGuardian, (patient.id, guardian.id)) is None:
                    self.s.add(
                        PatientGuardian(
                            patient_id=patient.id,
                            guardian_user_id=guardian.id,
                            relationship_label=p.guardian.relationship,
                            consent_captured_at=now,
                        )
                    )
                    self.s.flush()
                    self.result.bump("patient_guardians")
