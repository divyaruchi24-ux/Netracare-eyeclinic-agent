import streamlit as st

from netracare.ui.components import call
from netracare.ui.state import get_client

client = get_client()
days = st.segmented_control(
    "Period",
    [7, 30, 90],
    default=30,
    format_func=lambda d: f"{d} days",
    key="usage_days",
    required=True,
)
u = call(client.get, "/admin/llm-usage", days=days)
if not u:
    st.stop()

m = st.container(horizontal=True)
with m:
    st.metric("Today", f"${u['today_usd']:.4f}", f"of ${u['daily_budget_usd']:.2f}", border=True)
    st.metric(
        "Month to date",
        f"${u['month_to_date_usd']:.4f}",
        f"of ${u['monthly_budget_usd']:.2f}",
        border=True,
    )
    st.metric("Calls", u["total_calls"], border=True)
    st.metric("Failed", sum(v for k, v in u["by_status"].items() if k != "ok"), border=True)

if u["by_day"]:
    st.subheader("Daily cost (USD)")
    st.bar_chart({"cost": u["by_day"]}, x_label="Day", y_label="USD")

c1, c2 = st.columns(2)
with c1:
    st.subheader("By model")
    st.dataframe(
        [{"Model": k, **v} for k, v in u["by_model"].items()], hide_index=True, width="stretch"
    )
with c2:
    st.subheader("By feature")
    st.dataframe(
        [{"Feature": k, **v} for k, v in u["by_purpose"].items()], hide_index=True, width="stretch"
    )

st.subheader("Recent calls")
st.caption("Prompt and response text are never stored; only usage metadata.")
st.dataframe(
    [
        {
            "When": c["at_utc"][:19].replace("T", " "),
            "Feature": c["purpose"],
            "Prompt": f"{c['prompt_id']}@v{c['prompt_version']}",
            "Model": c["model"],
            "Tokens in/out": f"{c['input_tokens']}/{c['output_tokens']}",
            "Cost": c["cost_usd"],
            "Latency ms": c["latency_ms"],
            "Status": c["error_code"] or c["status"],
        }
        for c in u["recent"]
    ],
    hide_index=True,
    width="stretch",
)
