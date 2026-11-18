from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from netracare.errors import ConflictError


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/health")
    assert len(r.headers["x-request-id"]) >= 8
    r2 = client.get("/health", headers={"X-Request-ID": "abc12345-good-id"})
    assert r2.headers["x-request-id"] == "abc12345-good-id"


def test_invalid_inbound_request_id_replaced(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "bad id <script>"})
    assert r.headers["x-request-id"] != "bad id <script>"


def test_app_error_shape(app: FastAPI) -> None:
    @app.get("/_boom_conflict")
    def _boom() -> None:
        raise ConflictError("slot_unavailable", "taken", details={"slot": "x"})

    r = TestClient(app).get("/_boom_conflict", headers={"X-Request-ID": "req-12345678"})
    assert r.status_code == 409
    assert r.json() == {
        "error": {
            "code": "slot_unavailable",
            "message": "taken",
            "details": {"slot": "x"},
            "request_id": "req-12345678",
        }
    }


def test_validation_error_shape_hides_input(client: TestClient) -> None:
    r = client.post("/auth/login", json={"email": "a@example.com", "password": ""})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert all("input" not in d for d in err["details"])


def test_unhandled_error_is_500_without_traceback(app: FastAPI) -> None:
    @app.get("/_boom")
    def _boom() -> None:
        raise RuntimeError("secret internals")

    r = TestClient(app, raise_server_exceptions=False).get("/_boom")
    assert r.status_code == 500
    assert "secret internals" not in r.text
    assert r.json()["error"]["code"] == "internal_error"


def test_unknown_route_404_shape(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
