import streamlit as st

from netracare.ui.components import call
from netracare.ui.state import get_client

client = get_client()
filters = st.container(horizontal=True, vertical_alignment="bottom")
with filters:
    action = st.text_input("Action starts with", placeholder="appointment.", key="audit_action")
    entity = st.text_input("Entity ID", key="audit_entity")
    request_id = st.text_input("Request ID", key="audit_rid")
page = call(
    client.get, "/audit-logs", action=action or None, entity_id=entity or None, limit=500
) or {"items": []}
rows = [
    {
        "When (UTC)": r["at_utc"][:19].replace("T", " "),
        "Action": r["action"],
        "Entity": f"{r['entity_type']}:{r['entity_id'] or ''}",
        "Actor": r["actor_user_id"] or r["actor_type"],
        "Reason": r["reason"] or "",
        "Request ID": r["request_id"] or "",
    }
    for r in page["items"]
    if not request_id or (r["request_id"] or "").startswith(request_id)
]
st.dataframe(rows, hide_index=True, width="stretch")
st.caption(
    f"Showing {len(rows)} of {page.get('total', len(rows))} entries. The audit log is append-only."
)
