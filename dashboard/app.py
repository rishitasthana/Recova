"""
dashboard/app.py — ReCova Streamlit Dashboard

Live view of the payment recovery pipeline:
  - KPI tiles: events, revenue at risk, recovered, recovery rate
  - Live feed table: event → classification → decision → action → outcome
  - Charts: decision distribution, recovery by type, cumulative recovery
  - Batch results panel: summary from batch_runner.py

USAGE:
  streamlit run dashboard/app.py
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# Allow importing recova package from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from recova import db

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ReCova — AI Revenue Recovery Agent",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="collapsed",
)

BATCH_RESULTS_PATH = os.getenv("BATCH_RESULTS_PATH", "data/batch_results.json")

# ─────────────────────────────────────────────────────────────────────────────
# Styling
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

.main { background-color: #0d1117; color: #e6edf3; }

.header-container {
    background: linear-gradient(135deg, #1a1f2e 0%, #0d1117 60%, #0f1a2e 100%);
    border: 1px solid #21262d;
    border-radius: 16px;
    padding: 2rem 2.5rem;
    margin-bottom: 1.5rem;
}

.header-title {
    font-size: 2.4rem;
    font-weight: 700;
    background: linear-gradient(90deg, #58a6ff, #7c3aed, #db61a2);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0;
}

.header-sub {
    color: #8b949e;
    font-size: 1rem;
    margin-top: 0.4rem;
}

.metric-card {
    background: linear-gradient(135deg, #161b22 0%, #1c2128 100%);
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    text-align: center;
}

.metric-value {
    font-size: 2rem;
    font-weight: 700;
    color: #58a6ff;
}

.metric-label {
    font-size: 0.8rem;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-top: 0.3rem;
}

.metric-value.green { color: #3fb950; }
.metric-value.purple { color: #d2a8ff; }
.metric-value.orange { color: #d29922; }

.section-header {
    font-size: 1.1rem;
    font-weight: 600;
    color: #e6edf3;
    margin-bottom: 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.tag {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}

.tag-hard { background: #3d1515; color: #ff7b72; border: 1px solid #ff7b72; }
.tag-soft { background: #152a3d; color: #58a6ff; border: 1px solid #58a6ff; }
.tag-stop { background: #3d1515; color: #f85149; border: 1px solid #f85149; }
.tag-retry { background: #1a2f1a; color: #3fb950; border: 1px solid #3fb950; }
.tag-remind { background: #2b2415; color: #d29922; border: 1px solid #d29922; }
.tag-escalate { background: #2a1b3d; color: #d2a8ff; border: 1px solid #d2a8ff; }
.tag-success { background: #1a2f1a; color: #3fb950; }
.tag-failure { background: #3d1515; color: #ff7b72; }
.tag-logged { background: #1b2535; color: #79c0ff; }
.tag-skipped { background: #21262d; color: #8b949e; }

.invariant-pass {
    background: #1a2f1a;
    border: 1px solid #3fb950;
    border-radius: 8px;
    padding: 0.8rem 1.2rem;
    color: #3fb950;
    font-weight: 600;
}

.invariant-fail {
    background: #3d1515;
    border: 1px solid #f85149;
    border-radius: 8px;
    padding: 0.8rem 1.2rem;
    color: #f85149;
    font-weight: 600;
}

.stDataFrame { border-radius: 8px; overflow: hidden; }

div[data-testid="stMetric"] {
    background: linear-gradient(135deg, #161b22 0%, #1c2128 100%);
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 1rem;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=5)
def load_pipeline_data(limit: int = 200) -> pd.DataFrame:
    """Load the full pipeline view from SQLite."""
    db.init_db()
    rows = db.get_full_pipeline_view(limit=limit)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=5)
def load_metrics() -> dict:
    return db.get_summary_metrics()


@st.cache_data(ttl=10)
def load_batch_results() -> dict | None:
    if not Path(BATCH_RESULTS_PATH).exists():
        return None
    with open(BATCH_RESULTS_PATH) as f:
        return json.load(f)


def fmt_inr(paise: int) -> str:
    inr = paise / 100
    if inr >= 100_000:
        return f"₹{inr/100_000:.1f}L"
    if inr >= 1_000:
        return f"₹{inr/1_000:.1f}K"
    return f"₹{inr:,.0f}"


def classification_tag(val: str) -> str:
    cls = val.lower() if val else "unknown"
    if cls == "hard":
        return '<span class="tag tag-hard">hard</span>'
    if cls == "soft":
        return '<span class="tag tag-soft">soft</span>'
    return f'<span class="tag">{val}</span>'


def decision_tag(val: str) -> str:
    if not val:
        return "—"
    if "stop" in val:
        return f'<span class="tag tag-stop">{val}</span>'
    if "retry" in val:
        return f'<span class="tag tag-retry">{val}</span>'
    if "reminder" in val or "remind" in val:
        return f'<span class="tag tag-remind">{val}</span>'
    if "escalate" in val:
        return f'<span class="tag tag-escalate">{val}</span>'
    return val


def outcome_tag(val: str) -> str:
    if not val:
        return "—"
    mapping = {
        "success": "tag-success",
        "failure": "tag-failure",
        "api_error": "tag-failure",
        "logged": "tag-logged",
        "skipped": "tag-skipped",
    }
    cls = mapping.get(val, "")
    return f'<span class="tag {cls}">{val}</span>'


PLOTLY_DARK = dict(
    paper_bgcolor="#0d1117",
    plot_bgcolor="#161b22",
    font=dict(color="#8b949e", family="Inter"),
    title_font=dict(color="#e6edf3"),
)

DECISION_COLORS = {
    "retry_now": "#3fb950",
    "retry_scheduled": "#58a6ff",
    "send_reminder": "#d29922",
    "escalate_promise_to_pay": "#d2a8ff",
    "stop": "#f85149",
}

CLASSIFICATION_COLORS = {
    "soft": "#58a6ff",
    "hard": "#ff7b72",
    "unknown": "#8b949e",
}


# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="header-container">
    <div class="header-title">💰 ReCova</div>
    <div class="header-sub">
        AI Revenue Recovery Agent &nbsp;·&nbsp; Razorpay Buildathon
        &nbsp;·&nbsp; Rules-based · Fully auditable · No black box
    </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Auto-refresh
# ─────────────────────────────────────────────────────────────────────────────
col_refresh, col_ts = st.columns([1, 5])
with col_refresh:
    auto_refresh = st.toggle("Auto-refresh (5s)", value=False)
with col_ts:
    st.caption(f"Last loaded: {datetime.now().strftime('%H:%M:%S')}")

if auto_refresh:
    st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Load data
# ─────────────────────────────────────────────────────────────────────────────
df = load_pipeline_data()
metrics = load_metrics()
batch = load_batch_results()

# ─────────────────────────────────────────────────────────────────────────────
# KPI tiles
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("### 📊 Recovery Metrics")
k1, k2, k3, k4 = st.columns(4)

with k1:
    st.metric("Total Failed Events", metrics.get("total_events", 0))

with k2:
    risk_paise = metrics.get("revenue_at_risk_paise", 0)
    st.metric("Revenue at Risk", fmt_inr(risk_paise))

with k3:
    rec_paise = metrics.get("recovered_paise", 0)
    st.metric("Amount Recovered", fmt_inr(rec_paise))

with k4:
    rate = metrics.get("recovery_rate", 0) * 100
    st.metric("Recovery Rate", f"{rate:.1f}%")

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Charts row
# ─────────────────────────────────────────────────────────────────────────────
chart_col1, chart_col2, chart_col3 = st.columns(3)

with chart_col1:
    st.markdown("#### Decision Distribution")
    dec_counts = metrics.get("decision_counts", {})
    if dec_counts:
        fig = px.pie(
            values=list(dec_counts.values()),
            names=list(dec_counts.keys()),
            color=list(dec_counts.keys()),
            color_discrete_map=DECISION_COLORS,
            hole=0.45,
        )
        fig.update_traces(textinfo="label+percent", textfont_size=11)
        fig.update_layout(
            **PLOTLY_DARK,
            showlegend=False,
            margin=dict(l=10, r=10, t=10, b=10),
            height=250,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No decisions recorded yet.")

with chart_col2:
    st.markdown("#### Recovery Rate by Decline Type")
    rbt = metrics.get("recovery_by_type", [])
    if rbt:
        rbt_df = pd.DataFrame(rbt)
        rbt_df["recovery_rate_pct"] = rbt_df.apply(
            lambda r: round(r["recovered"] / r["at_risk"] * 100, 1) if r["at_risk"] > 0 else 0,
            axis=1,
        )
        rbt_df["classification"] = rbt_df["classification"].fillna("unknown")
        fig = px.bar(
            rbt_df,
            x="classification",
            y="recovery_rate_pct",
            color="classification",
            color_discrete_map=CLASSIFICATION_COLORS,
            text="recovery_rate_pct",
            labels={"recovery_rate_pct": "Recovery Rate (%)", "classification": "Decline Type"},
        )
        fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig.update_layout(
            **PLOTLY_DARK,
            showlegend=False,
            margin=dict(l=10, r=10, t=10, b=10),
            height=250,
            yaxis=dict(range=[0, 110], gridcolor="#21262d"),
            xaxis=dict(gridcolor="#21262d"),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No recovery data yet.")

with chart_col3:
    st.markdown("#### Cumulative Recovered (₹)")
    if not df.empty and "executed_at" in df.columns:
        df_success = df[
            (df["outcome"] == "success") &
            (df["action_type"].isin(["retry_now", "retry_scheduled"]))
        ].copy()

        if not df_success.empty:
            df_success["executed_at"] = pd.to_datetime(df_success["executed_at"])
            df_success = df_success.sort_values("executed_at")
            df_success["amount_inr"] = df_success["amount"] / 100
            df_success["cumulative"] = df_success["amount_inr"].cumsum()

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df_success["executed_at"],
                y=df_success["cumulative"],
                mode="lines+markers",
                line=dict(color="#3fb950", width=2),
                marker=dict(size=5),
                fill="tozeroy",
                fillcolor="rgba(63,185,80,0.1)",
            ))
            fig.update_layout(
                **PLOTLY_DARK,
                xaxis=dict(gridcolor="#21262d"),
                yaxis=dict(gridcolor="#21262d", title="₹ Recovered"),
                margin=dict(l=10, r=10, t=10, b=10),
                height=250,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No successful recoveries yet.")
    else:
        st.info("No data yet.")

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Live feed table
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("### 🔄 Live Event Feed")
st.caption("Showing the most recent 200 failed payment events and their pipeline outcomes.")

if df.empty:
    st.info("No events yet. Run `python scripts/batch_runner.py` or trigger a webhook.")
else:
    display_cols = [
        "payment_id", "amount", "error_code",
        "classification", "decision", "rule_triggered",
        "action_type", "outcome", "event_time",
    ]
    # Only keep columns that exist
    display_cols = [c for c in display_cols if c in df.columns]
    feed = df[display_cols].copy()

    # Format amount
    if "amount" in feed.columns:
        feed["amount"] = feed["amount"].apply(
            lambda x: f"₹{x/100:,.0f}" if pd.notna(x) else "—"
        )

    # Format timestamp
    if "event_time" in feed.columns:
        feed["event_time"] = pd.to_datetime(feed["event_time"]).dt.strftime("%m/%d %H:%M:%S")

    feed = feed.fillna("—")

    st.dataframe(
        feed,
        use_container_width=True,
        height=400,
        column_config={
            "payment_id": st.column_config.TextColumn("Payment ID", width="medium"),
            "amount": st.column_config.TextColumn("Amount", width="small"),
            "error_code": st.column_config.TextColumn("Error Code", width="medium"),
            "classification": st.column_config.TextColumn("Classification", width="small"),
            "decision": st.column_config.TextColumn("Decision", width="medium"),
            "rule_triggered": st.column_config.TextColumn("Rule", width="large"),
            "action_type": st.column_config.TextColumn("Action", width="medium"),
            "outcome": st.column_config.TextColumn("Outcome", width="small"),
            "event_time": st.column_config.TextColumn("Event Time", width="medium"),
        },
    )

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Batch Results panel
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("### 🧪 Batch Runner Results")

if batch is None:
    st.info(
        "No batch results found. Run `python scripts/batch_runner.py` to populate this panel.",
        icon="ℹ️",
    )
else:
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        st.metric("Batch Events", batch.get("total_events", 0))
    with b2:
        st.metric("At Risk", f"₹{batch.get('revenue_at_risk_inr', 0):,.0f}")
    with b3:
        st.metric("Recovered", f"₹{batch.get('recovered_inr', 0):,.0f}")
    with b4:
        avg = batch.get("avg_time_to_recovery_minutes")
        st.metric("Avg Recovery Time", f"{avg:.1f} min" if avg is not None else "N/A")

    # Invariant check status
    inv_passed = batch.get("invariant_check_passed", False)
    inv_count = batch.get("invariant_violations", 0)
    if inv_passed:
        st.markdown(
            '<div class="invariant-pass">✅ INVARIANT CHECK PASSED — '
            'Zero hard-decline events received a retry action. '
            'Correctness guarantee holds.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="invariant-fail">❌ INVARIANT VIOLATION — '
            f'{inv_count} hard-decline event(s) received a retry action. '
            f'This is a critical bug — check classifier.py and decision_engine.py.</div>',
            unsafe_allow_html=True,
        )

    st.markdown("##### Decision Counts")
    dec_df = pd.DataFrame(
        [(k, v) for k, v in batch.get("decision_counts", {}).items()],
        columns=["Decision", "Count"],
    )
    st.dataframe(dec_df, use_container_width=True, hide_index=True)

    st.markdown("##### Stopped by Rule")
    stop_df = pd.DataFrame(
        [(k, v) for k, v in batch.get("stopped_by_rule", {}).items()],
        columns=["Rule", "Count"],
    )
    if not stop_df.empty:
        st.dataframe(stop_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No stops recorded.")

    st.markdown("##### Recovery by Decline Type")
    rbt_batch = batch.get("recovery_by_type", [])
    if rbt_batch:
        rbt_batch_df = pd.DataFrame(rbt_batch)
        for col in ["at_risk_inr", "recovered_inr"]:
            if col in rbt_batch_df.columns:
                rbt_batch_df[col] = rbt_batch_df[col].apply(lambda x: f"₹{x:,.2f}")
        if "recovery_rate" in rbt_batch_df.columns:
            rbt_batch_df["recovery_rate"] = rbt_batch_df["recovery_rate"].apply(lambda x: f"{x*100:.1f}%")
        st.dataframe(rbt_batch_df, use_container_width=True, hide_index=True)

    run_at = batch.get("run_at", "")
    if run_at:
        st.caption(f"Batch run at: {run_at}")

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="text-align:center;color:#444d56;font-size:0.8rem;margin-top:1rem;">
    ReCova · Razorpay Buildathon · Revenue Recovery Track ·
    Rules-based · No LLMs · Every decision is traceable
</div>
""", unsafe_allow_html=True)
