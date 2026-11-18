import streamlit as st

from netracare.ui.components import call
from netracare.ui.state import get_client

client = get_client()
st.caption("Simulated outbox: nothing is actually sent in this demo.")
page = call(client.get, "/notifications", limit=100) or {"items": [], "total": 0}
st.dataframe(
    [
        {
            "When (UTC)": n["created_at"][:16].replace("T", " "),
            "Channel": n["channel"].upper(),
            "Template": n["template"],
            "Message": n["payload_json"].get("body", ""),
        }
        for n in page["items"]
    ],
    hide_index=True,
    width="stretch",
)
