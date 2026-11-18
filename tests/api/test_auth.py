from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.models import Patient, User
from tests.fixtures_api import (
    ADMIN,
    DOCTOR_EMAILS,
    FRONTDESK,
    PASSWORD,
    assert_audited,
    guardian_email,
    patient_email,
    pid,
)
from tests.helpers import MutableClock

NEW = {
    "email": "new.patient@example.com",
    "password": "Str0ng-pass!",
    "full_name": "Asha Verma",
    "dob": "1990-05-04",
    "phone": "+91 98765 43210",
}


def _login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, object]:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body: dict[str, object] = r.json()
    return body


def _bearer(tok: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def test_register_creates_patient_and_returns_tokens(client: TestClient, db: Session) -> None:
    r = client.post("/auth/register", json=NEW)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    me = client.get("/auth/me", headers=_bearer(body["access_token"])).json()
    assert me["role"] == "patient" and len(me["patient_ids"]) == 1
    patient = db.get(Patient, me["patient_ids"][0])
    assert patient is not None and patient.consent_captured_at is not None
    assert_audited(db, "auth.register", me["user_id"])


def test_register_duplicate_email_409(client: TestClient) -> None:
    assert client.post("/auth/register", json=NEW).status_code == 201
    dup = {**NEW, "email": NEW["email"].upper()}
    r = client.post("/auth/register", json=dup)
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", "short"),
        ("email", "not-an-email"),
        ("full_name", "   "),
        ("phone", "call me"),
        ("full_name", "x" * 200),
    ],
)
def test_register_invalid_fields_422(client: TestClient, field: str, value: str) -> None:
    r = client.post("/auth/register", json={**NEW, field: value})
    assert r.status_code == 422


def test_register_future_dob_422(client: TestClient) -> None:
    r = client.post("/auth/register", json={**NEW, "dob": "2030-01-01"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_dob"


def test_register_minor_rejected(client: TestClient) -> None:
    r = client.post("/auth/register", json={**NEW, "dob": "2015-01-01"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "minor_self_registration"


def test_register_unknown_field_rejected(client: TestClient) -> None:
    r = client.post("/auth/register", json={**NEW, "role": "admin"})
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("email", "role"),
    [
        (ADMIN, "admin"),
        (FRONTDESK, "front_desk"),
        (DOCTOR_EMAILS["D1"], "doctor"),
        (patient_email("P010"), "patient"),
    ],
)
def test_login_ok_each_seeded_role(client: TestClient, email: str, role: str) -> None:
    tok = _login(client, email)["access_token"]
    me = client.get("/auth/me", headers=_bearer(tok)).json()
    assert me["role"] == role and me["email"] == email


def test_doctor_me_has_doctor_id(client: TestClient) -> None:
    tok = _login(client, DOCTOR_EMAILS["D5"])["access_token"]
    me = client.get("/auth/me", headers=_bearer(tok)).json()
    assert me["doctor_id"] and me["display_name"].startswith("Dr.")


def test_login_wrong_password_401_generic(client: TestClient, db: Session) -> None:
    r = client.post("/auth/login", json={"email": ADMIN, "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "invalid email or password"
    assert r.headers["www-authenticate"] == "Bearer"
    row = assert_audited(db, "auth.login_failed")  # committed despite the error
    assert row.after_json == {"reason": "bad_credentials"}


def test_login_unknown_email_401_same_message(client: TestClient) -> None:
    r = client.post("/auth/login", json={"email": "ghost@example.com", "password": "whatever-1"})
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "invalid email or password"


def test_login_throttled_after_5_failures_429(client: TestClient, clock: MutableClock) -> None:
    for _ in range(5):
        client.post("/auth/login", json={"email": ADMIN, "password": "nope-nope-1"})
    r = client.post("/auth/login", json={"email": ADMIN, "password": PASSWORD})
    assert r.status_code == 429  # even the right password is refused while throttled
    clock.advance(timedelta(minutes=16))
    assert (
        client.post("/auth/login", json={"email": ADMIN, "password": PASSWORD}).status_code == 200
    )


def test_refresh_rotates(client: TestClient) -> None:
    pair = _login(client, ADMIN)
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != pair["refresh_token"]
    assert client.get("/auth/me", headers=_bearer(new["access_token"])).status_code == 200


def test_refresh_reuse_revokes_family(client: TestClient, db: Session) -> None:
    pair = _login(client, ADMIN)
    rotated = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]}).json()
    reuse = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert reuse.status_code == 401 and reuse.json()["error"]["code"] == "refresh_token_reused"
    # the legitimately rotated token is now dead too
    r = client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert r.status_code == 401
    assert_audited(db, "auth.refresh_reuse")


def test_refresh_expired_401(client: TestClient, clock: MutableClock) -> None:
    pair = _login(client, ADMIN)
    clock.advance(timedelta(days=8))
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401


def test_refresh_garbage_401(client: TestClient) -> None:
    r = client.post("/auth/refresh", json={"refresh_token": "x" * 40})
    assert r.status_code == 401


def test_logout_revokes(client: TestClient) -> None:
    pair = _login(client, ADMIN)
    assert (
        client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]}).status_code
        == 204
    )
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401
    # logging out twice / with an unknown token is still a quiet 204
    assert client.post("/auth/logout", json={"refresh_token": "y" * 40}).status_code == 204


def test_expired_access_token_401(client: TestClient, clock: MutableClock) -> None:
    tok = _login(client, ADMIN)["access_token"]
    clock.advance(timedelta(minutes=15))
    r = client.get("/auth/me", headers=_bearer(tok))
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"


def test_tampered_token_401(client: TestClient) -> None:
    tok = str(_login(client, patient_email("P010"))["access_token"])
    head, payload, sig = tok.split(".")
    r = client.get("/auth/me", headers=_bearer(f"{head}.{payload}x.{sig}"))
    assert r.status_code == 401


def test_refresh_token_as_access_401(client: TestClient) -> None:
    pair = _login(client, ADMIN)
    assert client.get("/auth/me", headers=_bearer(pair["refresh_token"])).status_code == 401


def test_missing_token_401(client: TestClient) -> None:
    r = client.get("/auth/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"


def test_inactive_user_locked_out(client: TestClient, db: Session) -> None:
    pair = _login(client, FRONTDESK)
    user = db.scalar(select(User).where(User.email == FRONTDESK))
    assert user is not None
    user.is_active = False
    db.commit()
    assert client.get("/auth/me", headers=_bearer(pair["access_token"])).status_code == 401
    assert (
        client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code
        == 401
    )
    r = client.post("/auth/login", json={"email": FRONTDESK, "password": PASSWORD})
    assert r.status_code == 401 and r.json()["error"]["message"] == "invalid email or password"


def test_me_patient_lists_own_and_dependants(client: TestClient) -> None:
    # The guardian of P000 (a minor) has no own patient record; they act for P000.
    tok = _login(client, guardian_email("P000"))["access_token"]
    me = client.get("/auth/me", headers=_bearer(tok)).json()
    assert me["patient_ids"] == [pid("P000")]
    tok2 = _login(client, patient_email("P010"))["access_token"]
    assert client.get("/auth/me", headers=_bearer(tok2)).json()["patient_ids"] == [pid("P010")]


def test_example_tld_email_accepted(client: TestClient) -> None:
    r = client.post("/auth/register", json={**NEW, "email": "someone@clinic.example.com"})
    assert r.status_code == 201, r.text


def test_login_is_case_insensitive(client: TestClient, login: Callable[..., object]) -> None:
    r = client.post("/auth/login", json={"email": ADMIN.upper(), "password": PASSWORD})
    assert r.status_code == 200
