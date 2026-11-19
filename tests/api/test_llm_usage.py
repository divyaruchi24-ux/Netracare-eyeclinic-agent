from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from netracare.llm.providers import FakeProvider
from netracare.llm.types import LLMUnavailable
from tests.fixtures_api import Headers
from tests.llm_helpers import resp, triage_json


def test_usage_totals_budget_and_recent_without_content(
    client: TestClient, use_llm: Callable[..., FakeProvider], admin_headers: Headers
) -> None:
    use_llm(
        [
            resp(triage_json(), tin=1000, tout=500, model="openai/gpt-oss-20b"),
            LLMUnavailable("a"),
            LLMUnavailable("b"),
            LLMUnavailable("c"),
        ]
    )
    client.post("/triage", json={"text": "my eye itches, secret-marker-123"})
    client.post("/triage", json={"text": "my eye itches again"})  # LLM down -> error row
    r = client.get("/admin/llm-usage?days=7", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 2
    assert body["by_status"] == {"ok": 1, "error": 1}
    assert body["by_purpose"]["triage"]["calls"] == 2
    assert body["by_model"]["openai/gpt-oss-20b"]["input_tokens"] == 1000
    assert abs(body["total_cost_usd"] - 0.000225) < 1e-9
    assert body["today_usd"] == body["month_to_date_usd"] == body["total_cost_usd"]
    assert body["daily_budget_usd"] == 1.5 and body["monthly_budget_usd"] == 20.0
    assert list(body["by_day"]) == ["2026-11-10"]
    assert len(body["recent"]) == 2 and body["recent"][0]["status"] in {"ok", "error"}
    assert "secret-marker-123" not in r.text


def test_usage_days_bounds(client: TestClient, admin_headers: Headers) -> None:
    assert client.get("/admin/llm-usage?days=0", headers=admin_headers).status_code == 422
    assert client.get("/admin/llm-usage", headers=admin_headers).json()["total_calls"] == 0
