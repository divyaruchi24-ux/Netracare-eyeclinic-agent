"""Tune retrieval: k, rerank on/off (and rerank depth), confidence signal and threshold.

Runs the real retriever (fastembed models) over evals/datasets/rag_retrieval_v0.jsonl and
writes reports/evals/rag_sweep-<ts>.{json,md}. No LLM calls.

    python -m uv run python scripts/rag_sweep.py

Selection rule for the refusal threshold (docs/phase-3/understanding.md 3.6): the value that
keeps in-KB answerable >= 0.95 while refusing as many out-of-KB questions as possible.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netracare.config import get_settings
from netracare.evals.rag_tasks import make_index
from netracare.evals.schema import load_dataset
from netracare.rag.embeddings import sigmoid

CONFIGS: list[dict[str, Any]] = [
    {"name": "hybrid, no rerank", "rag_rerank": False},
    {"name": "hybrid + rerank top-5", "rag_rerank": True, "rag_rerank_k": 5},
    {"name": "hybrid + rerank top-10", "rag_rerank": True, "rag_rerank_k": 10},
]
KS = (1, 3, 5, 8)
THRESHOLDS = [round(0.40 + 0.005 * i, 3) for i in range(111)]  # 0.400 .. 0.950
MIN_IN_KB = 0.95


def _run(config: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings().model_copy(
        update={k: v for k, v in config.items() if k != "name"} | {"rag_top_k": max(KS)}
    )
    index = make_index(settings)
    dataset = load_dataset(Path("evals/datasets/rag_retrieval_v0.jsonl"))
    rows = []
    for case in dataset.cases:
        started = time.perf_counter()
        r = index.retrieve(str(case.input["query"]))
        ms = (time.perf_counter() - started) * 1000
        top_rerank = r.chunks[0].rerank_score if r.chunks else None
        rows.append(
            {
                "id": case.id,
                "expected": case.expected,
                "slugs": [c.chunk.slug for c in r.chunks],
                "all_current": all(c.chunk.is_current for c in r.chunks),
                # The retriever's no-rerank confidence: the best cosine of the vector stage.
                "cosine": r.stages["vector"][0][1] if r.stages.get("vector") else 0.0,
                "rerank": None if top_rerank is None else sigmoid(top_rerank),
                "ms": ms,
            }
        )
    return rows


def _recall(rows: list[dict[str, Any]], k: int) -> float:
    ans = [r for r in rows if r["expected"].get("slugs")]
    hit = sum(any(s in r["expected"]["slugs"] for s in r["slugs"][:k]) for r in ans)
    return hit / len(ans)


def _mrr(rows: list[dict[str, Any]], k: int) -> float:
    ans = [r for r in rows if r["expected"].get("slugs")]
    total = 0.0
    for r in ans:
        for rank, s in enumerate(r["slugs"][:k], start=1):
            if s in r["expected"]["slugs"]:
                total += 1 / rank
                break
    return total / len(ans)


def _threshold_table(rows: list[dict[str, Any]], signal: str) -> list[dict[str, float]]:
    ans = [r for r in rows if r["expected"].get("slugs")]
    oob = [r for r in rows if r["expected"].get("refuse")]
    table = []
    for t in THRESHOLDS:
        in_kb = sum((r[signal] or 0.0) >= t for r in ans) / len(ans)
        refused = sum((r[signal] or 0.0) < t for r in oob) / len(oob)
        table.append({"threshold": t, "in_kb_answerable": in_kb, "out_of_kb_refusal": refused})
    return table


def _pick(table: list[dict[str, float]]) -> dict[str, float]:
    ok = [row for row in table if row["in_kb_answerable"] >= MIN_IN_KB]
    if not ok:
        return max(table, key=lambda r: (r["in_kb_answerable"], r["out_of_kb_refusal"]))
    return max(ok, key=lambda r: (r["out_of_kb_refusal"], r["in_kb_answerable"], -r["threshold"]))


def main() -> None:
    results = []
    for config in CONFIGS:
        rows = _run(config)
        latencies = [r["ms"] for r in rows]
        signals = ["cosine"] + (["rerank"] if config["rag_rerank"] else [])
        entry = {
            "config": config,
            "recall": {f"@{k}": round(_recall(rows, k), 4) for k in KS},
            "mrr@5": round(_mrr(rows, 5), 4),
            "no_superseded_leak": all(r["all_current"] for r in rows),
            "latency_ms": {
                "p50": round(statistics.median(latencies), 1),
                "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1),
            },
            "thresholds": {s: _pick(_threshold_table(rows, s)) for s in signals},
            "misses@5": [
                r["id"]
                for r in rows
                if r["expected"].get("slugs")
                and not any(s in r["expected"]["slugs"] for s in r["slugs"][:5])
            ],
            "cases": rows,
        }
        results.append(entry)
        print(
            f"{config['name']:26} recall@1/3/5={entry['recall']['@1']:.3f}/"
            f"{entry['recall']['@3']:.3f}/{entry['recall']['@5']:.3f} mrr={entry['mrr@5']:.3f} "
            f"p50={entry['latency_ms']['p50']}ms p95={entry['latency_ms']['p95']}ms "
            f"misses={entry['misses@5']}"
        )
        for sig, pick in entry["thresholds"].items():
            print(
                f"    {sig:7} threshold={pick['threshold']:.2f} "
                f"in_kb={pick['in_kb_answerable']:.3f} refuse_oob={pick['out_of_kb_refusal']:.3f}"
            )

    out = Path("reports/evals")
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    (out / f"rag_sweep-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    lines = [
        "| config | recall@1 | @3 | @5 | MRR@5 | p50 ms | p95 ms | signal | threshold "
        "| in-KB answerable | out-of-KB refused |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in results:
        for sig, pick in e["thresholds"].items():
            lines.append(
                f"| {e['config']['name']} | {e['recall']['@1']} | {e['recall']['@3']} "
                f"| {e['recall']['@5']} | {e['mrr@5']} | {e['latency_ms']['p50']} "
                f"| {e['latency_ms']['p95']} | {sig} | {pick['threshold']:.2f} "
                f"| {pick['in_kb_answerable']:.3f} | {pick['out_of_kb_refusal']:.3f} |"
            )
    (out / f"rag_sweep-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote reports/evals/rag_sweep-{stamp}.json/.md")


if __name__ == "__main__":
    main()
