"""Knowledge base: documents, index status, re-index and a retrieval debugger."""

from __future__ import annotations

from typing import Any

import streamlit as st

from netracare.ui.api_client import REINDEX_TIMEOUT_S
from netracare.ui.components import call
from netracare.ui.state import get_client

client = get_client()
status: dict[str, Any] | None = call(client.get, "/admin/kb")
if status is None:
    st.stop()

m = st.container(horizontal=True)
with m:
    st.metric("Documents", status["n_docs"], border=True)
    st.metric("Chunks", status["n_chunks"], border=True)
    st.metric("Vector index", "In sync" if status["vectors_ok"] else "Stale", border=True)
if not status["vectors_ok"]:
    st.warning(
        "The vector index does not match the documents. Answers use keyword search only "
        "until you re-index.",
        icon=":material/sync_problem:",
    )
st.caption(
    f"Embedding model `{status['embedding_model']}` · chunker v{status['chunker_version']} · "
    f"fingerprint `{(status['fingerprint'] or '')[:12]}`"
)

c1, c2 = st.columns(2)
with c1:
    if st.button("Re-index changed documents", icon=":material/sync:", key="kb_reindex"):
        with st.spinner("Re-indexing… (embedding can take a minute)"):
            if call(
                client.request, "POST", "/admin/kb/reindex", json={}, timeout=REINDEX_TIMEOUT_S
            ):
                st.rerun()
with c2:
    if st.button("Rebuild everything", icon=":material/restart_alt:", key="kb_rebuild"):
        with st.spinner("Rebuilding the whole index…"):
            if call(
                client.request,
                "POST",
                "/admin/kb/reindex",
                json={},
                params={"force": "true"},
                timeout=REINDEX_TIMEOUT_S,
            ):
                st.rerun()

refused = (status.get("report") or {}).get("refused") or []
if refused:
    st.error(
        "Some sections were not indexed because they contain instruction-like text "
        "(possible prompt injection): " + ", ".join(f"{r['slug']}#{r['ordinal']}" for r in refused),
        icon=":material/gpp_bad:",
    )

st.subheader("Documents")
st.dataframe(
    [
        {
            "Document": d["title"],
            "Slug": d["slug"],
            "Version": d["version"],
            "Effective": d["effective_date"],
            "Status": "Current" if d["is_current"] else "Superseded",
            "Chunks": d["n_chunks"],
            "File": d["source_path"],
        }
        for d in status["docs"]
    ],
    hide_index=True,
    width="stretch",
)

st.subheader("Search debugger")
with st.form("kb_search"):
    q = st.text_input("Query", placeholder="Dr Shetti timings", max_chars=300, key="kb_q")
    old = st.checkbox("Include superseded documents", key="kb_old")
    go = st.form_submit_button("Search", icon=":material/search:")
if go and q.strip():
    res = call(client.get, "/admin/kb/search", q=q, include_superseded=old or None)
    if res:
        verdict = "answerable" if res["answerable"] else "below threshold: hand off"
        st.markdown(
            f"Confidence **{res['confidence']:.3f}** (threshold {res['threshold']}) · {verdict}"
            f" · mode `{res['mode']}` · {res['latency_ms']} ms"
            + (f" · doctor match **{res['doctor']}**" if res["doctor"] else "")
        )
        st.caption(f"Normalised query: {res['normalized_query']}")
        st.dataframe(
            [
                {
                    "Rank": h["rank"],
                    "Document": f"{h['slug']} v{h['version']}",
                    "Section": h["heading_path"],
                    "Current": h["is_current"],
                    "BM25 rank": h["bm25_rank"],
                    "Vector rank": h["vector_rank"],
                    "Cosine": h["cosine"],
                    "RRF": h["rrf_score"],
                }
                for h in res["hits"]
            ],
            hide_index=True,
            width="stretch",
        )
