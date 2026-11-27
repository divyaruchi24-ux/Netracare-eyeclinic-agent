"""Ask NetraCare: the patient chat (signed-in patients and anonymous visitors)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from netracare.ui.components import PREFILL_KEY, call, md_lines
from netracare.ui.state import get_client

client = get_client()
SESSION_KEY = "chat_session_id"
OWNER_KEY = "chat_owner"
owner = client.me["user_id"] if client.is_authenticated and client.me else "anonymous"

# A new conversation whenever the signed-in identity changes (sign in / sign out).
if st.session_state.get(OWNER_KEY) != owner:
    st.session_state.pop(SESSION_KEY, None)
    st.session_state[OWNER_KEY] = owner


def _new_session() -> str | None:
    created = call(client.chat_create)
    if not created:
        return None
    st.session_state[SESSION_KEY] = created["id"]
    return str(created["id"])


def _sources(m: dict[str, Any]) -> None:
    if not m.get("citations"):
        return
    with st.expander(f"Sources ({len(m['citations'])})", icon=":material/menu_book:"):
        for c in m["citations"]:
            label = f"**[{c['n']}] {c['title']}** › {c['heading_path']}"
            st.markdown(f"{label}  \n:gray[Effective {c['effective_date']}]")
            st.caption(c["excerpt"])


def _render_assistant(m: dict[str, Any], *, latest: bool) -> None:
    safety = m.get("safety") or {}
    level = safety.get("level")
    text = md_lines(m["text"] if "text" in m else m["reply"])
    if level == "emergency":
        st.error(text, icon=":material/emergency:")
    elif level == "urgent":
        st.warning(text, icon=":material/priority_high:")
    elif m.get("mode") == "refused":
        st.info(text, icon=":material/support_agent:")
    else:
        st.markdown(text)
    _sources(m)
    for d in m.get("disclaimers") or []:
        st.caption(d)
    agent = m.get("agent") or {}
    if agent.get("kind") == "pending":
        st.badge("Awaiting front desk", icon=":material/hourglass_top:", color="orange")
    if not latest:
        return
    _agent_controls(m, agent)
    action = m.get("suggested_action")
    if action and action.get("type") == "book":
        doctor = action.get("doctor_name")
        label = f"Book with {doctor}" if doctor else "Book an appointment"
        if client.is_authenticated and client.role == "patient":
            if st.button(label, icon=":material/event_available:", key=f"book_{m.get('id')}"):
                st.session_state[PREFILL_KEY] = action
                st.switch_page("app_pages/patient_book.py")
        else:
            st.caption(f"{label}: please sign in first.")
    if m.get("handoff_offer") and client.is_authenticated:
        if st.button(
            "Ask the front desk to call me",
            icon=":material/call:",
            key=f"callback_{m.get('id')}",
        ):
            done = call(client.chat_handoff, st.session_state[SESSION_KEY])
            if done:
                st.success(md_lines(done["reply"]), icon=":material/check:")


def _send(text: str) -> None:
    turn = call(client.chat_send, st.session_state[SESSION_KEY], text)
    if turn:
        st.session_state["chat_last_turn"] = turn
    st.rerun()


def _agent_controls(m: dict[str, Any], agent: dict[str, Any]) -> None:
    """Booking-agent buttons: proposed times, and Yes / No / Change on a read-back. Buttons send
    exactly what a typed reply would ("option:<id>", "yes"), so the agent's rules are the same."""
    options = m.get("options") or agent.get("options") or []
    readback = m.get("readback") or agent.get("readback")
    msg = m.get("id") or m.get("message_id") or "latest"
    if options and not readback:
        cols = st.container(horizontal=True)
        with cols:
            for o in options:
                if st.button(o["label"], key=f"opt_{msg}_{o['id']}", icon=":material/schedule:"):
                    _send(f"option:{o['id']}")
    if readback:
        if readback.get("needs_approval"):
            st.caption("This request will go to the front desk for approval.")
        row = st.container(horizontal=True)
        with row:
            if st.button("Yes, book it", key=f"yes_{msg}", type="primary", icon=":material/check:"):
                _send("yes")
            if st.button("No", key=f"no_{msg}", icon=":material/close:"):
                _send("no")
            if st.button("Change", key=f"change_{msg}", icon=":material/edit:"):
                _send("I'd like a different time")


session_id = st.session_state.get(SESSION_KEY) or _new_session()
if not session_id:
    st.stop()

top = st.container(horizontal=True, vertical_alignment="center")
with top:
    st.caption(
        "Ask about our doctors, timings, prices, insurance or preparing for a visit. "
        "Answers come from the clinic's own information, with sources."
    )
    if st.button("New conversation", icon=":material/add_comment:", key="chat_new"):
        _new_session()
        st.session_state.pop("chat_last_turn", None)
        st.rerun()
    if st.button("Talk to a person", icon=":material/support_agent:", key="chat_human"):
        res = call(client.chat_handoff, session_id)
        if res:
            st.session_state["chat_last_turn"] = res
            st.rerun()

data = call(client.chat_get, session_id)
if data is None:
    st.session_state.pop(SESSION_KEY, None)
    st.stop()

messages: list[dict[str, Any]] = data["messages"]
last_turn: dict[str, Any] | None = st.session_state.get("chat_last_turn")
for i, m in enumerate(messages):
    with st.chat_message("user" if m["role"] == "user" else "assistant"):
        if m["role"] == "user":
            st.markdown(m["text"])
        else:
            latest = i == len(messages) - 1
            merged = dict(m)
            if latest and last_turn and last_turn.get("message_id") == m["id"]:
                merged.update(
                    suggested_action=last_turn.get("suggested_action"),
                    handoff_offer=last_turn.get("handoff_offer"),
                    options=last_turn.get("options"),
                    readback=last_turn.get("readback"),
                )
            _render_assistant(merged, latest=latest)

if last_turn and not last_turn.get("message_id"):  # e.g. the "Talk to a person" button
    with st.chat_message("assistant"):
        st.markdown(md_lines(last_turn["reply"]))

if data["status"] == "ended":
    st.info("This conversation has ended. Start a new one to continue.", icon=":material/info:")
else:
    prompt = st.chat_input("Type your question", max_chars=2000, key="chat_input")
    if prompt is not None:
        turn = call(client.chat_send, session_id, prompt)
        if turn:
            st.session_state["chat_last_turn"] = turn
        st.rerun()

st.caption(data["footer"])
