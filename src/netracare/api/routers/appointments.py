from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.appointments import (
    AppointmentOut,
    BookingResponse,
    BookRequest,
    CancelRequest,
    CancelResponse,
    RescheduleRequest,
    appointment_out,
    approval_ref,
)
from netracare.api.schemas.common import Page
from netracare.api.security import (
    BookerUserDep,
    ClinicianUserDep,
    CurrentUserDep,
    StaffUserDep,
)
from netracare.db.models import Appointment
from netracare.domain.enums import AppointmentStatus, Role
from netracare.errors import NotFoundError, require
from netracare.services.access import AccessPolicy
from netracare.services.booking_service import BookCommand, BookingResult, BookingService
from netracare.services.idempotency_service import (
    IdempotencyService,
    request_hash,
    scoped_key,
    validate_key,
)

router = APIRouter(prefix="/appointments", tags=["appointments"])

IdemKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=64)]
OptionalIdemKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)]
REPLAY_HEADER = "Idempotent-Replayed"


def _booking_body(db: DbSession, result: BookingResult, settings: SettingsDep) -> dict[str, Any]:
    return BookingResponse(
        appointment=appointment_out(db, result.appointment, settings.tz),
        requires_approval=result.requires_approval,
        approval_reasons=result.approval_reasons,
    ).model_dump(mode="json")


def _idempotent(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    user_id: str,
    key: str | None,
    payload: dict[str, Any],
) -> tuple[IdempotencyService, str | None, JSONResponse | None]:
    """Return (service, body_hash, replay_response). Replays skip the handler entirely."""
    svc = IdempotencyService(db, clock)
    if key is None:
        return svc, None, None
    validate_key(key)
    body_hash = request_hash(payload)
    stored = svc.lookup(user_id, key, request.method, request.url.path, body_hash)
    if stored is None:
        return svc, body_hash, None
    return (
        svc,
        body_hash,
        JSONResponse(stored.body, status_code=stored.status_code, headers={REPLAY_HEADER: "true"}),
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=BookingResponse)
def book(
    body: BookRequest,
    request: Request,
    user: BookerUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    idempotency_key: IdemKey,
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    idem, body_hash, replay = _idempotent(request, db, clock, user.id, idempotency_key, payload)
    if replay is not None:
        return replay
    result = BookingService(db, clock, settings).book(
        user,
        BookCommand(
            patient_id=body.patient_id,
            doctor_id=body.doctor_id,
            slot_ids=tuple(body.slot_ids),
            type=body.type,
            reason=body.reason,
            idempotency_key=scoped_key(user.id, idempotency_key),
            override=body.override,
            override_reason=body.override_reason,
        ),
    )
    out = _booking_body(db, result, settings)
    idem.store(
        user.id, idempotency_key, request.method, request.url.path, require(body_hash), 201, out
    )
    return JSONResponse(out, status_code=201)


@router.get("")
def list_appointments(
    user: CurrentUserDep,
    db: DbSession,
    settings: SettingsDep,
    patient_id: Annotated[str | None, Query(max_length=36)] = None,
    doctor_id: Annotated[str | None, Query(max_length=36)] = None,
    status_: Annotated[AppointmentStatus | None, Query(alias="status")] = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AppointmentOut]:
    tz = settings.tz
    stmt = select(Appointment)
    if user.role is Role.PATIENT:
        allowed = AccessPolicy(db).patient_ids_for(user)
        if patient_id is not None and patient_id not in allowed:
            raise NotFoundError("patient_not_found", "patient not found")
        stmt = stmt.where(Appointment.patient_id.in_([patient_id] if patient_id else allowed))
    elif patient_id is not None:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    if user.role is Role.DOCTOR:
        stmt = stmt.where(Appointment.doctor_id == user.doctor_id)
    elif doctor_id is not None:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if status_ is not None:
        stmt = stmt.where(Appointment.status == status_)
    if from_date is not None:
        stmt = stmt.where(Appointment.start_utc >= datetime.combine(from_date, time(0), tz))
    if to_date is not None:
        end = datetime.combine(to_date + timedelta(days=1), time(0), tz)
        stmt = stmt.where(Appointment.start_utc < end)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(stmt.order_by(Appointment.start_utc).limit(limit).offset(offset))
    return Page[AppointmentOut](
        items=[appointment_out(db, a, tz) for a in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{appointment_id}")
def get_appointment(
    appointment_id: str, user: CurrentUserDep, db: DbSession, settings: SettingsDep
) -> AppointmentOut:
    appt = AccessPolicy(db).get_appointment_for_view(user, appointment_id)
    return appointment_out(db, appt, settings.tz)


@router.post("/{appointment_id}/cancel")
def cancel(
    appointment_id: str,
    body: CancelRequest,
    user: CurrentUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> CancelResponse:
    res = BookingService(db, clock, settings).cancel(user, appointment_id, body.reason)
    return CancelResponse(
        appointment=appointment_out(db, res.appointment, settings.tz),
        cancelled=res.cancelled,
        approval=approval_ref(res.approval),
    )


@router.post("/{appointment_id}/reschedule", response_model=BookingResponse)
def reschedule(
    appointment_id: str,
    body: RescheduleRequest,
    request: Request,
    user: CurrentUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    idempotency_key: OptionalIdemKey = None,
) -> JSONResponse:
    payload = {"appointment_id": appointment_id, **body.model_dump(mode="json")}
    idem, body_hash, replay = _idempotent(request, db, clock, user.id, idempotency_key, payload)
    if replay is not None:
        return replay
    result = BookingService(db, clock, settings).reschedule(
        user,
        appointment_id,
        body.slot_ids,
        body.reason,
        scoped_key(user.id, idempotency_key) if idempotency_key else None,
    )
    out = _booking_body(db, result, settings)
    if idempotency_key is not None and body_hash is not None:
        idem.store(user.id, idempotency_key, request.method, request.url.path, body_hash, 200, out)
    return JSONResponse(out, status_code=200)


@router.post("/{appointment_id}/check-in")
def check_in(
    appointment_id: str, user: StaffUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> AppointmentOut:
    appt = BookingService(db, clock, settings).check_in(user, appointment_id)
    return appointment_out(db, appt, settings.tz)


@router.post("/{appointment_id}/no-show")
def no_show(
    appointment_id: str, user: StaffUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> AppointmentOut:
    appt = BookingService(db, clock, settings).mark_no_show(user, appointment_id)
    return appointment_out(db, appt, settings.tz)


@router.post("/{appointment_id}/complete")
def complete(
    appointment_id: str,
    user: ClinicianUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> AppointmentOut:
    appt = BookingService(db, clock, settings).complete(user, appointment_id)
    return appointment_out(db, appt, settings.tz)
