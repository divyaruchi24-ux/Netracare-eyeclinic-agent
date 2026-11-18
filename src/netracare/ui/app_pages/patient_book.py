import streamlit as st

from netracare.ui.components import booking_widget, call
from netracare.ui.state import get_client

client = get_client()
me = client.me or {}
patients = [
    p for p in (call(client.get, f"/patients/{pid}") for pid in me.get("patient_ids", [])) if p
]
if not patients:
    st.info("No patient record is linked to this account yet. Please contact the front desk.")
    st.stop()

patient = st.selectbox(
    "Booking for",
    patients,
    format_func=lambda p: f"{p['full_name']}" + (" (child)" if p["is_minor"] else ""),
    key="book_patient",
)
if patient["is_minor"]:
    st.caption("Bookings for children are confirmed by the front desk.")
booking_widget(client, patient["id"], staff=False, key="patient_book")
