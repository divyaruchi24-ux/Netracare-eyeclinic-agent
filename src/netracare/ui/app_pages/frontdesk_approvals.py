from datetime import date, timedelta

import streamlit as st

from netracare.ui.components import appointment_summary, call, fmt_local, reasons_text, status_badge
from netracare.ui.state import get_client

client = get_client()
items = call(client.get, "/approvals") or []
st.caption("Oldest and overdue requests first. Approving or rejecting notifies the patient.")
if not items:
    st.success("No requests waiting.", icon=":material/done_all:")

KIND = {"new_booking": "New booking", "reschedule": "Reschedule", "cancel": "Cancellation"}
for r in items:
    with st.container(border=True):
        head = st.container(horizontal=True, vertical_alignment="center")
        with head:
            st.markdown(f"**{KIND[r['kind']]}** for **{r['appointment']['patient']['name']}**")
            if r["is_overdue"]:
                st.badge("Overdue", icon=":material/alarm:", color="red")
            if r.get("source") == "chat":
                st.badge("via chat", icon=":material/chat:", color="blue")
        st.markdown(f"Why: {reasons_text(r['reasons'])}")
        st.markdown(f"Current: {appointment_summary(r['appointment'])}")
        status_badge(r["appointment"]["status"])
        if r["proposed"]:
            st.markdown(f"Proposed: **{fmt_local(r['proposed']['start_local'])}**")
        if r.get("request_note"):
            st.caption(f"Patient note: {r['request_note']}")
        note = st.text_input("Note (optional)", key=f"note_{r['id']}", max_chars=500)
        actions = st.container(horizontal=True)
        with actions:
            if st.button(
                "Approve", key=f"approve_{r['id']}", type="primary", icon=":material/check:"
            ):
                if call(client.post, f"/approvals/{r['id']}/approve", {"note": note or None}):
                    st.rerun()
            if st.button("Reject", key=f"reject_{r['id']}", icon=":material/close:"):
                if call(client.post, f"/approvals/{r['id']}/reject", {"note": note or None}):
                    st.rerun()
        if r["kind"] != "cancel":
            pending = r["proposed"] or r["appointment"]
            with st.expander("Edit: approve at a different time", icon=":material/edit_calendar:"):
                today = date.today()
                avail = call(
                    client.get,
                    "/availability",
                    doctor_id=pending["doctor"]["id"],
                    from_date=today.isoformat(),
                    to_date=(today + timedelta(days=13)).isoformat(),
                    type=pending["type"],
                ) or {"days": []}
                choices = [o for d in avail["days"] for o in d["options"]]
                if not choices:
                    st.caption("No other free times for this doctor in the next two weeks.")
                else:
                    pick = st.selectbox(
                        "New time",
                        choices,
                        format_func=lambda o: f"{fmt_local(o['start_local'])} · {o['doctor_name']}",
                        key=f"edit_pick_{r['id']}",
                    )
                    if st.button(
                        "Approve with this time",
                        key=f"edit_go_{r['id']}",
                        icon=":material/event_available:",
                    ):
                        body = {"slot_ids": pick["slot_ids"], "note": note or None}
                        if call(client.post, f"/approvals/{r['id']}/approve-with-changes", body):
                            st.rerun()
