import streamlit as st

from netracare.ui.components import call, show_triage
from netracare.ui.state import get_client

client = get_client()
st.caption(
    "Describe what you are noticing. We will point you to the right next step. This is not a "
    "diagnosis and not medical advice."
)
with st.form("symptom_form"):
    text = st.text_area(
        "What is happening with your eyes?",
        max_chars=2000,
        placeholder="For example: a few new floaters since yesterday",
        key="symptom_text",
    )
    submitted = st.form_submit_button("Check", type="primary", icon=":material/health_and_safety:")

if submitted:
    if not text.strip():
        st.warning("Please describe what is going on first.")
    else:
        result = call(client.post, "/triage", {"text": text})
        if result:
            show_triage(result)
            if result["level"] == "routine":
                st.page_link(
                    "app_pages/patient_book.py",
                    label="Book a visit",
                    icon=":material/event_available:",
                )
            elif result["level"] == "non_clinical":
                st.page_link(
                    "app_pages/patient_book.py", label="Bookings", icon=":material/event_available:"
                )
            st.caption(result["disclaimer"])
