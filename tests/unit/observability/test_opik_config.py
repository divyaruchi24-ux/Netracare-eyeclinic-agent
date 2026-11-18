from __future__ import annotations

from typing import Any

import pytest

from netracare.config import Settings
from netracare.observability import opik_config
from netracare.observability.opik_config import apply_opik_env, resolve_workspace


def _s(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_explicit_workspace_wins() -> None:
    assert resolve_workspace(_s(opik_api_key="k" * 20, opik_workspace="team")) == "team"


def test_self_hosted_and_keyless_have_no_workspace() -> None:
    assert (
        resolve_workspace(_s(opik_api_key="k" * 20, opik_url_override="http://opik:5173")) is None
    )
    assert resolve_workspace(_s()) is None


def test_default_workspace_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake(key: str, base: str) -> str:
        calls.append((key, base))
        return "my-default"

    monkeypatch.setattr(opik_config, "_default_workspace", fake)
    assert resolve_workspace(_s(opik_api_key="k" * 20)) == "my-default"
    assert calls == [("k" * 20, opik_config.COMET_BASE_URL)]


class _Resp:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code, self._body = status, body

    def json(self) -> dict[str, Any]:
        return self._body


class _Client:
    def __init__(self, resp: _Resp | Exception) -> None:
        self.resp = resp

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def get(self, url: str, headers: dict[str, str]) -> _Resp:
        assert headers["Authorization"] == "secret-key"
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.mark.parametrize(
    ("resp", "expected"),
    [
        (_Resp(200, {"defaultWorkspaceName": "ws"}), "ws"),
        (_Resp(200, {}), None),
        (_Resp(401, {}), None),
        (RuntimeError("offline"), None),
    ],
)
def test_default_workspace_http(
    monkeypatch: pytest.MonkeyPatch, resp: _Resp | Exception, expected: str | None
) -> None:
    import httpx

    opik_config._default_workspace.cache_clear()
    monkeypatch.setattr(httpx, "Client", lambda **_: _Client(resp))
    assert opik_config._default_workspace("secret-key", "https://example.invalid/") == expected
    opik_config._default_workspace.cache_clear()


def test_apply_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPIK_API_KEY", "OPIK_WORKSPACE", "OPIK_PROJECT_NAME", "OPIK_URL_OVERRIDE"):
        monkeypatch.setenv(var, "")  # recorded, so monkeypatch restores the original on exit
        monkeypatch.delenv(var)
    assert apply_opik_env(_s()) is False
    s = _s(opik_api_key="k" * 20, opik_workspace="team", opik_url_override="http://opik")
    assert apply_opik_env(s) is True
    import os

    assert os.environ["OPIK_API_KEY"] == "k" * 20 and os.environ["OPIK_WORKSPACE"] == "team"
    assert os.environ["OPIK_URL_OVERRIDE"] == "http://opik"
