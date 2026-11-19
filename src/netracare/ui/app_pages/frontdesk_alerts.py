from typing import Any

import streamlit as st

from netracare.ui.components import call, fmt_local
from netracare.ui.state import get_client

client = get_client()
view = st.segmented_control(
    "Show", ["Open", "Acknowledged", "All"], default="Open", key="alerts_view", required=True
)
status = {"Open": "open", "Acknowledged": "acknowledged", "All": None}[view]
data = call(client.get, "/triage/alerts", status=status) or {
    "items": [],
    "open_emergencies": 0,
    "open_total": 0,
}
m = st.container(horizontal=True)
with m:
    st.metric("Open emergencies", data["open_emergencies"], border=True)
    st.metric("Open alerts", data["open_total"], border=True)
if data["open_emergencies"]:
    st.error("Open emergency alerts: contact these patients now.", icon=":material/emergency:")

items: list[dict[str, Any]] = data["items"]
if not items:
    st.info("No alerts here.", icon=":material/check_circle:")

for a in items:
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        with top:
            if a["level"] == "emergency":
                st.badge("Emergency", icon=":material/emergency:", color="red")
            else:
                st.badge("Urgent", icon=":material/priority_high:", color="orange")
            st.badge(a["status"].capitalize(), color="gray")
            st.caption(f"{fmt_local(a['created_at'])} · codes {', '.join(a['codes']) or '-'}")
        who = a["patient_name"] or "Unknown visitor (not signed in)"
        st.markdown(f"**{who}** · “{a['excerpt_redacted']}”")
        if a["status"] == "open":
            if st.button("Acknowledge", key=f"ack_{a['id']}", icon=":material/done:"):
                if call(client.post, f"/triage/alerts/{a['id']}/ack"):
                    st.rerun()
        if a["status"] != "resolved":
            with st.form(f"resolve_{a['id']}"):
                note = st.text_input("Resolution note", max_chars=500, key=f"note_{a['id']}")
                if st.form_submit_button("Resolve", icon=":material/task_alt:"):
                    if not note.strip():
                        st.warning("Add a short note first.")
                    elif call(client.post, f"/triage/alerts/{a['id']}/resolve", {"note": note}):
                        st.rerun()
        else:
            st.caption(f"Resolved: {a['resolution_note']}")
