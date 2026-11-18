from datetime import date, timedelta

import streamlit as st

from netracare.ui.components import TYPE_LABELS, call, fmt_local
from netracare.ui.state import get_client

client = get_client()
picked = st.date_input(
    "Days", value=(date.today(), date.today() + timedelta(days=6)), key="sched_range"
)
if not isinstance(picked, tuple) or len(picked) != 2:
    st.stop()
days = (
    call(
        client.get,
        "/doctors/me/schedule",
        from_date=picked[0].isoformat(),
        to_date=picked[1].isoformat(),
    )
    or []
)
if not days:
    st.info("No clinic sessions in this range.")

for d in days:
    booked = [e for e in d["entries"] if e["appointment_id"]]
    with st.expander(
        f"{d['date']} · {len(booked)} booked · {d['open_slots']} free", expanded=bool(booked)
    ):
        rows = [
            {
                "Time": fmt_local(e["start_local"])[-5:],
                "Patient": e["patient_name"] or "",
                "Visit": TYPE_LABELS.get(e["type"] or "", ""),
                "Status": (
                    e["status"] or ("free" if e["slot_status"] == "open" else "blocked")
                ).replace("_", " "),
                "Reason": e["reason"] or "",
            }
            for e in d["entries"]
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
        for e in booked:
            if e["status"] == "checked_in" and st.button(
                f"Mark {e['patient_name']} complete",
                key=f"complete_{e['appointment_id']}",
                icon=":material/task_alt:",
            ):
                if call(client.post, f"/appointments/{e['appointment_id']}/complete"):
                    st.rerun()
