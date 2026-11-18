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
