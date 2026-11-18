import streamlit as st

from netracare.ui.api_client import ApiError
from netracare.ui.components import error_box
from netracare.ui.state import get_client

client = get_client()
st.caption("Book and manage eye-care appointments at NetraCare (a fictional clinic).")

with st.form("login"):
    email = st.text_input("Email", key="login_email")
    password = st.text_input("Password", type="password", key="login_password")
    submitted = st.form_submit_button("Sign in", type="primary", icon=":material/login:")

if submitted:
    try:
        client.login(email, password)
    except ApiError as err:
        error_box(err)
    else:
        st.rerun()

st.info(
    "Demo accounts use the seed password from your `.env`: `admin@netracare.example`, "
    "`frontdesk1@netracare.example`, `kavya.nair@netracare.example` (doctor).",
    icon=":material/info:",
)
