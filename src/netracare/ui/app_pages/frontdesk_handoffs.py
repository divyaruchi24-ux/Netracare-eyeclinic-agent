"""Chat handoffs: conversations the assistant passed to the front desk."""

from __future__ import annotations

from typing import Any

import streamlit as st

from netracare.ui.components import call, fmt_local, md_lines
from netracare.ui.state import get_client

client = get_client()
REASONS = {
    "user_request": "Asked for a person",
    "low_confidence": "Assistant could not answer",
    "router_uncertain": "Unclear request",
    "frustration": "Frustrated with the assistant",
    "spam": "Repeated messages (ended)",
    "budget": "AI budget reached",
}

view = st.segmented_control(
    "Show", ["Open", "Claimed", "All"], default="Open", key="handoffs_view", required=True
)
status = {"Open": "open", "Claimed": "claimed", "All": None}[view]
data = call(client.get, "/chat/handoffs", status=status) or {"items": [], "open_total": 0}
st.metric("Open handoffs", data["open_total"], border=True)
items: list[dict[str, Any]] = data["items"]
if not items:
    st.info("No handoffs here.", icon=":material/check_circle:")

for h in items:
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        with top:
            st.badge(REASONS.get(h["reason"], h["reason"]), icon=":material/support_agent:")
            st.badge(h["status"].capitalize(), color="gray")
            st.caption(fmt_local(h["created_at"]))
        who = h["patient_name"] or ("Anonymous visitor" if h["anonymous"] else "Signed-in user")
        st.markdown(f"**{who}**")
        st.text(h["summary_redacted"])
        with st.expander("Full conversation", icon=":material/forum:"):
            convo = call(client.get, f"/chat/handoffs/{h['id']}/conversation")
            for m in (convo or {}).get("messages", []):
                speaker = "Patient" if m["role"] == "user" else "Assistant"
                st.markdown(f"**{speaker}:** {md_lines(m['text'])}")
        if h["status"] == "open":
            if st.button("Claim", key=f"claim_{h['id']}", icon=":material/assignment_ind:"):
                if call(client.post, f"/chat/handoffs/{h['id']}/claim", {"version": h["version"]}):
                    st.rerun()
        if h["status"] != "closed":
            with st.form(f"close_{h['id']}"):
                note = st.text_input("What was done?", max_chars=500, key=f"hnote_{h['id']}")
                if st.form_submit_button("Close", icon=":material/task_alt:"):
                    if not note.strip():
                        st.warning("Add a short note first.")
                    elif call(client.post, f"/chat/handoffs/{h['id']}/close", {"note": note}):
                        st.rerun()
        else:
            st.caption(f"Closed: {h['close_note']}")
