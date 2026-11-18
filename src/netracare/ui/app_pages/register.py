from datetime import date

import streamlit as st

from netracare.ui.api_client import ApiError
from netracare.ui.components import error_box
from netracare.ui.state import get_client

client = get_client()
st.caption(
    "Patients aged 18 or over can register here. Children and dependants are added by the "
    "front desk with a parent or guardian."
)

with st.form("register"):
    full_name = st.text_input("Full name", max_chars=128)
    email = st.text_input("Email")
    phone = st.text_input("Mobile number (optional)", placeholder="+91 98765 43210")
    dob = st.date_input(
        "Date of birth", value=date(1990, 1, 1), min_value=date(1900, 1, 1), max_value=date.today()
    )
    password = st.text_input("Password (at least 10 characters)", type="password")
    consent = st.checkbox("I agree that NetraCare may store my details to manage my appointments.")
    submitted = st.form_submit_button(
        "Create account", type="primary", icon=":material/person_add:"
    )

if submitted:
    if not consent:
        st.warning("Please agree to the data-use statement to continue.")
    else:
        try:
            client.register(
                email=email,
                password=password,
                full_name=full_name,
                dob=dob.isoformat(),
                phone=phone or None,
            )
        except ApiError as err:
            error_box(err)
        else:
            st.rerun()
