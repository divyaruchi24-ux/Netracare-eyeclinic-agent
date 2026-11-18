"""NetraCare clinic website (Streamlit). Run: `streamlit run src/netracare/ui/app.py`.

The UI is a thin client of the REST API: every read and write goes through `ApiClient`.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from netracare.ui.components import DISCLAIMER
from netracare.ui.navigation import pages_for
from netracare.ui.state import get_client

PAGES = Path(__file__).parent / "app_pages"

st.set_page_config(
    page_title="NetraCare Eye Clinic", page_icon=":material/visibility:", layout="wide"
)

client = get_client()
role = client.role if client.is_authenticated else None
nav = st.navigation(
    {
        section: [
            st.Page(PAGES / p.file, title=p.title, icon=p.icon, help=p.help, default=p.default)
            for p in specs
        ]
        for section, specs in pages_for(role).items()
    },
    position="sidebar",
)

with st.sidebar:
    st.markdown("### :material/visibility: NetraCare")
    if client.is_authenticated and client.me:
        who, what = client.me["display_name"], client.me["role"].replace("_", " ")
        st.caption(f"Signed in as **{who}** · {what}")
        if st.button("Sign out", icon=":material/logout:", key="logout"):
            client.logout()
            st.rerun()

st.title(nav.title)
nav.run()
st.divider()
st.caption(DISCLAIMER)
