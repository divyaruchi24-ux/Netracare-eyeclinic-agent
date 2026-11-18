from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from netracare.ui.components import call, fmt_local
from netracare.ui.state import get_client

IST = ZoneInfo("Asia/Kolkata")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
client = get_client()
doctors = call(client.get, "/doctors") or []
doc = st.selectbox("Doctor", doctors, format_func=lambda d: d["full_name"], key="sched_doc")
tab_blocks, tab_leave, tab_holidays = st.tabs(["Weekly blocks", "Leave", "Holidays"])


def show_affected(res: dict) -> None:  # type: ignore[type-arg]
    affected = res.get("affected_appointments", [])
    if affected:
        st.warning(f"{len(affected)} booked appointment(s) are affected; patients were notified.")
        st.dataframe(affected, hide_index=True, width="stretch")
    else:
        st.success(f"Done. {res.get('blocked_slots', 0)} slots blocked; no bookings affected.")


with tab_blocks:
    if doc:
        tpls = call(client.get, f"/doctors/{doc['id']}/schedule-templates") or []
        for t in tpls:
            row = st.container(horizontal=True, vertical_alignment="center")
            with row:
                st.markdown(f"**{DAYS[t['weekday']]}** {t['start_local'][:5]}-{t['end_local'][:5]}")
                if st.button("Remove", key=f"rm_tpl_{t['id']}", icon=":material/delete:"):
                    res = call(client.delete, f"/schedule-templates/{t['id']}")
                    if res:
                        show_affected(res)
        with st.form("add_tpl"):
            wd = st.selectbox("Day", range(6), format_func=lambda i: DAYS[i])
            start = st.time_input("From", value=time(9, 0), step=900)
            end = st.time_input("To", value=time(13, 0), step=900)
            if st.form_submit_button("Add block", type="primary"):
                res = call(
                    client.post,
                    f"/doctors/{doc['id']}/schedule-templates",
                    {"weekday": wd, "start_local": start.isoformat(), "end_local": end.isoformat()},
                )
                if res:
                    st.success(f"Added. {res['opened_slots']} slots opened.")

with tab_leave:
    if doc:
        for lv in call(client.get, f"/doctors/{doc['id']}/leaves") or []:
            row = st.container(horizontal=True, vertical_alignment="center")
            with row:
                start_iso = datetime.fromisoformat(lv["start_utc"]).astimezone(IST).isoformat()
                end_iso = datetime.fromisoformat(lv["end_utc"]).astimezone(IST).isoformat()
                st.markdown(f"{fmt_local(start_iso)} → {fmt_local(end_iso)} · {lv['reason']}")
                if st.button("Remove", key=f"rm_leave_{lv['id']}", icon=":material/delete:"):
                    if call(client.delete, f"/leaves/{lv['id']}") is not None:
                        st.rerun()
        with st.form("add_leave"):
            d1 = st.date_input("From day", value=date.today() + timedelta(days=1))
            t1 = st.time_input("From time", value=time(9, 0), step=900)
            d2 = st.date_input("To day", value=date.today() + timedelta(days=1))
            t2 = st.time_input("To time", value=time(18, 0), step=900)
            reason = st.text_input("Reason", value="Leave")
            if st.form_submit_button("Add leave", type="primary"):
                body = {
                    "start": datetime.combine(d1, t1, IST).isoformat(),
                    "end": datetime.combine(d2, t2, IST).isoformat(),
                    "reason": reason,
                }
                res = call(client.post, f"/doctors/{doc['id']}/leaves", body)
                if res:
                    show_affected(res)

with tab_holidays:
    hols = call(client.get, "/holidays") or []
    st.dataframe(hols, hide_index=True, width="stretch")
    with st.form("add_holiday"):
        day = st.date_input("Day", value=date.today() + timedelta(days=7))
        name = st.text_input("Name")
        if st.form_submit_button("Add holiday", type="primary"):
            res = call(client.post, "/holidays", {"day": day.isoformat(), "name": name})
            if res:
                show_affected(res)
    if st.button("Regenerate slots (8 weeks)", icon=":material/refresh:"):
        res = call(client.post, "/admin/slots/regenerate", {"weeks": 8})
        if res is not None:
            st.success(f"{res['count']} new slots created.")
