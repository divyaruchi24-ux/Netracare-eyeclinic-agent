from datetime import date
from typing import Any

import streamlit as st

from netracare.ui.components import appointment_summary, call
from netracare.ui.state import get_client

client = get_client()
doctors = call(client.get, "/doctors") or []
doctor = st.selectbox(
    "Doctor", doctors, format_func=lambda d: f"{d['full_name']} · {d['specialty']['name']}"
)
mode = st.segmented_control(
    "Patient", ["Existing", "New"], default="Existing", key="walkin_mode", required=True
)

body: dict[str, Any] | None = None
if mode == "Existing":
    q = st.text_input("Search by name, email or phone", key="walkin_q")
    found = (call(client.get, "/patients", q=q, limit=10) or {"items": []})["items"] if q else []
    pick = st.selectbox(
        "Patient", found, format_func=lambda p: f"{p['full_name']} ({p['age']})", key="walkin_pick"
    )
    if pick:
        body = {"patient_id": pick["id"]}
else:
    name = st.text_input("Full name", key="walkin_name")
    dob = st.date_input(
        "Date of birth", value=date(1990, 1, 1), min_value=date(1900, 1, 1), key="walkin_dob"
    )
    phone = st.text_input("Mobile (optional)", key="walkin_phone")
    if name:
        body = {"new_patient": {"full_name": name, "dob": dob.isoformat(), "phone": phone or None}}

reason = st.text_input("Reason (optional)", key="walkin_reason", max_chars=500)
clicked = st.button(
    "Add to next free slot today",
    type="primary",
    disabled=not (doctor and body),
    icon=":material/add:",
)
if clicked and doctor and body:
    res = call(
        client.post,
        "/front-desk/walk-ins",
        {"doctor_id": doctor["id"], "reason": reason or None, **body},
    )
    if res:
        st.success(
            f"Booked: {appointment_summary(res['appointment'])}", icon=":material/check_circle:"
        )
