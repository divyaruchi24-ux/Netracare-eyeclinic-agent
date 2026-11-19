from typing import Any

import streamlit as st

from netracare.ui.api_client import ApiError
from netracare.ui.components import (
    appointment_summary,
    call,
    fmt_local,
    reasons_text,
    status_badge,
)
from netracare.ui.state import get_client

client = get_client()
page = call(client.get, "/appointments", limit=200) or {"items": []}
items: list[dict[str, Any]] = page["items"]
ACTIVE = {"confirmed", "pending_approval", "checked_in"}
upcoming = [a for a in items if a["status"] in ACTIVE]
past = [a for a in items if a["status"] not in ACTIVE]

view = st.segmented_control(
    "Show", ["Upcoming", "History"], default="Upcoming", key="appt_view", required=True
)
shown = upcoming if view == "Upcoming" else list(reversed(past))
if not shown:
    st.info("Nothing here yet. Use **Book a visit** to find a time.", icon=":material/event_busy:")

for a in shown:
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        with top:
            st.markdown(appointment_summary(a))
            status_badge(a["status"])
        st.caption(f"Patient: {a['patient']['name']}")
        if a["status"] in ("completed", "checked_in"):
            try:
                summary = client.get(f"/appointments/{a['id']}/visit-summary")
            except ApiError:
                summary = None  # none approved yet
            if summary and summary.get("final"):
                final = summary["final"]
                with st.expander("Visit summary", icon=":material/description:"):
                    st.caption(summary["disclaimer"])
                    if final.get("what_we_checked"):
                        st.markdown("**What we checked:** " + "; ".join(final["what_we_checked"]))
                    st.markdown(f"**What we found:** {final['what_we_found']}")
                    for step in final.get("next_steps", []):
                        st.markdown(f"- {step}")
                    for med in final.get("medicines_mentioned", []):
                        st.markdown(f"- **{med['name']}**: {med['instructions_verbatim']}")
                    if final.get("follow_up"):
                        st.markdown(f"**Follow-up:** {final['follow_up']}")
        approval = a.get("approval")
        if approval and approval["status"] == "pending":
            what = {"cancel": "cancellation", "reschedule": "move", "new_booking": "booking"}[
                approval["kind"]
            ]
            why = reasons_text(approval["reasons"])
            st.warning(
                f"Your {what} request is waiting for the front desk ({why}).",
                icon=":material/hourglass_top:",
            )
            if st.button("Withdraw request", key=f"withdraw_{a['id']}", icon=":material/undo:"):
                if call(client.post, f"/approvals/{approval['id']}/withdraw"):
                    st.rerun()
        if a["status"] == "confirmed" and not (approval and approval["status"] == "pending"):
            actions = st.container(horizontal=True)
            with actions:
                if st.button("Cancel", key=f"cancel_{a['id']}", icon=":material/event_busy:"):
                    res = call(client.post, f"/appointments/{a['id']}/cancel", {})
                    if res:
                        st.rerun()
                with st.popover("Reschedule", icon=":material/event_repeat:"):
                    avail = call(
                        client.get,
                        "/availability",
                        doctor_id=a["doctor"]["id"],
                        type=a["type"],
                    )
                    opts = [o for d in (avail or {}).get("days", []) for o in d["options"]]
                    if opts:
                        new = st.selectbox(
                            "New time",
                            opts,
                            format_func=lambda o: fmt_local(o["start_local"]),
                            key=f"resched_pick_{a['id']}",
                        )
                        if st.button("Move appointment", key=f"resched_{a['id']}", type="primary"):
                            if call(
                                client.post,
                                f"/appointments/{a['id']}/reschedule",
                                {"slot_ids": new["slot_ids"]},
                            ):
                                st.rerun()
                    else:
                        st.caption("No free times with this doctor in the next week.")
        st.caption("Changes less than 24 hours before a visit are reviewed by the front desk.")
