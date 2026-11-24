"""Eval tasks for the chat router (Phase 3).

* `router_rules` - layer 1 only (deterministic; CI gate): hidden emergencies, medical-advice and
  lab-report requests, human requests, empty/emoji/spam, and no false positives on ordinary
  questions (expected intent "defer" = left for the LLM router).
* `router_llm` - the router as deployed: layer 1, then `intent_router@v1` on Groq (`--live`).
"""

from __future__ import annotations

from typing import Any

from netracare.agents.router_rules import layer1


def router_rules_task(inp: dict[str, Any]) -> dict[str, Any]:
    d = layer1(str(inp["message"]), list(inp.get("previous") or []))
    return {
        "intent": d.intent.value if d.intent else "defer",
        "reason": d.reason,
        "codes": list(d.codes),
        "injection": list(d.injection),
    }


def router_llm_task(inp: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - live
    from netracare.agents.router import Router
    from netracare.evals.tasks import live_llm

    text = str(inp["message"])
    d1 = layer1(text, list(inp.get("previous") or []))
    if d1.intent is not None:
        return {"intent": d1.intent.value, "source": "rules", "standalone_query": text}
    r = Router(live_llm()).route(text, str(inp.get("history") or ""))
    return {
        "intent": r.intent.value,
        "source": r.source,
        "confidence": r.confidence,
        "standalone_query": r.standalone_query,
        "error": r.error,
    }
