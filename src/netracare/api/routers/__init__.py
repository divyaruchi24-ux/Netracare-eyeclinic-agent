"""HTTP routers, in registration order."""

from __future__ import annotations

from fastapi import APIRouter

from netracare.api.routers import (
    admin,
    appointments,
    approvals,
    auth,
    availability,
    chat,
    directory,
    doctor,
    eye_fact,
    front_desk,
    handoffs,
    kb,
    notifications,
    schedule,
    triage,
    visit_summaries,
)

# `doctor` (/doctors/me/schedule) is registered before `directory` (/doctors/{doctor_id});
# `handoffs` (/chat/handoffs) before `chat` (/chat/sessions/...) for clarity.
ALL_ROUTERS: list[APIRouter] = [
    auth.router,
    doctor.router,
    directory.router,
    schedule.router,
    availability.router,
    visit_summaries.router,
    appointments.router,
    approvals.router,
    front_desk.router,
    admin.router,
    notifications.router,
    triage.router,
    eye_fact.router,
    handoffs.router,
    chat.router,
    kb.router,
]
