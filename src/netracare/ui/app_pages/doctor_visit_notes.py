from typing import Any

import streamlit as st

from netracare.ui.api_client import ApiError
from netracare.ui.components import appointment_summary, call, error_box
from netracare.ui.state import get_client

client = get_client()
visits: list[dict[str, Any]] = []
for status in ("checked_in", "completed"):
    page = call(client.get, "/appointments", status=status, limit=100) or {"items": []}
    visits += page["items"]
visits.sort(key=lambda a: a["start_local"], reverse=True)
if not visits:
    st.info("No checked-in or completed visits yet.", icon=":material/event_busy:")
    st.stop()

visit = st.selectbox(
    "Visit",
    visits,
    format_func=lambda a: f"{a['patient']['name']} · {appointment_summary(a).replace('**', '')}",
    key="vn_visit",
)
aid = visit["id"]
url = f"/appointments/{aid}/visit-summary"

summary: dict[str, Any] | None
try:
    summary = client.get(url)
except ApiError as err:
    summary = None
    if err.status != 404:
        error_box(err)

if summary and summary["status"] == "approved":
    st.success(summary["disclaimer"] or "Approved.", icon=":material/verified:")
    final = summary["final"] or {}
    st.markdown(f"**What we found:** {final.get('what_we_found', '')}")
    st.stop()

with st.form(f"notes_{aid}"):
    notes = st.text_area(
        "Your visit notes (only you can see these)",
        value=(summary or {}).get("notes") or "",
        height=180,
        max_chars=8000,
        key=f"vn_notes_{aid}",
    )
    gen = st.form_submit_button("Generate draft", type="primary", icon=":material/auto_awesome:")
if gen:
    try:
        client.post(url, {"notes": notes})
        st.rerun()
    except ApiError as err:
        if err.code != "llm_unavailable":
            error_box(err)
        else:  # notes were saved as an empty draft: show the manual editor
            st.session_state["vn_flash"] = err.message
            st.rerun()

flash = st.session_state.pop("vn_flash", None)
if flash:
    st.warning(flash, icon=":material/edit:")

if not summary:
    st.stop()

if summary.get("disclaimer"):
    st.warning(summary["disclaimer"], icon=":material/smart_toy:")
if summary["status"] == "discarded":
    st.caption("This draft was discarded. Generate a new one or edit below.")
draft: dict[str, Any] = summary["draft"] or {}
ver = f"{aid}_{summary['version']}"  # new widget state whenever the summary changes


def _lines(value: list[str]) -> str:
    return "\n".join(value or [])


def _split(text: str) -> list[str]:
    return [x.strip() for x in text.splitlines() if x.strip()]


with st.form(f"edit_{aid}"):
    found = st.text_area(
        "What we found", value=draft.get("what_we_found", ""), key=f"vn_found_{ver}"
    )
    checked = st.text_area(
        "What we checked (one per line)",
        value=_lines(draft.get("what_we_checked", [])),
        key=f"vn_checked_{ver}",
    )
    steps = st.text_area(
        "Next steps (one per line)",
        value=_lines(draft.get("next_steps", [])),
        key=f"vn_steps_{ver}",
    )
    meds = st.text_area(
        "Medicines, exactly as in your notes (name | instructions, one per line)",
        value="\n".join(
            f"{m['name']} | {m['instructions_verbatim']}"
            for m in draft.get("medicines_mentioned", [])
        ),
        key=f"vn_meds_{ver}",
    )
    follow = st.text_input("Follow-up", value=draft.get("follow_up") or "", key=f"vn_follow_{ver}")
    unclear = _lines(draft.get("unclear_in_notes", []))
    if unclear:
        st.caption(f"Unclear in notes: {unclear}")
    acks: list[str] = []
    flags: list[str] = summary["needs_attention"]
    if flags:
        st.markdown(":red[**Needs your attention before approval:**]")
    for i, flag in enumerate(flags):
        if st.checkbox(
            f":red[{flag}]",
            value=flag in summary["acknowledged_flags"],
            key=f"vn_ack_{ver}_{i}",
            help="Tick to confirm this item is correct as written.",
        ):
            acks.append(flag)
    save = st.form_submit_button("Save changes", icon=":material/save:")
if save:
    medicines = []
    for line in _split(meds):
        name, _, instructions = line.partition("|")
        medicines.append({"name": name.strip(), "instructions_verbatim": instructions.strip()})
    body = {
        "draft": {
            "what_we_checked": _split(checked),
            "what_we_found": found,
            "next_steps": _split(steps),
            "medicines_mentioned": medicines,
            "follow_up": follow or None,
            "unclear_in_notes": draft.get("unclear_in_notes", []),
        },
        "acknowledged_flags": acks,
    }
    if call(client.patch, url, body):
        st.rerun()

actions = st.container(horizontal=True)
with actions:
    if st.button(
        "Approve and share", type="primary", icon=":material/verified:", key=f"vn_approve_{aid}"
    ):
        if call(client.post, f"{url}/approve"):
            st.rerun()
    if st.button("Discard draft", icon=":material/delete:", key=f"vn_discard_{aid}"):
        if call(client.post, f"{url}/discard"):
            st.rerun()
if summary["unresolved_flags"]:
    st.caption(f"{len(summary['unresolved_flags'])} flagged item(s) still need attention.")
