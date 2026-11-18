"""Ownership checks. Role checks happen at the route; *which records* a caller may touch is
decided here so agents and MCP tools (which call services directly) get the same rules.

Records the caller may not see are reported as 404, never 403, so IDs cannot be probed.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from netracare.db.models import Appointment, Patient, PatientGuardian
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import Role
from netracare.errors import ForbiddenError, NotFoundError


class AccessPolicy:
    def __init__(self, session: Session) -> None:
        self.s = session

    def patient_ids_for(self, user: CurrentUser) -> set[str]:
        """Patients a patient-role user may act for: their own record + guardian links."""
        if user.role is not Role.PATIENT:
            return set()
        own = self.s.scalars(select(Patient.id).where(Patient.user_id == user.id))
        linked = self.s.scalars(
            select(PatientGuardian.patient_id).where(PatientGuardian.guardian_user_id == user.id)
        )
        return set(own) | set(linked)

    def is_guardian_of(self, user: CurrentUser, patient_id: str) -> bool:
        return (
            self.s.get(PatientGuardian, (patient_id, user.id)) is not None
            if user.role is Role.PATIENT
            else False
        )

    def get_patient_for_view(self, user: CurrentUser, patient_id: str) -> Patient:
        patient = self.s.get(Patient, patient_id)
        if patient is None or not self._can_view_patient(user, patient_id):
            raise NotFoundError("patient_not_found", "patient not found")
        return patient

    def _can_view_patient(self, user: CurrentUser, patient_id: str) -> bool:
        if user.is_staff:
            return True
        if user.role is Role.PATIENT:
            return patient_id in self.patient_ids_for(user)
        if user.role is Role.DOCTOR and user.doctor_id:
            return bool(
                self.s.scalar(
                    select(
                        exists().where(
                            Appointment.patient_id == patient_id,
                            Appointment.doctor_id == user.doctor_id,
                        )
                    )
                )
            )
        return False

    def assert_can_view_appointment(self, user: CurrentUser, appt: Appointment) -> None:
        if user.is_staff:
            return
        if user.role is Role.DOCTOR and appt.doctor_id == user.doctor_id:
            return
        if user.role is Role.PATIENT and appt.patient_id in self.patient_ids_for(user):
            return
        raise NotFoundError("appointment_not_found", "appointment not found")

    def get_appointment_for_view(self, user: CurrentUser, appointment_id: str) -> Appointment:
        appt = self.s.get(Appointment, appointment_id)
        if appt is None:
            raise NotFoundError("appointment_not_found", "appointment not found")
        self.assert_can_view_appointment(user, appt)
        return appt

    def assert_can_act_for_patient(self, user: CurrentUser, patient_id: str) -> Patient:
        """Booking/changing on behalf of a patient: staff, or the patient/guardian themselves."""
        if user.role is Role.DOCTOR:
            raise ForbiddenError("forbidden", "doctors cannot book or change appointments")
        return self.get_patient_for_view(user, patient_id)
