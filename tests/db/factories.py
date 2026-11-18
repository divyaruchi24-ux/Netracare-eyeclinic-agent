"""Minimal object builders for DB tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from netracare.db.models import Appointment, Doctor, Patient, Slot, Specialty
from netracare.domain.enums import AppointmentStatus, AppointmentType

T0 = datetime(2026, 12, 1, 3, 30, tzinfo=UTC)


def make_doctor(session: Session, code: str = "D9") -> Doctor:
    spec = Specialty(code=f"S-{code}", name="Test specialty")
    session.add(spec)
    session.flush()
    doc = Doctor(code=code, full_name=f"Dr. Test {code}", specialty_id=spec.id)
    session.add(doc)
    session.flush()
    return doc


def make_patient(session: Session, name: str = "Test Patient") -> Patient:
    p = Patient(full_name=name, dob=date(1990, 1, 1))
    session.add(p)
    session.flush()
    return p


def make_slot(session: Session, doctor: Doctor, start: datetime = T0) -> Slot:
    s = Slot(doctor_id=doctor.id, start_utc=start, end_utc=start + timedelta(minutes=15))
    session.add(s)
    session.flush()
    return s


def make_appointment(session: Session, patient: Patient, doctor: Doctor, slot: Slot) -> Appointment:
    a = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        type=AppointmentType.ROUTINE,
        status=AppointmentStatus.CONFIRMED,
        start_utc=slot.start_utc,
        end_utc=slot.end_utc,
    )
    session.add(a)
    session.flush()
    return a
