import streamlit as st

from netracare.ui.components import call
from netracare.ui.state import get_client

client = get_client()
role = st.segmented_control(
    "Role",
    ["admin", "front_desk", "doctor", "patient"],
    key="users_role",
    format_func=lambda r: r.replace("_", " "),
)
page = call(client.get, "/users", role=role, limit=200) or {"items": [], "total": 0}
st.dataframe(
    [
        {
            "Email": u["email"],
            "Name": u["display_name"] or "",
            "Role": u["role"],
            "Active": u["is_active"],
        }
        for u in page["items"]
    ],
    hide_index=True,
    width="stretch",
)

with st.expander("Add staff account", icon=":material/person_add:"):
    doctors = call(client.get, "/doctors") or []
    with st.form("add_staff"):
        email = st.text_input("Email")
        name = st.text_input("Display name")
        new_role = st.selectbox("Role", ["front_desk", "admin", "doctor"])
        doc = st.selectbox(
            "Doctor record (doctor accounts only)",
            [None, *doctors],
            format_func=lambda d: "-" if d is None else d["full_name"],
        )
        pw = st.text_input("Temporary password (10+ chars)", type="password")
        if st.form_submit_button("Create", type="primary"):
            body = {
                "email": email,
                "display_name": name,
                "role": new_role,
                "temp_password": pw,
                "doctor_id": doc["id"] if doc else None,
            }
            if call(client.post, "/users", body):
                st.success("Account created.")

with st.expander("Activate / deactivate", icon=":material/toggle_on:"):
    user = st.selectbox("User", page["items"], format_func=lambda u: u["email"], key="toggle_user")
    if user and st.button("Deactivate" if user["is_active"] else "Activate", key="toggle_user_btn"):
        if call(client.patch, f"/users/{user['id']}", {"is_active": not user["is_active"]}):
            st.rerun()
