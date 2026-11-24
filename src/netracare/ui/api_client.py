"""HTTP client the Streamlit UI uses to talk to the NetraCare API (it never touches the DB).

Tokens live only on this object, which lives in the user's `st.session_state`. A 401 triggers
one silent refresh-and-retry; if that fails the session is cleared and the user signs in again.
The transport is injectable: production uses `requests.Session`, tests use FastAPI's TestClient.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

CHAT_TIMEOUT_S = 90.0  # a turn may run router + triage + answer LLM calls
REINDEX_TIMEOUT_S = 600.0  # embedding the whole KB on a laptop CPU takes about a minute


class _Response(Protocol):
    status_code: int
    text: str

    @property
    def headers(self) -> Any: ...

    def json(self) -> Any: ...


class Transport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details


@dataclass
class ApiClient:
    base_url: str
    http: Any = field(default_factory=requests.Session)  # Transport-compatible
    timeout: float | None = 15.0  # None: let the transport decide (e.g. TestClient)
    access_token: str | None = None
    refresh_token: str | None = None
    me: dict[str, Any] | None = None
    chat_tokens: dict[str, str] = field(default_factory=dict)  # anonymous chat session tokens

    # -- session -----------------------------------------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return self.access_token is not None and self.me is not None

    @property
    def role(self) -> str | None:
        return str(self.me["role"]) if self.me else None

    def _store(self, pair: dict[str, Any]) -> None:
        self.access_token = pair["access_token"]
        self.refresh_token = pair["refresh_token"]

    def clear(self) -> None:
        self.access_token = self.refresh_token = None
        self.me = None

    def login(self, email: str, password: str) -> dict[str, Any]:
        pair = self.request(
            "POST", "/auth/login", json={"email": email, "password": password}, auth=False
        )
        self._store(pair)
        self.me = self.request("GET", "/auth/me")
        return self.me

    def register(self, **fields: Any) -> dict[str, Any]:
        pair = self.request("POST", "/auth/register", json=fields, auth=False)
        self._store(pair)
        self.me = self.request("GET", "/auth/me")
        return self.me

    def refresh(self) -> bool:
        if not self.refresh_token:
            return False
        try:
            pair = self.request(
                "POST", "/auth/refresh", json={"refresh_token": self.refresh_token}, auth=False
            )
        except ApiError:
            self.clear()
            return False
        self._store(pair)
        return True

    def logout(self) -> None:
        if self.refresh_token:
            with contextlib.suppress(ApiError):
                self.request(
                    "POST", "/auth/logout", json={"refresh_token": self.refresh_token}, auth=False
                )
        self.clear()

    # -- transport ---------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
        _retry: bool = True,
    ) -> Any:
        hdrs = dict(headers or {})
        if auth and self.access_token:
            hdrs["Authorization"] = f"Bearer {self.access_token}"
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            # timeout=None on the client means "the transport decides" (tests): keep it so.
            wait = None if self.timeout is None else (timeout or self.timeout)
            extra = {"timeout": wait} if wait is not None else {}
            resp: _Response = self.http.request(
                method,
                self.base_url.rstrip("/") + path,
                json=json,
                params=clean or None,
                headers=hdrs,
                **extra,
            )
        except requests.RequestException as exc:
            raise ApiError(0, "api_unreachable", "the clinic server is not reachable") from exc
        if resp.status_code == 401 and auth and _retry and self.refresh():
            return self.request(
                method,
                path,
                json=json,
                params=params,
                headers=headers,
                auth=auth,
                timeout=timeout,
                _retry=False,
            )
        if resp.status_code == 204:
            return None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
            if resp.status_code == 401 and auth:
                self.clear()
            raise ApiError(
                resp.status_code,
                err.get("code", "http_error"),
                err.get("message", resp.text[:200] or "request failed"),
                err.get("request_id") or resp.headers.get("x-request-id"),
                err.get("details"),
            )
        return body

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None, *, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self.request("POST", path, json=body if body is not None else {}, headers=headers)

    def patch(self, path: str, body: Any, **params: Any) -> Any:
        return self.request("PATCH", path, json=body, params=params)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    # -- chat (Phase 3) ------------------------------------------------------------------------
    def _chat_headers(self, session_id: str) -> dict[str, str] | None:
        token = self.chat_tokens.get(session_id)
        return {"X-Chat-Token": token} if token else None

    def chat_create(self) -> dict[str, Any]:
        body: dict[str, Any] = self.request("POST", "/chat/sessions", json={})
        if body.get("session_token"):
            self.chat_tokens[body["id"]] = body["session_token"]
        return body

    def chat_get(self, session_id: str) -> dict[str, Any]:
        body: dict[str, Any] = self.request(
            "GET", f"/chat/sessions/{session_id}", headers=self._chat_headers(session_id)
        )
        return body

    def chat_send(self, session_id: str, text: str) -> dict[str, Any]:
        body: dict[str, Any] = self.request(
            "POST",
            f"/chat/sessions/{session_id}/messages",
            json={"text": text},
            headers=self._chat_headers(session_id),
            timeout=CHAT_TIMEOUT_S,
        )
        return body

    def chat_handoff(self, session_id: str) -> dict[str, Any]:
        body: dict[str, Any] = self.request(
            "POST",
            f"/chat/sessions/{session_id}/handoff",
            json={},
            headers=self._chat_headers(session_id),
        )
        return body


def new_idempotency_key() -> str:
    return f"ui-{uuid.uuid4().hex}"
