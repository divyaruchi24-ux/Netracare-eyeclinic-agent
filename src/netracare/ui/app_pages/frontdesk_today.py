from datetime import date

import streamlit as st

from netracare.ui.components import TYPE_LABELS, call, fmt_local, status_badge
from netracare.ui.state import get_client

client = get_client()
day = st.date_input("Day", value=date.today(), key="queue_day")
q = call(client.get, "/front-desk/queue", date=day.isoformat())
if not q:
    st.stop()

alerts = call(client.get, "/triage/alerts", status="open", limit=1)
if alerts and alerts["open_emergencies"]:
    st.error(
        f"{alerts['open_emergencies']} open emergency alert(s). See **Alerts** now.",
        icon=":material/emergency:",
    )

counts = q["status_counts"]
m = st.container(horizontal=True)
with m:
    st.metric("Booked", sum(counts.values()), border=True)
    st.metric("Checked in", counts.get("checked_in", 0), border=True)
    st.metric("Completed", counts.get("completed", 0), border=True)
    st.metric("Pending approvals", q["pending_approvals"], border=True)
    st.metric("Overdue approvals", q["overdue_approvals"], border=True)
if q["overdue_approvals"]:
    st.warning(
        "Some approval requests have waited over 4 hours. See **Approvals**.",
        icon=":material/alarm:",
    )

for d in q["doctors"]:
    with st.container(border=True):
        st.subheader(f"{d['doctor_name']} · {d['specialty']}")
        if not d["appointments"]:
            st.caption("No appointments.")
        for a in d["appointments"]:
            row = st.container(horizontal=True, vertical_alignment="center")
            with row:
                st.markdown(
                    f"**{fmt_local(a['start_local'])[-5:]}** · {a['patient']['name']} · "
                    f"{TYPE_LABELS.get(a['type'], a['type'])}"
                )
                status_badge(a["status"])
                if a["status"] == "confirmed":
                    if st.button("Check in", key=f"ci_{a['id']}", icon=":material/how_to_reg:"):
                        if call(client.post, f"/appointments/{a['id']}/check-in"):
                            st.rerun()
                    if st.button("No-show", key=f"ns_{a['id']}", icon=":material/person_off:"):
                        if call(client.post, f"/appointments/{a['id']}/no-show"):
                            st.rerun()
                if a["status"] == "checked_in":
                    if st.button("Complete", key=f"done_{a['id']}", icon=":material/task_alt:"):
                        if call(client.post, f"/appointments/{a['id']}/complete"):
                            st.rerun()
