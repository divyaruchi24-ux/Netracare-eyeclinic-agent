"""Doctors, specialties and patient records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netracare.db.models import (
    Appointment,
    Doctor,
    Patient,
    PatientGuardian,
    Slot,
    Specialty,
    User,
)
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import ACTIVE_STATUSES, Role, SlotStatus
from netracare.errors import ConflictError, NotFoundError, ValidationAppError
from netracare.repositories.directory_repo import (
    DoctorRepository,
    SpecialtyRepository,
    UserRepository,
)
from netracare.services.audit_service import AuditService

GUARDIAN_RELATIONSHIPS = frozenset(
    {"mother", "father", "guardian", "adult_child", "spouse", "other"}
)


@dataclass(frozen=True)
class DoctorUpdate:
    full_name: str | None = None
    specialty_code: str | None = None
    bio_slug: str | None = None
    is_active: bool | None = None


@dataclass(frozen=True)
class DoctorChange:
    doctor: Doctor
    affected_appointment_ids: list[str]


@dataclass(frozen=True)
class NewPatient:
    full_name: str
    dob: date
    phone: str | None = None
    email: str | None = None


def doctor_snapshot(d: Doctor) -> dict[str, object]:
    return {
        "code": d.code,
        "full_name": d.full_name,
        "specialty_id": d.specialty_id,
        "bio_slug": d.bio_slug,
        "is_active": d.is_active,
    }


class DirectoryService:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.s = session
        self.clock = clock
        self.doctors = DoctorRepository(session)
        self.specialties = SpecialtyRepository(session)
        self.audit = AuditService(session)

    # -- specialties & doctors ---------------------------------------------------------------
    def list_specialties(self) -> list[Specialty]:
        return list(self.s.scalars(select(Specialty).order_by(Specialty.name)))

    def list_doctors(
        self, specialty_code: str | None = None, active_only: bool = True
    ) -> list[Doctor]:
        stmt = select(Doctor).join(Specialty).order_by(Doctor.code)
        if specialty_code:
            stmt = stmt.where(Specialty.code == specialty_code)
        if active_only:
            stmt = stmt.where(Doctor.is_active)
        return list(self.s.scalars(stmt))

    def get_doctor(self, doctor_id: str) -> Doctor:
        doctor = self.doctors.get(doctor_id)
        if doctor is None:
            raise NotFoundError("doctor_not_found", "doctor not found")
        return doctor

    def _specialty(self, code: str) -> Specialty:
        spec = self.specialties.by_code(code)
        if spec is None:
            raise ValidationAppError("unknown_specialty", f"unknown specialty '{code}'")
        return spec

    def create_doctor(
        self,
        actor: CurrentUser,
        code: str,
        full_name: str,
        specialty_code: str,
        bio_slug: str | None,
    ) -> Doctor:
        if self.doctors.by_code(code) is not None:
            raise ConflictError("doctor_code_taken", f"doctor code '{code}' already exists")
        doctor = self.doctors.add(
            Doctor(
                code=code,
                full_name=full_name,
                specialty_id=self._specialty(specialty_code).id,
                bio_slug=bio_slug,
            )
        )
        self.audit.record(
            actor, "doctor.create", "doctor", doctor.id, after=doctor_snapshot(doctor)
        )
        return doctor

    def future_appointments(self, doctor_id: str) -> list[Appointment]:
        return list(
            self.s.scalars(
                select(Appointment)
                .where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.status.in_(ACTIVE_STATUSES),
                    Appointment.start_utc >= self.clock.now_utc(),
                )
                .order_by(Appointment.start_utc)
            )
        )

    def update_doctor(
        self, actor: CurrentUser, doctor_id: str, upd: DoctorUpdate, *, force: bool = False
    ) -> DoctorChange:
        doctor = self.get_doctor(doctor_id)
        before = doctor_snapshot(doctor)
        affected: list[str] = []
        if upd.is_active is False and doctor.is_active:
            appts = self.future_appointments(doctor.id)
            affected = [a.id for a in appts]
            if appts and not force:
                raise ConflictError(
                    "doctor_has_appointments",
                    "doctor has upcoming appointments; reassign them or pass force=true",
                    details={"appointment_ids": affected},
                )
            # Close future availability; existing bookings stay for the front desk to handle.
            for slot in self.s.scalars(
                select(Slot).where(
                    Slot.doctor_id == doctor.id,
                    Slot.start_utc >= self.clock.now_utc(),
                    Slot.status == SlotStatus.OPEN,
                )
            ):
                slot.status = SlotStatus.BLOCKED
        if upd.full_name is not None:
            doctor.full_name = upd.full_name
        if upd.specialty_code is not None:
            doctor.specialty_id = self._specialty(upd.specialty_code).id
        if upd.bio_slug is not None:
            doctor.bio_slug = upd.bio_slug
        if upd.is_active is not None:
            doctor.is_active = upd.is_active
        self.s.flush()
        self.audit.record(
            actor,
            "doctor.update",
            "doctor",
            doctor.id,
            before=before,
            after={**doctor_snapshot(doctor), "affected_appointments": affected},
            reason="forced deactivation" if force and affected else None,
        )
        return DoctorChange(doctor, affected)

    # -- patients ----------------------------------------------------------------------------
    def search_patients(self, q: str | None, limit: int, offset: int) -> tuple[list[Patient], int]:
        stmt = select(Patient)
        if q:
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Patient.full_name).like(like),
                    func.lower(Patient.email).like(like),
                    Patient.phone.like(f"%{q.strip()}%"),
                )
            )
        total = int(self.s.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = self.s.scalars(stmt.order_by(Patient.full_name).limit(limit).offset(offset))
        return list(rows), total

    def guardians_of(self, patient_id: str) -> list[tuple[PatientGuardian, User]]:
        rows = self.s.execute(
            select(PatientGuardian, User)
            .join(User, User.id == PatientGuardian.guardian_user_id)
            .where(PatientGuardian.patient_id == patient_id)
        )
        return [(g, u) for g, u in rows]

    def link_guardian(
        self, actor: CurrentUser, patient_id: str, guardian_email: str, relationship: str
    ) -> PatientGuardian:
        patient = self.s.get(Patient, patient_id)
        if patient is None:
            raise NotFoundError("patient_not_found", "patient not found")
        if relationship not in GUARDIAN_RELATIONSHIPS:
            raise ValidationAppError(
                "invalid_relationship",
                f"relationship must be one of {sorted(GUARDIAN_RELATIONSHIPS)}",
            )
        guardian = UserRepository(self.s).by_email(guardian_email)
        if guardian is None or guardian.role is not Role.PATIENT or not guardian.is_active:
            raise ValidationAppError(
                "guardian_not_found", "the guardian must have an active patient account"
            )
        if patient.user_id == guardian.id:
            raise ValidationAppError("self_guardian", "a patient cannot be their own guardian")
        if self.s.get(PatientGuardian, (patient.id, guardian.id)) is not None:
            raise ConflictError("already_linked", "this guardian is already linked")
        link = PatientGuardian(
            patient_id=patient.id,
            guardian_user_id=guardian.id,
            relationship_label=relationship,
            consent_captured_at=self.clock.now_utc(),
        )
        self.s.add(link)
        self.s.flush()
        self.audit.record(
            actor,
            "patient.guardian_link",
            "patient",
            patient.id,
            after={"guardian_user_id": guardian.id, "relationship": relationship},
        )
        return link

    def create_patient(self, actor: CurrentUser, new: NewPatient) -> Patient:
        """Front-desk registration of a patient without an online account (e.g. walk-in)."""
        if new.dob > self.clock.now_utc().date():
            raise ValidationAppError("invalid_dob", "date of birth cannot be in the future")
        patient = Patient(
            full_name=new.full_name,
            dob=new.dob,
            phone=new.phone,
            email=new.email.lower() if new.email else None,
            consent_captured_at=self.clock.now_utc(),
        )
        self.s.add(patient)
        try:
            self.s.flush()
        except IntegrityError as exc:  # pragma: no cover - no unique fields today
            raise ConflictError("patient_exists") from exc
        self.audit.record(
            actor, "patient.create", "patient", patient.id, after={"source": "front_desk"}
        )
        return patient
