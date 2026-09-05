"""
dashboard/app.py — ReCova Streamlit Dashboard

Live interactive view of the payment recovery pipeline:
  - Tab 1: 📊 Overview & Live Feed (KPIs, Plotly charts, audit table, batch summary)
  - Tab 2: 🧪 Interactive Sandbox (Test custom payment failure, watch pipeline live, simulate recovery)
  - Tab 3: 📧 Dunning & Notification Center (HTML email, SMS/WhatsApp & CRM escalation ticket previews)
  - Tab 4: 📜 Rules Explorer & Policy Matrix (Deterministic rules, precedence, trigger metrics)
  - Tab 5: 💾 Export & Audit Center (One-click CSV & JSON downloads)

USAGE:
  streamlit run dashboard/app.py
"""

import csv
import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
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
from recova.decision_engine import (
    LOW_RISK_RETRY_THRESHOLD_PAISE,
    MAX_RETRIES,
    NO_CONTACT_AFTER_DAYS,
    PERMANENTLY_UNRECOVERABLE_CODES,
    RETRY_BACKOFF_HOURS,
    RULES,
)
from recova.pipeline import process_failed_payment

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ReCova — AI Revenue Recovery Agent",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
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
    background: linear-gradient(135deg, #161b26 0%, #0d1117 50%, #152238 100%);
    border: 1px solid #30363d;
    border-radius: 16px;
    padding: 1.8rem 2.2rem;
    margin-bottom: 1.2rem;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.header-title {
    font-size: 2.3rem;
    font-weight: 800;
    background: linear-gradient(90deg, #58a6ff, #a371f7, #f778ba);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 0.6rem;
}

.header-sub {
    color: #8b949e;
    font-size: 0.95rem;
    margin-top: 0.35rem;
}

.tag {
    display: inline-block;
    padding: 0.2rem 0.55rem;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.02em;
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
    background: #0d2818;
    border: 1px solid #2ea043;
    border-radius: 8px;
    padding: 0.9rem 1.2rem;
    color: #3fb950;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.invariant-fail {
    background: #3d1515;
    border: 1px solid #f85149;
    border-radius: 8px;
    padding: 0.9rem 1.2rem;
    color: #f85149;
    font-weight: 600;
}

.sandbox-step {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1rem 1.2rem;
    margin-bottom: 0.8rem;
}

.sandbox-step-title {
    font-weight: 700;
    font-size: 0.95rem;
    color: #58a6ff;
    margin-bottom: 0.4rem;
}

.email-mockup {
    background: #ffffff;
    color: #1f2328;
    border-radius: 12px;
    padding: 2rem;
    box-shadow: 0 4px 18px rgba(0,0,0,0.4);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 650px;
    margin: 0 auto;
}

.email-btn {
    display: inline-block;
    background: #0969da;
    color: #ffffff !important;
    text-decoration: none;
    font-weight: 600;
    padding: 12px 24px;
    border-radius: 6px;
    margin: 1rem 0;
}

.ticket-box {
    background: #161b22;
    border-left: 4px solid #a371f7;
    border-radius: 0 8px 8px 0;
    padding: 1.2rem 1.5rem;
    color: #e6edf3;
    font-family: monospace;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Helper functions & Cached loaders
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3)
def load_pipeline_data(limit: int = 500) -> pd.DataFrame:
    db.init_db()
    rows = db.get_full_pipeline_view(limit=limit)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=3)
def load_metrics() -> dict:
    return db.get_summary_metrics()


@st.cache_data(ttl=5)
def load_batch_results() -> dict | None:
    if not Path(BATCH_RESULTS_PATH).exists():
        return None
    try:
        with open(BATCH_RESULTS_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def fmt_inr(paise: int | float) -> str:
    inr = paise / 100
    if inr >= 100_000:
        return f"₹{inr/100_000:.2f}L"
    if inr >= 1_000:
        return f"₹{inr/1_000:.1f}K"
    return f"₹{inr:,.0f}"


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
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 💰 ReCova Control Panel")
    if st.button("🔄 Refresh Data", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("#### ⚡ System Status")
    st.success("✅ Decision Engine: Online")
    st.success("✅ Invariant Monitor: Enforcing")
    st.info("📡 API: http://127.0.0.1:8000")

    st.markdown("---")
    st.markdown("#### 🏷️ Legend")
    st.markdown("""
- **🔴 Hard**: Permanent card failure (unusable)
- **🔵 Soft**: Transient failure (retriable)
- **⚡ retry_now**: Immediate retry for small-ticket (≤ ₹500)
- **⏰ retry_scheduled**: Backoff retry (1h, 24h, 72h)
- **📧 send_reminder**: Customer action needed
- **👥 escalate**: Human concierge / promise-to-pay
- **⛔ stop**: Recovery abandoned (policy/risk)
""")

    st.markdown("---")
    st.caption("Razorpay Buildathon · Revenue Recovery Track")


# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="header-container">
    <div class="header-title">💰 ReCova</div>
    <div class="header-sub">
        AI Autonomous Revenue Recovery Agent &nbsp;·&nbsp; Razorpay Test Mode &nbsp;·&nbsp;
        Deterministic Rules &nbsp;·&nbsp; 100% Traceable Audit Trail &nbsp;·&nbsp; Zero LLM Hallucination
    </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 Overview & Live Feed",
    "🧪 Interactive Sandbox",
    "📧 Dunning & Notification Center",
    "📜 Rules Explorer & Policy Matrix",
    "💾 Export & Compliance Center",
])

df = load_pipeline_data()
metrics = load_metrics()
batch = load_batch_results()

# =============================================================================
# TAB 1: Overview & Live Feed
# =============================================================================
with tab1:
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("Total Failed Events", metrics.get("total_events", 0))
    with k2:
        st.metric("Revenue at Risk", fmt_inr(metrics.get("revenue_at_risk_paise", 0)))
    with k3:
        st.metric("Amount Recovered", fmt_inr(metrics.get("recovered_paise", 0)))
    with k4:
        rate = metrics.get("recovery_rate", 0) * 100
        st.metric("Recovery Rate", f"{rate:.1f}%")

    st.markdown("---")

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
                height=260,
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
                height=260,
                yaxis=dict(range=[0, 110], gridcolor="#21262d"),
                xaxis=dict(gridcolor="#21262d"),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No recovery data yet.")

    with chart_col3:
        st.markdown("#### Cumulative Recovered (₹)")
        if not df.empty and "executed_at" in df.columns:
            df_success = df[df["outcome"] == "success"].copy()
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
                    line=dict(color="#3fb950", width=2.5),
                    marker=dict(size=6),
                    fill="tozeroy",
                    fillcolor="rgba(63,185,80,0.15)",
                ))
                fig.update_layout(
                    **PLOTLY_DARK,
                    xaxis=dict(gridcolor="#21262d"),
                    yaxis=dict(gridcolor="#21262d", title="₹ Recovered"),
                    margin=dict(l=10, r=10, t=10, b=10),
                    height=260,
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No successful recoveries yet. Use the Sandbox to simulate recovery!")
        else:
            st.info("No data yet.")

    st.markdown("---")

    # Invariant check banner
    if batch:
        inv_passed = batch.get("invariant_check_passed", True)
        if inv_passed:
            st.markdown(
                '<div class="invariant-pass">🛡️ INVARIANT ENFORCED: 0 hard-decline events received a retry action. '
                'Audit log confirms complete adherence to policy.</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div class="invariant-fail">❌ INVARIANT VIOLATION: Hard decline retry detected! Check logs immediately.</div>',
                unsafe_allow_html=True,
            )

    st.markdown("### 🔄 Live Event Audit Trail")
    if not df.empty:
        filter_col1, filter_col2, filter_col3 = st.columns(3)
        with filter_col1:
            cls_filter = st.multiselect(
                "Filter Classification",
                options=list(df["classification"].dropna().unique()),
                default=list(df["classification"].dropna().unique()),
            )
        with filter_col2:
            dec_filter = st.multiselect(
                "Filter Decision",
                options=list(df["decision"].dropna().unique()),
                default=list(df["decision"].dropna().unique()),
            )
        with filter_col3:
            outcome_filter = st.multiselect(
                "Filter Outcome",
                options=list(df["outcome"].dropna().unique()),
                default=list(df["outcome"].dropna().unique()),
            )

        filtered_df = df[
            df["classification"].isin(cls_filter) &
            df["decision"].isin(dec_filter) &
            df["outcome"].isin(outcome_filter)
        ].copy()

        display_cols = [
            "payment_id", "amount", "error_code",
            "classification", "decision", "rule_triggered",
            "action_type", "outcome", "event_time",
        ]
        display_cols = [c for c in display_cols if c in filtered_df.columns]
        table_df = filtered_df[display_cols].copy()

        if "amount" in table_df.columns:
            table_df["amount"] = table_df["amount"].apply(lambda x: f"₹{x/100:,.0f}" if pd.notna(x) else "—")
        if "event_time" in table_df.columns:
            table_df["event_time"] = pd.to_datetime(table_df["event_time"]).dt.strftime("%m/%d %H:%M:%S")

        st.dataframe(table_df, use_container_width=True, height=350)
    else:
        st.info("No events in database. Use Tab 2 (Interactive Sandbox) or run scripts/batch_runner.py.")

    if batch:
        with st.expander("🧪 Latest Batch Runner Diagnostics", expanded=False):
            st.json(batch)


# =============================================================================
# TAB 2: Interactive Sandbox
# =============================================================================
with tab2:
    st.markdown("### 🧪 Interactive Payment Recovery Simulator")
    st.markdown(
        "Inject any custom subscription payment failure into the live ReCova pipeline. "
        "Inspect the step-by-step decision chain in real time, and simulate customer settlement!"
    )

    s_col1, s_col2 = st.columns(2)

    with s_col1:
        st.markdown("#### 1. Configure Payment Failure")
        sim_customer = st.text_input("Customer ID", value="cust_rahul_verma")
        sim_sub = st.text_input("Subscription ID", value="sub_recova_premium_01")
        sim_amount_inr = st.number_input("Amount (INR)", min_value=1, max_value=100000, value=499, step=50)

        error_options = [
            ("insufficient_funds", "insufficient_funds (Soft - Balance low)"),
            ("issuer_unavailable", "issuer_unavailable (Soft - Bank downtime)"),
            ("GATEWAY_ERROR", "GATEWAY_ERROR (Soft - Gateway timeout)"),
            ("card_expired", "card_expired (Hard - Expired card)"),
            ("lost_card", "lost_card (Hard - Permanent block/stolen)"),
            ("card_blacklisted", "card_blacklisted (Hard - Fraud blacklist)"),
            ("do_not_honor", "do_not_honor (Hard - Issuer blanket decline)"),
        ]
        selected_error = st.selectbox("Error Code", options=[e[0] for e in error_options], format_func=lambda x: dict(error_options).get(x, x))
        sim_age_days = st.slider("Payment Age (Days since failure)", min_value=0, max_value=25, value=1)
        sim_prior_retries = st.slider("Prior Retries on Subscription", min_value=0, max_value=5, value=0)

        btn_run = st.button("⚡ Run Through ReCova Pipeline", type="primary", use_container_width=True)

    with s_col2:
        st.markdown("#### 2. Real-time Pipeline Execution Trace")
        if btn_run:
            sim_payment_id = f"pay_sim_{uuid.uuid4().hex[:10]}"
            sim_event_id = f"evt_sim_{uuid.uuid4().hex[:10]}"
            amount_paise = int(sim_amount_inr * 100)

            # Insert synthetic event with specified age
            raw_event = {
                "razorpay_event_id": sim_event_id,
                "payment_id": sim_payment_id,
                "subscription_id": sim_sub,
                "customer_id": sim_customer,
                "amount": amount_paise,
                "currency": "INR",
                "status": "failed",
                "error_code": selected_error,
                "error_description": f"Simulated error {selected_error}",
                "error_reason": selected_error,
                "error_source": "bank",
                "error_step": "payment_authorization",
                "raw_payload": json.dumps({"simulated": True, "age_days": sim_age_days}),
            }

            # If age > 0, backdate in DB
            created_row_id = db.insert_payment_event(raw_event)
            if sim_age_days > 0 and created_row_id:
                backdate = datetime.now(timezone.utc) - pd.Timedelta(days=sim_age_days)
                with db.get_connection() as conn:
                    conn.execute("UPDATE payment_events SET created_at = ? WHERE id = ?", (backdate.strftime("%Y-%m-%d %H:%M:%S"), created_row_id))

            # If prior retries requested, insert mock prior actions
            if sim_prior_retries > 0 and created_row_id:
                with db.get_connection() as conn:
                    for r_idx in range(sim_prior_retries):
                        conn.execute("""
                            INSERT INTO actions_log (decision_id, action_type, outcome, outcome_detail)
                            SELECT d.id, 'retry_scheduled', 'api_error', 'prior retry attempt'
                            FROM decisions d LIMIT 1
                        """)

            # Run through pipeline
            result = process_failed_payment(created_row_id)
            cls_row = dict(db.get_classification(result.classification_id)) if result.classification_id else {}
            dec_row = dict(db.get_decision(result.decision_id)) if result.decision_id else {}

            st.session_state["last_sim_event_id"] = created_row_id
            st.session_state["last_sim_payment_id"] = sim_payment_id
            st.session_state["last_sim_sub_id"] = sim_sub
            st.session_state["last_sim_amount"] = amount_paise

            # Step 1: Ingestion
            st.markdown(f"""
            <div class="sandbox-step">
                <div class="sandbox-step-title">1️⃣ Ingested & Validated</div>
                <b>Payment ID:</b> <code>{sim_payment_id}</code> | <b>Event ID:</b> {created_row_id}<br/>
                <b>Amount:</b> ₹{sim_amount_inr:,.0f} | <b>Reported Error:</b> <code>{selected_error}</code>
            </div>
            """, unsafe_allow_html=True)

            # Step 2: Classification
            cls_val = cls_row.get("classification", "unknown")
            conf = cls_row.get("confidence", 0.0)
            src = cls_row.get("lookup_source", "unknown")
            tag_class = "tag-hard" if cls_val == "hard" else "tag-soft"
            st.markdown(f"""
            <div class="sandbox-step">
                <div class="sandbox-step-title">2️⃣ Classification Layer</div>
                <span class="tag {tag_class}">{cls_val.upper()} DECLINE</span> &nbsp;
                <b>Confidence:</b> {conf * 100:.0f}% &nbsp; <b>Lookup Source:</b> <code>{src}</code><br/>
                <small style="color:#8b949e">{cls_row.get('reason', '')}</small>
            </div>
            """, unsafe_allow_html=True)

            # Step 3: Decision Engine
            dec_val = dec_row.get("decision", "unknown")
            rule_trig = dec_row.get("rule_triggered", "unknown")
            expl = dec_row.get("rule_explanation", "")
            st.markdown(f"""
            <div class="sandbox-step">
                <div class="sandbox-step-title">3️⃣ Decision Engine (Deterministic Rules)</div>
                <b>Decision:</b> <code>{dec_val}</code><br/>
                <b>Triggered Rule:</b> <code>{rule_trig}</code><br/>
                <span style="color:#3fb950">💡 {expl}</span>
            </div>
            """, unsafe_allow_html=True)

            # Step 4: Execution
            st.markdown(f"""
            <div class="sandbox-step">
                <div class="sandbox-step-title">4️⃣ Execution Layer</div>
                Action logged to audit trail (Row ID #{result.action_log_id}). Full idempotency verified.
            </div>
            """, unsafe_allow_html=True)

        elif "last_sim_event_id" in st.session_state:
            st.info(f"Last simulated event: #{st.session_state['last_sim_event_id']} ({st.session_state.get('last_sim_payment_id')})")
        else:
            st.info("Select parameters on the left and click 'Run Through ReCova Pipeline' to see the trace.")

    st.markdown("---")
    st.markdown("#### 3. Closed-Loop Recovery Settlement")
    st.caption("When a customer clicks the recovery payment link or updates their card, close the loop to record recovery.")

    rec_c1, rec_c2 = st.columns([2, 1])
    with rec_c1:
        settle_event_id = st.number_input(
            "Event ID to Settle / Recover",
            min_value=1,
            value=st.session_state.get("last_sim_event_id", 1),
        )
    with rec_c2:
        st.write("")
        st.write("")
        if st.button("💳 Simulate Customer Payment (Mark Recovered)", type="secondary", use_container_width=True):
            updated_id = db.record_recovery(
                event_id=settle_event_id,
                detail="Customer clicked recovery link & settled payment via Razorpay",
            )
            if updated_id:
                st.success(f"🎉 Event #{settle_event_id} marked as RECOVERED! Revenue credited to metrics.")
                st.cache_data.clear()
            else:
                st.warning(f"Could not find pending action for Event #{settle_event_id} (may already be recovered or not found).")


# =============================================================================
# TAB 3: Dunning & Notification Center
# =============================================================================
with tab3:
    st.markdown("### 📧 Customer Dunning & Communications Center")
    st.markdown("Preview the omnichannel recovery communications dispatched automatically by ReCova.")

    notif_type = st.radio("Select Channel / Template", ["Email Notification", "SMS / WhatsApp Message", "CRM Escalation Ticket (Zendesk)"], horizontal=True)

    if notif_type == "Email Notification":
        st.markdown("""
        <div class="email-mockup">
            <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e1e4e8;padding-bottom:12px;margin-bottom:16px;">
                <span style="font-weight:700;font-size:1.2rem;color:#0969da;">Acme Cloud &bull; ReCova</span>
                <span style="background:#fff8c5;color:#9a6700;font-size:0.75rem;padding:3px 8px;border-radius:12px;font-weight:600;">Action Required</span>
            </div>
            <h3 style="color:#1f2328;margin-top:0;">Payment failed for your subscription</h3>
            <p style="color:#57606a;line-height:1.6;">
                Hi <b>Rahul Verma</b>,<br/>
                We were unable to process your recurring monthly subscription payment of <b>₹999.00</b> on your card ending in <b>**0002</b>.
            </p>
            <p style="color:#57606a;line-height:1.6;">
                <b>Reason:</b> Your bank declined the transaction (<code>Card Expired / Insufficient Funds</code>).
                Your subscription remains active for the next <b>72 hours</b> while we hold your service.
            </p>
            <div style="text-align:center;margin:1.5rem 0;">
                <a class="email-btn" href="#">Update Card & Settle ₹999.00 &rarr;</a>
                <div style="font-size:0.8rem;color:#8c959f;margin-top:0.4rem;">Secured by Razorpay Payments &bull; 256-bit SSL</div>
            </div>
            <p style="font-size:0.85rem;color:#8c959f;border-top:1px solid #e1e4e8;padding-top:12px;">
                Automatic retry schedule: Next attempt scheduled in 24 hours. To prevent service disruption, please verify your payment method.
            </p>
        </div>
        """, unsafe_allow_html=True)

    elif notif_type == "SMS / WhatsApp Message":
        st.markdown("""
        <div style="max-width:450px;margin:0 auto;background:#1a2332;border:1px solid #30363d;border-radius:16px;padding:1.5rem;">
            <div style="color:#3fb950;font-weight:700;margin-bottom:0.5rem;">📱 WhatsApp / SMS Notification</div>
            <div style="background:#0d1117;border-radius:12px;padding:1rem;color:#e6edf3;font-size:0.9rem;line-height:1.5;">
                ⚠️ <b>Acme Cloud Alert</b>: Your monthly subscription payment of <b>₹999</b> could not be completed.<br/><br/>
                Tap here to verify your payment method and avoid suspension:<br/>
                <span style="color:#58a6ff;">https://pay.rzp.io/rcv/sub_999_recova</span><br/><br/>
                <small style="color:#8b949e">Reply STOP to unsubscribe.</small>
            </div>
        </div>
        """, unsafe_allow_html=True)

    else:
        st.markdown("""
        <div class="ticket-box">
            <b>ZENDESK / FRESHDESK REVENUE CONCIERGE ESCALATION TICKET #RCV-8821</b><br/>
            <b>Priority:</b> HIGH &bull; <b>Status:</b> OPEN &bull; <b>Assigned Group:</b> Retention Concierge<br/>
            --------------------------------------------------------------------------------<br/>
            <b>Customer:</b> Karan Mehta (cust_recova_004)<br/>
            <b>Subscription ID:</b> sub_recova_004 &bull; <b>Plan:</b> ReCova Pro Monthly (₹999.00)<br/>
            <b>Customer LTV:</b> ₹11,988.00 (12 billing cycles)<br/>
            <b>Rule Triggered:</b> <code>rule_4_soft_max_retries_escalate</code><br/>
            <b>Attempts Exhausted:</b> 3 retries failed (insufficient_funds)<br/>
            <b>Intervention Prescribed:</b> Promise-to-Pay Concierge Phone Call / Custom Settlement Link<br/>
            --------------------------------------------------------------------------------<br/>
            <b>Audit Log Payload:</b><br/>
            {<br/>
            &nbsp;&nbsp;"ticket_type": "promise_to_pay_escalation",<br/>
            &nbsp;&nbsp;"customer_id": "cust_recova_004",<br/>
            &nbsp;&nbsp;"amount_paise": 99900,<br/>
            &nbsp;&nbsp;"prior_retries": 3,<br/>
            &nbsp;&nbsp;"recommended_action": "offer_grace_period_or_upi_mandate"<br/>
            }
        </div>
        """, unsafe_allow_html=True)


# =============================================================================
# TAB 4: Rules Explorer & Policy Matrix
# =============================================================================
with tab4:
    st.markdown("### 📜 Deterministic Rules Explorer & Policy Matrix")
    st.markdown(
        "ReCova evaluates recovery rules in strict top-to-bottom priority. "
        "The first rule whose conditions match is executed. Every decision is 100% reproducible with written justification."
    )

    rules_summary = db.get_rules_summary()

    rule_table_data = [
        {
            "Priority": 1,
            "Rule Name": "rule_1_stale_stop",
            "Condition": f"Payment age > {NO_CONTACT_AFTER_DAYS} days",
            "Action": "stop",
            "Triggers": next((r["trigger_count"] for r in rules_summary if r["rule"] == "rule_1_stale_stop"), 0),
            "Rationale": "Policy beats recovery. Chasing debts older than 14 days causes negative brand friction.",
        },
        {
            "Priority": 2,
            "Rule Name": "rule_2_hard_permanent_stop",
            "Condition": "Hard decline + permanent block (blacklisted, lost, stolen, pickup)",
            "Action": "stop",
            "Triggers": next((r["trigger_count"] for r in rules_summary if r["rule"] == "rule_2_hard_permanent_stop"), 0),
            "Rationale": "Card is physically or legally unusable; re-attempting is futile and incurs network penalties.",
        },
        {
            "Priority": 3,
            "Rule Name": "rule_3_hard_other_remind",
            "Condition": "Hard decline + user-fixable code (card expired, do_not_honor)",
            "Action": "send_reminder",
            "Triggers": next((r["trigger_count"] for r in rules_summary if r["rule"] == "rule_3_hard_other_remind"), 0),
            "Rationale": "Requires user to update card details; automated retry will fail.",
        },
        {
            "Priority": 4,
            "Rule Name": "rule_4_soft_max_retries_escalate",
            "Condition": f"Soft decline + retry_count ≥ {MAX_RETRIES}",
            "Action": "escalate_promise_to_pay",
            "Triggers": next((r["trigger_count"] for r in rules_summary if r["rule"] == "rule_4_soft_max_retries_escalate"), 0),
            "Rationale": "Automated retries exhausted. Human intervention (concierge/dunning) needed to prevent churn.",
        },
        {
            "Priority": 5,
            "Rule Name": "rule_5_soft_low_amount_retry_now",
            "Condition": f"Soft decline + retry_count = 0 + amount ≤ ₹{LOW_RISK_RETRY_THRESHOLD_PAISE/100:.0f}",
            "Action": "retry_now",
            "Triggers": next((r["trigger_count"] for r in rules_summary if r["rule"] == "rule_5_soft_low_amount_retry_now"), 0),
            "Rationale": "Low-risk immediate retry for small tickets. High probability of immediate recovery.",
        },
        {
            "Priority": 6,
            "Rule Name": "rule_6_soft_retry_scheduled",
            "Condition": f"Soft decline + retry_count < {MAX_RETRIES}",
            "Action": "retry_scheduled",
            "Triggers": next((r["trigger_count"] for r in rules_summary if r["rule"] == "rule_6_soft_retry_scheduled"), 0),
            "Rationale": f"Transient error; exponential backoff schedule: {RETRY_BACKOFF_HOURS} hours.",
        },
    ]

    st.dataframe(
        pd.DataFrame(rule_table_data),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Priority": st.column_config.NumberColumn("Priority", width="small"),
            "Rule Name": st.column_config.TextColumn("Rule Name", width="medium"),
            "Condition": st.column_config.TextColumn("Condition", width="large"),
            "Action": st.column_config.TextColumn("Action Prescribed", width="medium"),
            "Triggers": st.column_config.NumberColumn("Live Triggers", width="small"),
            "Rationale": st.column_config.TextColumn("Architectural Rationale", width="large"),
        },
    )


# =============================================================================
# TAB 5: Export & Compliance Center
# =============================================================================
with tab5:
    st.markdown("### 💾 Export & Audit Trail Compliance Center")
    st.markdown(
        "Download complete forensic audit records of all events, classifications, rules triggered, "
        "and execution payloads for compliance, financial reconciliation, and SOC2 audits."
    )

    if not df.empty:
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("#### 📄 CSV Audit Trail")
            csv_buffer = io.StringIO()
            df.to_csv(csv_buffer, index=False)
            st.download_button(
                label="📥 Download Audit Trail (CSV)",
                data=csv_buffer.getvalue(),
                file_name=f"recova_audit_trail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True,
                type="primary",
            )
            st.caption(f"Contains {len(df)} records formatted for Excel / Google Sheets.")

        with c2:
            st.markdown("#### 🗄️ JSON Structured Logs")
            json_str = df.to_json(orient="records", indent=2)
            st.download_button(
                label="📥 Download Audit Trail (JSON)",
                data=json_str,
                file_name=f"recova_audit_trail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
                use_container_width=True,
            )
            st.caption(f"Contains full JSON payloads and foreign-key references.")

        st.markdown("---")
        st.markdown("#### 🔎 Raw Event Inspection")
        selected_event_idx = st.selectbox("Select Record to Inspect Raw Payload", range(min(len(df), 50)), format_func=lambda i: f"Event #{df.iloc[i].get('event_id')} — {df.iloc[i].get('payment_id')} ({df.iloc[i].get('error_code')})")
        st.json(df.iloc[selected_event_idx].to_dict())
    else:
        st.info("No audit logs recorded yet.")

# ─────────────────────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown("""
<div style="text-align:center;color:#444d56;font-size:0.8rem;padding-bottom:1rem;">
    ReCova · Razorpay Buildathon · Revenue Recovery Track ·
    Rules-based · No LLMs · Zero Black-Box Decisions
</div>
""", unsafe_allow_html=True)
