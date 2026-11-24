"""Chat, KB citation and handoff endpoints (Phase 3)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import ChatHandoff, TriageAlert
from netracare.guardrails import scripts
from netracare.llm.providers import FakeProvider
from tests.fixtures_api import Headers, assert_audited, fresh
from tests.helpers import MutableClock
from tests.llm_helpers import ChatScript, answer_json, route_json

UseLLM = Callable[..., FakeProvider]


def _anon(client: TestClient) -> tuple[str, dict[str, str]]:
    r = client.post("/chat/sessions")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session_token"] and body["footer"] == scripts.CHAT_FOOTER
    return body["id"], {"X-Chat-Token": body["session_token"]}


def _say(client: TestClient, sid: str, text: str, headers: dict[str, str]) -> dict[str, object]:
    r = client.post(f"/chat/sessions/{sid}/messages", json={"text": text}, headers=headers)
    assert r.status_code == 200, r.text
    body: dict[str, object] = r.json()
    return body


def test_anonymous_chat_answer_with_resolvable_citations(
    client: TestClient, use_llm: UseLLM
) -> None:
    script = ChatScript()
    script.router.append(route_json("faq_rag", "Is there parking at the clinic?"))
    script.answer.append(answer_json("Basement parking is available [1].", [1]))
    use_llm(script)
    sid, tok = _anon(client)
    body = _say(client, sid, "is there parking?", tok)
    assert body["intent"] == "faq_rag" and body["mode"] == "llm"
    assert body["footer"] == scripts.CHAT_FOOTER
    citations = body["citations"]
    assert isinstance(citations, list) and citations
    for c in citations:
        chunk = client.get(f"/kb/chunks/{c['chunk_id']}")
        assert chunk.status_code == 200
        assert chunk.json()["text"].startswith(c["excerpt"].rstrip(" …")[:40])
    history = client.get(f"/chat/sessions/{sid}", headers=tok).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    assert history["session_token"] is None  # the token is only returned once


def test_session_access_is_private(
    client: TestClient, patient_headers: Headers, login: Callable[..., Headers]
) -> None:
    sid, tok = _anon(client)
    assert client.get(f"/chat/sessions/{sid}").status_code == 404
    assert client.get(f"/chat/sessions/{sid}", headers={"X-Chat-Token": "nope"}).status_code == 404
    mine = client.post("/chat/sessions", headers=patient_headers).json()
    assert mine["session_token"] is None
    assert client.get(f"/chat/sessions/{mine['id']}", headers=patient_headers).status_code == 200
    from tests.fixtures_api import patient_email

    other = login(patient_email("P011"))
    assert client.get(f"/chat/sessions/{mine['id']}", headers=other).status_code == 404
    r = client.post(f"/chat/sessions/{mine['id']}/messages", json={"text": "hi"}, headers=other)
    assert r.status_code == 404
    assert client.get(f"/chat/sessions/{mine['id']}", headers=tok).status_code == 404


def test_emergency_in_chat_is_verbatim_and_alerts_once(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    sid = client.post("/chat/sessions", headers=patient_headers).json()["id"]
    text = "lol bleach got in my eye, anyway how much is a check-up?"
    first = _say(client, sid, text, patient_headers)
    assert first["intent"] == "emergency"
    assert first["reply"] == scripts.emergency_message(["E4"])
    assert first["citations"] == [] and first["suggested_action"] is None
    second = _say(client, sid, text, patient_headers)
    assert second["safety"]["alert_id"] == first["safety"]["alert_id"]  # type: ignore[index]
    assert fresh(db).scalar(select(func.count()).select_from(TriageAlert)) == 1


def test_out_of_kb_gets_low_confidence_script(client: TestClient, use_llm: UseLLM) -> None:
    script = ChatScript()
    script.router.append(route_json("faq_rag", "Do you offer hair transplants?"))
    use_llm(script)
    sid, tok = _anon(client)
    body = _say(client, sid, "do you do hair transplants?", tok)
    assert body["mode"] == "refused" and str(body["reply"]).startswith(scripts.LOW_CONFIDENCE)
    assert script.seen == ["router"]


def test_llm_disabled_still_answers_extractively(client: TestClient) -> None:
    sid, tok = _anon(client)
    body = _say(client, sid, "is there parking at the clinic", tok)
    assert body["mode"] == "extractive"
    assert str(body["reply"]).startswith(scripts.EXTRACTIVE_INTRO) and body["citations"]


def test_message_validation_and_rate_limit(client: TestClient, clock: MutableClock) -> None:
    sid, tok = _anon(client)
    r = client.post(f"/chat/sessions/{sid}/messages", json={"text": "x" * 2001}, headers=tok)
    assert r.status_code == 422
    # 10/min for anonymous callers: session creation used one; the 422 failed validation
    # before reaching the limiter, so it does not count.
    for i in range(9):
        _say(client, sid, f"hello {i}", tok)
    r = client.post(f"/chat/sessions/{sid}/messages", json={"text": "hello"}, headers=tok)
    assert r.status_code == 429
    clock.advance(timedelta(minutes=2))
    ok = client.post(f"/chat/sessions/{sid}/messages", json={"text": "hi"}, headers=tok)
    assert ok.status_code == 200


def test_ended_session_returns_409(client: TestClient) -> None:
    sid, tok = _anon(client)
    for _ in range(3):
        body = _say(client, sid, "BUY FOLLOWERS", tok)
    assert body["reply"] == scripts.SPAM_END and body["session_status"] == "ended"
    r = client.post(f"/chat/sessions/{sid}/messages", json={"text": "hello"}, headers=tok)
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_ended"


def test_booking_suggestion(client: TestClient, use_llm: UseLLM) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book a child's eye check-up"))
    use_llm(script)
    sid, tok = _anon(client)
    body = _say(client, sid, "My 6 year old needs a routine eye check-up, can I book?", tok)
    action = body["suggested_action"]
    assert isinstance(action, dict) and action["doctor_name"] == "Dr. Arjun Menon"
    assert action["type"] == "book" and action["specialty"] == "PEDIATRIC"


def test_handoff_flow_for_front_desk(
    client: TestClient, db: Session, patient_headers: Headers, frontdesk_headers: Headers
) -> None:
    sid = client.post("/chat/sessions", headers=patient_headers).json()["id"]
    body = _say(client, sid, "my email is a@b.com, please call me back", patient_headers)
    assert body["reply"] == scripts.HANDOFF_CREATED and body["handoff_id"]
    hid = str(body["handoff_id"])
    button = client.post(f"/chat/sessions/{sid}/handoff", headers=patient_headers).json()
    assert button["handoff_id"] == hid  # idempotent

    listing = client.get("/chat/handoffs", headers=frontdesk_headers).json()
    item = next(i for i in listing["items"] if i["id"] == hid)
    assert listing["open_total"] >= 1 and item["patient_name"] and not item["anonymous"]
    assert "a@b.com" not in item["summary_redacted"]
    convo = client.get(f"/chat/handoffs/{hid}/conversation", headers=frontdesk_headers).json()
    assert [m["seq"] for m in convo["messages"]] == [1, 2]

    claim = client.post(f"/chat/handoffs/{hid}/claim", json={}, headers=frontdesk_headers)
    assert claim.status_code == 200 and claim.json()["status"] == "claimed"
    again = client.post(f"/chat/handoffs/{hid}/claim", json={}, headers=frontdesk_headers)
    assert again.status_code == 409
    closed = client.post(
        f"/chat/handoffs/{hid}/close", json={"note": "called back"}, headers=frontdesk_headers
    )
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    assert_audited(db, "chat.handoff_close", hid)
    assert fresh(db).get(ChatHandoff, hid).close_note == "called back"  # type: ignore[union-attr]


def test_anonymous_handoff_gives_phone_number(client: TestClient) -> None:
    sid, tok = _anon(client)
    body = client.post(f"/chat/sessions/{sid}/handoff", headers=tok).json()
    assert body["reply"] == scripts.ANON_HANDOFF and body["handoff_id"] is None


def test_kb_public_endpoints(client: TestClient) -> None:
    docs = client.get("/kb/docs").json()
    old = next(d for d in docs if d["slug"] == "price-list" and d["version"] == 1)
    new = next(d for d in docs if d["slug"] == "price-list" and d["version"] == 2)
    assert not old["is_current"] and new["is_current"] and new["n_chunks"] >= 1
    assert client.get("/kb/chunks/missing").status_code == 404


def test_admin_kb_status_search_and_reindex(
    client: TestClient, admin_headers: Headers, db: Session
) -> None:
    status = client.get("/admin/kb", headers=admin_headers).json()
    assert status["vectors_ok"] and status["n_chunks"] > 70 and status["fingerprint"]
    search = client.get(
        "/admin/kb/search", params={"q": "Dr Shetti timings"}, headers=admin_headers
    )
    body = search.json()
    assert search.status_code == 200 and body["doctor"] == "Dr. Vikram Shetty"
    assert body["hits"][0]["slug"] == "dr-vikram-shetty"
    assert all(h["is_current"] for h in body["hits"])
    old = client.get(
        "/admin/kb/search",
        params={"q": "price list 2025", "include_superseded": "true"},
        headers=admin_headers,
    ).json()
    assert any(not h["is_current"] for h in old["hits"])
    re = client.post("/admin/kb/reindex", headers=admin_headers)
    assert re.status_code == 200, re.text
    assert re.json()["fingerprint"] == status["fingerprint"] and re.json()["vectors_ok"]
    assert_audited(db, "kb.reindex")
    forced = client.post("/admin/kb/reindex", params={"force": "true"}, headers=admin_headers)
    assert forced.json()["report"]["rebuilt_vectors"] is True
