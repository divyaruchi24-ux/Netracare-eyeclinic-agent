"""HTTP routers, in registration order."""

from __future__ import annotations

from fastapi import APIRouter

from netracare.api.routers import (
    admin,
    appointments,
    approvals,
    auth,
    availability,
    directory,
    doctor,
    front_desk,
    notifications,
    schedule,
)

# `doctor` (/doctors/me/schedule) is registered before `directory` (/doctors/{doctor_id}).
ALL_ROUTERS: list[APIRouter] = [
    auth.router,
    doctor.router,
    directory.router,
    schedule.router,
    availability.router,
    appointments.router,
    approvals.router,
    front_desk.router,
    admin.router,
    notifications.router,
]
