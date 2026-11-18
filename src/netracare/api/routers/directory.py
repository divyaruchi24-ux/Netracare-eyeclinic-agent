from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.common import Page
from netracare.api.schemas.directory import (
    DoctorChangeOut,
    DoctorCreate,
    DoctorOut,
    DoctorUpdateIn,
    GuardianLinkIn,
    GuardianOut,
    PatientOut,
    SpecialtyOut,
)
from netracare.api.security import AdminUserDep, CurrentUserDep, StaffUserDep
from netracare.domain.clock import clinic_today
from netracare.services.access import AccessPolicy
from netracare.services.directory_service import DirectoryService, DoctorUpdate

router = APIRouter(tags=["directory"])

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100_000)]


@router.get("/specialties")
def list_specialties(db: DbSession, clock: ClockDep) -> list[SpecialtyOut]:
    return [SpecialtyOut.model_validate(s) for s in DirectoryService(db, clock).list_specialties()]


@router.get("/doctors")
def list_doctors(
    db: DbSession,
    clock: ClockDep,
    specialty: Annotated[str | None, Query(max_length=32)] = None,
    active: bool = True,
) -> list[DoctorOut]:
    docs = DirectoryService(db, clock).list_doctors(specialty, active_only=active)
    return [DoctorOut.of(d) for d in docs]


@router.get("/doctors/{doctor_id}")
def get_doctor(doctor_id: str, db: DbSession, clock: ClockDep) -> DoctorOut:
    return DoctorOut.of(DirectoryService(db, clock).get_doctor(doctor_id))


@router.post("/doctors", status_code=status.HTTP_201_CREATED)
def create_doctor(
    body: DoctorCreate, user: AdminUserDep, db: DbSession, clock: ClockDep
) -> DoctorOut:
    doc = DirectoryService(db, clock).create_doctor(
        user, body.code, body.full_name, body.specialty_code, body.bio_slug
    )
    return DoctorOut.of(doc)


@router.patch("/doctors/{doctor_id}")
def update_doctor(
    doctor_id: str,
    body: DoctorUpdateIn,
    user: AdminUserDep,
    db: DbSession,
    clock: ClockDep,
    force: bool = False,
) -> DoctorChangeOut:
    change = DirectoryService(db, clock).update_doctor(
        user, doctor_id, DoctorUpdate(**body.model_dump()), force=force
    )
    return DoctorChangeOut(
        doctor=DoctorOut.of(change.doctor), affected_appointment_ids=change.affected_appointment_ids
    )


@router.get("/patients")
def search_patients(
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    q: Annotated[str | None, Query(max_length=64)] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> Page[PatientOut]:
    rows, total = DirectoryService(db, clock).search_patients(q, limit, offset)
    today = clinic_today(clock, settings.tz)
    return Page[PatientOut](
        items=[PatientOut.of(p, today) for p in rows], total=total, limit=limit, offset=offset
    )


@router.get("/patients/{patient_id}")
def get_patient(
    patient_id: str, user: CurrentUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> PatientOut:
    patient = AccessPolicy(db).get_patient_for_view(user, patient_id)
    guardians = [
        GuardianOut(user_id=u.id, email=u.email, relationship=g.relationship_label)
        for g, u in DirectoryService(db, clock).guardians_of(patient.id)
    ]
    return PatientOut.of(patient, clinic_today(clock, settings.tz), guardians)


@router.post("/patients/{patient_id}/guardians", status_code=status.HTTP_201_CREATED)
def link_guardian(
    patient_id: str, body: GuardianLinkIn, user: StaffUserDep, db: DbSession, clock: ClockDep
) -> GuardianOut:
    link = DirectoryService(db, clock).link_guardian(
        user, patient_id, str(body.guardian_email), body.relationship
    )
    return GuardianOut(
        user_id=link.guardian_user_id,
        email=str(body.guardian_email).lower(),
        relationship=link.relationship_label,
    )
