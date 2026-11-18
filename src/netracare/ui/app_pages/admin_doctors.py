import streamlit as st

from netracare.ui.components import call
from netracare.ui.state import get_client

client = get_client()
doctors = call(client.get, "/doctors", active="false") or []
st.dataframe(
    [
        {
            "Code": d["code"],
            "Name": d["full_name"],
            "Specialty": d["specialty"]["name"],
            "Active": d["is_active"],
        }
        for d in doctors
    ],
    hide_index=True,
    width="stretch",
)

specialties = call(client.get, "/specialties") or []
with st.expander("Add a doctor", icon=":material/person_add:"), st.form("add_doctor"):
    code = st.text_input("Code (e.g. D6)")
    name = st.text_input("Full name")
    spec = st.selectbox("Specialty", [s["code"] for s in specialties])
    if st.form_submit_button("Add doctor", type="primary"):
        if call(client.post, "/doctors", {"code": code, "full_name": name, "specialty_code": spec}):
            st.success("Doctor added. Add their weekly blocks on **Schedules**.")

with st.expander("Activate / deactivate", icon=":material/toggle_on:"):
    doc = st.selectbox("Doctor", doctors, format_func=lambda d: d["full_name"], key="toggle_doc")
    force = st.checkbox("Deactivate even with upcoming appointments", key="toggle_force")
    if doc and st.button("Deactivate" if doc["is_active"] else "Activate", key="toggle_btn"):
        res = call(
            client.patch,
            f"/doctors/{doc['id']}",
            {"is_active": not doc["is_active"]},
            force=str(force).lower(),
        )
        if res:
            if res["affected_appointment_ids"]:
                n = len(res["affected_appointment_ids"])
                st.warning(f"{n} upcoming appointments need rescheduling.")
            st.rerun()
