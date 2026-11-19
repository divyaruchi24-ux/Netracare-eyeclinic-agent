"""Every route x {anonymous, patient, doctor, front_desk, admin}.

The table below is the access policy. `test_matrix_covers_every_route` fails when a route is
added without a row here, so no endpoint can ship without an explicit permission decision.

Expected outcome per caller:
* allowed       -> anything except 401/403 (e.g. 200, 404 for a dummy id, 422 for an empty body)
* not allowed   -> 403 (signed in) or 401 (anonymous)
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures_api import ADMIN, DOCTOR_EMAILS, FRONTDESK, Headers, did, patient_email

PUBLIC = "public"
P, D, F, A = "patient", "doctor", "front_desk", "admin"
ALL = frozenset({P, D, F, A})
STAFF = frozenset({F, A})
ADMIN_ONLY = frozenset({A})

ROUTES: dict[tuple[str, str], frozenset[str] | str] = {
    ("GET", "/health"): PUBLIC,
    ("POST", "/auth/register"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("POST", "/auth/refresh"): PUBLIC,
    ("POST", "/auth/logout"): PUBLIC,
    ("GET", "/auth/me"): ALL,
    ("GET", "/doctors/me/schedule"): frozenset({D}),
    ("GET", "/specialties"): PUBLIC,
    ("GET", "/doctors"): PUBLIC,
    ("POST", "/doctors"): ADMIN_ONLY,
    ("GET", "/doctors/{doctor_id}"): PUBLIC,
    ("PATCH", "/doctors/{doctor_id}"): ADMIN_ONLY,
    ("GET", "/patients"): STAFF,
    ("GET", "/patients/{patient_id}"): ALL,
    ("POST", "/patients/{patient_id}/guardians"): STAFF,
    ("GET", "/doctors/{doctor_id}/schedule-templates"): ADMIN_ONLY,
    ("POST", "/doctors/{doctor_id}/schedule-templates"): ADMIN_ONLY,
    ("DELETE", "/schedule-templates/{template_id}"): ADMIN_ONLY,
    ("GET", "/doctors/{doctor_id}/leaves"): ADMIN_ONLY,
    ("POST", "/doctors/{doctor_id}/leaves"): ADMIN_ONLY,
    ("DELETE", "/leaves/{leave_id}"): ADMIN_ONLY,
    ("GET", "/holidays"): ALL,
    ("POST", "/holidays"): ADMIN_ONLY,
    ("DELETE", "/holidays/{day}"): ADMIN_ONLY,
    ("POST", "/admin/slots/regenerate"): ADMIN_ONLY,
    ("GET", "/availability"): ALL,
    ("POST", "/appointments"): frozenset({P, F, A}),
    ("GET", "/appointments"): ALL,
    ("GET", "/appointments/{appointment_id}"): ALL,
    # cancel/reschedule are open at the route; the service refuses doctors (tested elsewhere)
    ("POST", "/appointments/{appointment_id}/cancel"): ALL,
    ("POST", "/appointments/{appointment_id}/reschedule"): ALL,
    ("POST", "/appointments/{appointment_id}/check-in"): STAFF,
    ("POST", "/appointments/{appointment_id}/no-show"): STAFF,
    ("POST", "/appointments/{appointment_id}/complete"): frozenset({D, F, A}),
    ("GET", "/approvals"): STAFF,
    ("GET", "/approvals/{request_id}"): ALL,
    ("POST", "/approvals/{request_id}/approve"): STAFF,
    ("POST", "/approvals/{request_id}/reject"): STAFF,
    ("POST", "/approvals/{request_id}/withdraw"): frozenset({P}),
    ("GET", "/front-desk/queue"): STAFF,
    ("POST", "/front-desk/walk-ins"): STAFF,
    ("GET", "/users"): ADMIN_ONLY,
    ("POST", "/users"): ADMIN_ONLY,
    ("PATCH", "/users/{user_id}"): ADMIN_ONLY,
    ("GET", "/audit-logs"): ADMIN_ONLY,
    ("GET", "/notifications"): frozenset({P, F, A}),
    # Phase 2: LLM features
    ("POST", "/triage"): PUBLIC,
    ("GET", "/triage/alerts"): STAFF,
    ("POST", "/triage/alerts/{alert_id}/ack"): STAFF,
    ("POST", "/triage/alerts/{alert_id}/resolve"): STAFF,
    ("GET", "/eye-fact"): PUBLIC,
    ("POST", "/appointments/{appointment_id}/visit-summary"): frozenset({D}),
    # patients/guardians see approved summaries only; staff see status only (service-level)
    ("GET", "/appointments/{appointment_id}/visit-summary"): ALL,
    ("PATCH", "/appointments/{appointment_id}/visit-summary"): frozenset({D}),
    ("POST", "/appointments/{appointment_id}/visit-summary/approve"): frozenset({D}),
    ("POST", "/appointments/{appointment_id}/visit-summary/discard"): frozenset({D}),
    ("GET", "/admin/llm-usage"): ADMIN_ONLY,
}

PATH_VALUES = {
    "doctor_id": did("D1"),
    "patient_id": "no-such-patient",
    "template_id": "no-such-template",
    "leave_id": "no-such-leave",
    "day": "2027-02-02",
    "appointment_id": "no-such-appointment",
    "request_id": "no-such-request",
    "user_id": "no-such-user",
    "alert_id": "no-such-alert",
}
CALLERS = ["anonymous", P, D, F, A]


def _app_routes(app: FastAPI) -> set[tuple[str, str]]:
    paths = app.openapi()["paths"]
    return {(m.upper(), p) for p, ops in paths.items() for m in ops}


def test_matrix_covers_every_route(app: FastAPI) -> None:
    assert _app_routes(app) == set(ROUTES), "update ROUTES in this file for new/removed endpoints"


@pytest.mark.parametrize(("method", "path"), sorted(ROUTES), ids=lambda v: str(v))
def test_route_permissions(
    client: TestClient, login: Callable[..., Headers], method: str, path: str
) -> None:
    allowed = ROUTES[(method, path)]
    url = path.format(**PATH_VALUES)
    emails = {P: patient_email("P010"), D: DOCTOR_EMAILS["D1"], F: FRONTDESK, A: ADMIN}
    outcomes: dict[str, int] = {}
    for caller in CALLERS:
        headers = {} if caller == "anonymous" else dict(login(emails[caller]))
        kwargs: dict[str, object] = {"headers": headers}
        if method in {"POST", "PATCH"}:
            kwargs["json"] = {}
        r = client.request(method, url, **kwargs)  # type: ignore[arg-type]
        assert r.status_code < 500, (caller, r.status_code, r.text)
        outcomes[caller] = r.status_code

    for caller, code in outcomes.items():
        is_allowed = allowed == PUBLIC or (caller != "anonymous" and caller in allowed)
        if is_allowed:
            assert code not in (401, 403), f"{caller} should be allowed on {method} {path}: {code}"
        elif caller == "anonymous":
            assert code == 401, f"anonymous should get 401 on {method} {path}: {code}"
        else:
            assert code == 403, f"{caller} should get 403 on {method} {path}: {code}"
