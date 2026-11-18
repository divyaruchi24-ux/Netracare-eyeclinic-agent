from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.models import User
from tests.fixtures_api import (
    ADMIN,
    FRONTDESK,
    Headers,
    assert_audited,
    did,
    fresh,
)

STAFF = {
    "email": "newdesk@netracare.example",
    "display_name": "Asha (Front Desk)",
    "role": "front_desk",
    "temp_password": "Temp-pass-123",
}


def _user_id(db: Session, email: str) -> str:
    u = fresh(db).scalar(select(User).where(User.email == email))
    assert u is not None
    return u.id


def test_list_users_filters(client: TestClient, admin_headers: Headers) -> None:
    r = client.get("/users", params={"role": "doctor"}, headers=admin_headers).json()
    assert r["total"] == 5 and all(u["doctor_id"] for u in r["items"])
    q = client.get("/users", params={"q": "front desk"}, headers=admin_headers).json()
    assert {u["email"] for u in q["items"]} == {FRONTDESK, "frontdesk2@netracare.example"}


def test_create_staff_and_login(client: TestClient, db: Session, admin_headers: Headers) -> None:
    r = client.post("/users", json=STAFF, headers=admin_headers)
    assert r.status_code == 201, r.text
    assert_audited(db, "user.create", r.json()["id"])
    login = client.post(
        "/auth/login", json={"email": STAFF["email"], "password": STAFF["temp_password"]}
    )
    assert login.status_code == 200
    assert client.post("/users", json=STAFF, headers=admin_headers).status_code == 409


def test_create_staff_validation(client: TestClient, admin_headers: Headers) -> None:
    def post(**kw: object) -> int:
        return client.post("/users", json={**STAFF, **kw}, headers=admin_headers).status_code

    assert post(role="patient") == 422
    assert post(role="doctor") == 422  # doctor needs doctor_id
    assert post(doctor_id=did("D1")) == 422  # front desk must not have one
    assert post(role="doctor", doctor_id=did("D1")) == 409  # D1 already has an account
    assert post(role="doctor", doctor_id="nope") == 404
    assert post(temp_password="short") == 422


def test_deactivate_revokes_sessions(
    client: TestClient, db: Session, admin_headers: Headers
) -> None:
    pair = client.post(
        "/auth/login", json={"email": FRONTDESK, "password": "NetraCare#Dev1"}
    ).json()
    uid = _user_id(db, FRONTDESK)
    r = client.patch(f"/users/{uid}", json={"is_active": False}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (
        client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code
        == 401
    )
    assert_audited(db, "user.update", uid)
    back = client.patch(f"/users/{uid}", json={"is_active": True}, headers=admin_headers)
    assert back.json()["is_active"] is True


def test_role_change_rules(client: TestClient, db: Session, admin_headers: Headers) -> None:
    fd = _user_id(db, FRONTDESK)
    promoted = client.patch(f"/users/{fd}", json={"role": "admin"}, headers=admin_headers)
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
    doc = _user_id(db, "kavya.nair@netracare.example")
    assert (
        client.patch(f"/users/{doc}", json={"role": "admin"}, headers=admin_headers).status_code
        == 422
    )
    pat = _user_id(db, "admin@netracare.example")
    assert (
        client.patch(f"/users/{pat}", json={"role": "patient"}, headers=admin_headers).status_code
        == 422
    )


def test_cannot_deactivate_self_or_last_admin(
    client: TestClient, db: Session, admin_headers: Headers, login: Callable[..., Headers]
) -> None:
    me = _user_id(db, ADMIN)
    r = client.patch(f"/users/{me}", json={"is_active": False}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "cannot_demote_self"
    # promote frontdesk1 to admin, then let them try to remove the original admin twice
    fd = _user_id(db, FRONTDESK)
    client.patch(f"/users/{fd}", json={"role": "admin"}, headers=admin_headers)
    fd_headers = login(FRONTDESK)
    ok = client.patch(f"/users/{me}", json={"role": "front_desk"}, headers=fd_headers)
    assert ok.status_code == 200
    last = client.patch(f"/users/{fd}", json={"role": "front_desk"}, headers=fd_headers)
    assert last.status_code == 409
    assert (
        client.patch("/users/nope", json={"is_active": True}, headers=fd_headers).status_code == 404
    )


def test_audit_log_search(client: TestClient, admin_headers: Headers) -> None:
    client.post("/auth/login", json={"email": ADMIN, "password": "wrong-pass-1"})
    r = client.get("/audit-logs", params={"action": "auth.login"}, headers=admin_headers).json()
    actions = {i["action"] for i in r["items"]}
    assert {"auth.login", "auth.login_failed"} <= actions
    assert all(i["request_id"] for i in r["items"])
    seed = client.get("/audit-logs", params={"entity_type": "system"}, headers=admin_headers).json()
    assert seed["items"][0]["action"] == "seed.run"
    window = client.get(
        "/audit-logs",
        params={"from": "2000-01-01T00:00:00Z", "to": "2000-01-02T00:00:00Z"},
        headers=admin_headers,
    ).json()
    assert window["total"] == 0
    by_actor = client.get(
        "/audit-logs", params={"actor_user_id": "nobody", "entity_id": "x"}, headers=admin_headers
    ).json()
    assert by_actor["total"] == 0


def test_admin_routes_forbidden_for_others(
    client: TestClient, frontdesk_headers: Headers, patient_headers: Headers
) -> None:
    for h in (frontdesk_headers, patient_headers):
        assert client.get("/users", headers=h).status_code == 403
        assert client.get("/audit-logs", headers=h).status_code == 403
