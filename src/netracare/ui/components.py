"""Reusable UI pieces. Every API call goes through `ApiClient`; errors show the request ID."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import streamlit as st

from netracare.ui.api_client import ApiClient, ApiError
from netracare.ui.state import form_key, reset_form_key

DISCLAIMER = (
    "NetraCare's online services handle bookings and clinic information only and do not give "
    "medical advice. **If you have sudden vision loss, a chemical splash or an eye injury, call "
    "the clinic emergency line +91 80 0000 9999 or 112 now.**"
)

_STATUS: dict[str, tuple[str, str, str]] = {
    "confirmed": ("Confirmed", ":material/check_circle:", "green"),
    "pending_approval": ("Awaiting front desk", ":material/hourglass_top:", "orange"),
    "requested": ("Requested", ":material/hourglass_top:", "orange"),
    "checked_in": ("Checked in", ":material/how_to_reg:", "blue"),
    "completed": ("Completed", ":material/task_alt:", "gray"),
    "cancelled": ("Cancelled", ":material/cancel:", "gray"),
    "rejected": ("Not approved", ":material/block:", "red"),
    "no_show": ("No-show", ":material/person_off:", "red"),
    "rescheduled": ("Rescheduled", ":material/event_repeat:", "gray"),
}
TYPE_LABELS = {
    "routine": "Routine check (15 min)",
    "follow_up": "Follow-up (15 min)",
    "new_patient": "New patient (30 min)",
    "surgery_consult": "Surgery consultation (30 min)",
}
REASON_LABELS = {
    "within_24h_change": "change within 24h",
    "new_patient_surgery_consult": "new-patient surgery consult",
    "guardian_booking_minor": "guardian booking for a minor",
    "post_red_flag": "follow-up after a red-flag symptom",
    "no_show_flagged": "patient flagged for no-shows",
}


def status_badge(status: str) -> None:
    """Status as text + icon + colour (never colour alone)."""
    label, icon, color = _STATUS.get(status, (status, ":material/help:", "gray"))
    st.badge(label, icon=icon, color=color)  # type: ignore[arg-type]


def fmt_local(iso: str) -> str:
    """'2026-11-13T10:00:00+05:30' -> 'Fri 13 Nov 2026, 10:00'."""
    return datetime.fromisoformat(iso).strftime("%a %d %b %Y, %H:%M")


def reasons_text(reasons: list[str]) -> str:
    return ", ".join(REASON_LABELS.get(r, r) for r in reasons)


def error_box(err: ApiError) -> None:
    ref = f" (reference: `{err.request_id}`)" if err.request_id else ""
    st.error(f"{err.message}{ref}", title="Something went wrong", icon=":material/error:")


def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run an API call; show a friendly error box (with request ID) instead of raising."""
    try:
        return fn(*args, **kwargs)
    except ApiError as err:
        error_box(err)
        return None


def appointment_summary(a: dict[str, Any]) -> str:
    return (
        f"**{fmt_local(a['start_local'])}** · {a['doctor']['name']} "
        f"({a['doctor']['specialty']}) · {TYPE_LABELS.get(a['type'], a['type'])}"
    )


def booking_widget(client: ApiClient, patient_id: str, *, staff: bool, key: str) -> None:
    """Pick specialty/doctor/type/dates -> choose a time -> read back -> confirm."""
    specialties = call(client.get, "/specialties") or []
    spec_codes = [s["code"] for s in specialties]
    spec_names = {s["code"]: s["name"] for s in specialties}
    c1, c2 = st.columns(2)
    with c1:
        spec = st.selectbox(
            "Specialty", spec_codes, format_func=lambda c: spec_names.get(c, c), key=f"{key}_spec"
        )
    doctors = call(client.get, "/doctors", specialty=spec) or []
    with c2:
        doctor = st.selectbox(
            "Doctor",
            doctors,
            format_func=lambda d: d["full_name"],
            key=f"{key}_doctor",
        )
    appt_type = st.segmented_control(
        "Visit type",
        list(TYPE_LABELS),
        default="routine",
        format_func=lambda t: TYPE_LABELS[t],
        key=f"{key}_type",
        required=True,
    )
    today = date.today()
    picked = st.date_input(
        "Dates to search",
        value=(today, today + timedelta(days=6)),
        min_value=today,
        max_value=today + timedelta(days=56),
        key=f"{key}_dates",
    )
    if not doctor or not appt_type or not isinstance(picked, tuple) or len(picked) != 2:
        st.info("Choose a doctor, a visit type and a date range.")
        return
    avail = call(
        client.get,
        "/availability",
        doctor_id=doctor["id"],
        type=appt_type,
        from_date=picked[0].isoformat(),
        to_date=picked[1].isoformat(),
    )
    options = [o for d in (avail or {}).get("days", []) for o in d["options"]]
    if not options:
        st.warning("No free times in that range. Try other dates or another doctor.")
        return
    choice = st.selectbox(
        f"Available times ({len(options)})",
        options,
        format_func=lambda o: fmt_local(o["start_local"]),
        key=f"{key}_slot",
    )
    with st.form(f"{key}_confirm"):
        st.markdown(
            f"**Please check:** {TYPE_LABELS[appt_type]} with **{choice['doctor_name']}** "
            f"on **{fmt_local(choice['start_local'])}**."
        )
        reason = st.text_input(
            "Reason for visit (optional, no medical details needed)", max_chars=500
        )
        override = override_reason = None
        if staff:
            override = st.checkbox("Override approval rules (staff only)")
            override_reason = st.text_input("Override reason") or None
        submitted = st.form_submit_button(
            "Confirm booking", type="primary", icon=":material/event_available:"
        )
    if submitted:
        body: dict[str, Any] = {
            "patient_id": patient_id,
            "doctor_id": choice["doctor_id"],
            "slot_ids": choice["slot_ids"],
            "type": appt_type,
            "reason": reason or None,
        }
        if override:
            body |= {"override": True, "override_reason": override_reason}
        res = call(client.post, "/appointments", body, idempotency_key=form_key(key))
        if res:
            reset_form_key(key)
            appt = res["appointment"]
            if res["requires_approval"]:
                st.warning(
                    f"Request received for {fmt_local(appt['start_local'])}. The front desk will "
                    f"confirm it ({reasons_text(res['approval_reasons'])}).",
                    title="Awaiting confirmation",
                    icon=":material/hourglass_top:",
                )
            else:
                st.success(
                    f"Booked: {appointment_summary(appt)}",
                    title="Confirmed",
                    icon=":material/check_circle:",
                )
