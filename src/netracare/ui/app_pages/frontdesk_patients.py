import streamlit as st

from netracare.ui.components import booking_widget, call
from netracare.ui.state import get_client

client = get_client()
q = st.text_input("Search patients by name, email or phone", key="pt_q")
page = call(client.get, "/patients", q=q or None, limit=25) or {"items": [], "total": 0}
rows = [
    {
        "Name": p["full_name"],
        "Age": p["age"],
        "Phone": p["phone_masked"],
        "Email": p["email"],
        "No-shows": p["no_show_count"],
        "Flagged": p["flagged"],
        "Online account": p["has_account"],
    }
    for p in page["items"]
]
st.dataframe(rows, hide_index=True, width="stretch")
st.caption(f"{page['total']} matching patients")

patient = st.selectbox(
    "Open patient", page["items"], format_func=lambda p: p["full_name"], key="pt_open"
)
if patient:
    detail = call(client.get, f"/patients/{patient['id']}") or {}
    tab_book, tab_guardian = st.tabs(["Book on behalf", "Guardians"])
    with tab_book:
        booking_widget(client, patient["id"], staff=True, key=f"fd_book_{patient['id']}")
    with tab_guardian:
        for g in detail.get("guardians", []):
            st.markdown(f"- {g['email']} ({g['relationship']})")
        with st.form(f"guardian_{patient['id']}"):
            email = st.text_input("Guardian's account email")
            rel = st.selectbox(
                "Relationship", ["mother", "father", "guardian", "adult_child", "spouse", "other"]
            )
            if st.form_submit_button("Link guardian", icon=":material/link:"):
                if call(
                    client.post,
                    f"/patients/{patient['id']}/guardians",
                    {"guardian_email": email, "relationship": rel},
                ):
                    st.success("Guardian linked. Consent was recorded with today's date.")
