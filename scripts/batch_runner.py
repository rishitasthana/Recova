"""
scripts/batch_runner.py — Run the full pipeline over 50-100 synthetic events.

Injects synthetic payment.failed events directly into the DB (bypassing the
webhook) so the pipeline can be validated quickly without a running server
or live Razorpay calls (except for retry actions, which hit the test API).

Each synthetic event gets a unique razorpay_event_id so the dedup constraint
never rejects them.

HARD INVARIANT CHECK (correctness guarantee):
  After processing all events, this script asserts that zero rows in
  actions_log show a hard-decline classification paired with action_type
  retry_now or retry_scheduled. If this invariant is ever violated, the
  script exits with a non-zero status code and prints a loud error.
  This is the single most important correctness bar for the revenue-recovery
  track.

USAGE:
  python scripts/batch_runner.py

OUTPUT:
  Console summary + data/batch_results.json (read by the Streamlit dashboard)
"""

import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fix Windows console encoding for special characters
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

# Add project root to sys.path so recova package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from recova import db
from recova.pipeline import process_failed_payment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

BATCH_RESULTS_PATH = os.getenv("BATCH_RESULTS_PATH", "data/batch_results.json")

# ─────────────────────────────────────────────────────────────────────────────
# Synthetic event profiles
# Each profile represents one class of decline scenario.
# Weights control the proportion in the batch.
# ─────────────────────────────────────────────────────────────────────────────
EVENT_PROFILES = [
    # Soft declines — retriable
    {
        "label": "insufficient_funds",
        "error_code": "insufficient_funds",
        "error_description": "Your payment was declined due to insufficient funds.",
        "error_reason": "insufficient_funds",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "expected_type": "soft",
        "amount_paise": 99900,       # ₹999
        "weight": 25,
    },
    {
        "label": "issuer_unavailable",
        "error_code": "issuer_unavailable",
        "error_description": "The payment was declined as the bank is unavailable.",
        "error_reason": "issuer_unavailable",
        "error_source": "bank",
        "error_step": "payment_authorization",
        "expected_type": "soft",
        "amount_paise": 99900,
        "weight": 15,
    },
    {
        "label": "gateway_error",
        "error_code": "GATEWAY_ERROR",
        "error_description": "Payment gateway returned an error. Please try again.",
        "error_reason": "gateway_error",
        "error_source": "gateway",
        "error_step": "payment_authorization",
        "expected_type": "soft",
        "amount_paise": 49900,       # ₹499 — qualifies for retry_now
        "weight": 10,
    },
    {
        "label": "payment_timeout",
        "error_code": "payment_timeout",
        "error_description": "Payment timed out. Please try again.",
        "error_reason": "payment_timeout",
        "error_source": "gateway",
        "error_step": "payment_authorization",
        "expected_type": "soft",
        "amount_paise": 29900,       # ₹299 — qualifies for retry_now
        "weight": 10,
    },
    # Hard declines — no retry
    {
        "label": "card_expired",
        "error_code": "card_expired",
        "error_description": "Your card has expired. Please update your payment method.",
        "error_reason": "card_expired",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "expected_type": "hard",
        "amount_paise": 99900,
        "weight": 15,
    },
    {
        "label": "card_blacklisted",
        "error_code": "card_blacklisted",
        "error_description": "Your card has been blocked by the issuer.",
        "error_reason": "card_blacklisted",
        "error_source": "bank",
        "error_step": "payment_authorization",
        "expected_type": "hard",
        "amount_paise": 99900,
        "weight": 8,
    },
    {
        "label": "lost_card",
        "error_code": "lost_card",
        "error_description": "This card has been reported as lost.",
        "error_reason": "lost_card",
        "error_source": "bank",
        "error_step": "payment_authorization",
        "expected_type": "hard",
        "amount_paise": 99900,
        "weight": 5,
    },
    {
        "label": "do_not_honor",
        "error_code": "do_not_honor",
        "error_description": "The issuer has declined the payment without specifying a reason.",
        "error_reason": "do_not_honor",
        "error_source": "bank",
        "error_step": "payment_authorization",
        "expected_type": "hard",
        "amount_paise": 99900,
        "weight": 7,
    },
    # Stale events — should always stop regardless of decline type
    {
        "label": "stale_soft_decline",
        "error_code": "insufficient_funds",
        "error_description": "Insufficient funds.",
        "error_reason": "insufficient_funds",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "expected_type": "soft",
        "amount_paise": 99900,
        "weight": 3,
        "age_days_override": 20,    # 20 days old — past NO_CONTACT_AFTER_DAYS
    },
    # Unknown code — should default to soft
    {
        "label": "unknown_error",
        "error_code": "UNKNOWN_DECLINE_CODE_XYZ",
        "error_description": "Payment declined for an unspecified reason.",
        "error_reason": "unknown",
        "error_source": "unknown",
        "error_step": "payment_authorization",
        "expected_type": "soft",    # our default
        "amount_paise": 99900,
        "weight": 2,
    },
]

# Build a weighted list
_WEIGHTED_PROFILES: list[dict] = []
for profile in EVENT_PROFILES:
    _WEIGHTED_PROFILES.extend([profile] * profile["weight"])


def make_subscription_ids(n: int) -> list[str]:
    """Generate n fake subscription IDs to distribute events across."""
    return [f"sub_batch_{i:03d}" for i in range(n)]


def build_synthetic_event(profile: dict, subscription_id: str, age_days: float = 0) -> dict:
    """Build a synthetic payment.failed event dict for direct DB insertion."""
    event_time = datetime.now(timezone.utc) - timedelta(days=age_days)
    event_id = f"evt_batch_{uuid.uuid4().hex[:16]}"

    return {
        "razorpay_event_id": event_id,
        "payment_id": f"pay_batch_{uuid.uuid4().hex[:14]}",
        "subscription_id": subscription_id,
        "customer_id": f"cust_batch_{random.randint(1000, 9999)}",
        "amount": profile["amount_paise"],
        "currency": "INR",
        "status": "failed",
        "error_code": profile["error_code"],
        "error_description": profile["error_description"],
        "error_reason": profile["error_reason"],
        "error_source": profile["error_source"],
        "error_step": profile["error_step"],
        "raw_payload": json.dumps({
            "event": "payment.failed",
            "synthetic": True,
            "profile_label": profile["label"],
        }),
        "_created_at_override": event_time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def insert_synthetic_event_with_time(event_data: dict) -> int | None:
    """
    Insert a synthetic event, with optional created_at override for stale testing.
    Returns the new row id or None if duplicate.
    """
    created_at = event_data.pop("_created_at_override", None)

    sql = """
        INSERT OR IGNORE INTO payment_events
            (razorpay_event_id, payment_id, subscription_id, customer_id,
             amount, currency, status, error_code, error_description,
             error_reason, error_source, error_step, raw_payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with db.get_connection() as conn:
        cur = conn.execute(sql, (
            event_data.get("razorpay_event_id"),
            event_data.get("payment_id"),
            event_data.get("subscription_id"),
            event_data.get("customer_id"),
            event_data.get("amount"),
            event_data.get("currency", "INR"),
            event_data.get("status"),
            event_data.get("error_code"),
            event_data.get("error_description"),
            event_data.get("error_reason"),
            event_data.get("error_source"),
            event_data.get("error_step"),
            event_data.get("raw_payload"),
            created_at,
        ))
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid
        return None


def run_invariant_check() -> tuple[bool, int, list[dict]]:
    """
    HARD INVARIANT: No hard-decline event must ever produce a retry action.
    Returns (passed: bool, violation_count: int, violations: list[dict])
    """
    sql = """
        SELECT
            pe.payment_id,
            pe.error_code,
            c.classification,
            d.decision,
            al.action_type,
            d.rule_triggered,
            al.executed_at
        FROM actions_log al
        JOIN decisions d       ON al.decision_id       = d.id
        JOIN classifications c ON d.classification_id  = c.id
        JOIN payment_events pe ON c.payment_event_id   = pe.id
        WHERE c.classification = 'hard'
          AND al.action_type IN ('retry_now', 'retry_scheduled')
    """
    with db.get_connection() as conn:
        violations = [dict(r) for r in conn.execute(sql).fetchall()]
    return (len(violations) == 0, len(violations), violations)


def compute_avg_time_to_recovery() -> float | None:
    """Average minutes from payment_event.created_at to actions_log.executed_at for successful retries."""
    sql = """
        SELECT
            AVG(
                (julianday(al.executed_at) - julianday(pe.created_at)) * 24 * 60
            ) AS avg_minutes
        FROM actions_log al
        JOIN decisions d       ON al.decision_id       = d.id
        JOIN classifications c ON d.classification_id  = c.id
        JOIN payment_events pe ON c.payment_event_id   = pe.id
        WHERE al.action_type IN ('retry_now', 'retry_scheduled')
          AND al.outcome = 'success'
    """
    with db.get_connection() as conn:
        row = conn.execute(sql).fetchone()
        return round(row[0], 2) if row and row[0] is not None else None


def compute_stopped_by_rule() -> dict[str, int]:
    sql = """
        SELECT d.rule_triggered, COUNT(*) as cnt
        FROM decisions d
        WHERE d.decision = 'stop'
        GROUP BY d.rule_triggered
    """
    with db.get_connection() as conn:
        rows = conn.execute(sql).fetchall()
        return {r["rule_triggered"]: r["cnt"] for r in rows}


def print_summary(results: dict) -> None:
    inv = results["invariant_check_passed"]
    inv_str = "✅ PASSED" if inv else f"❌ FAILED ({results['invariant_violations']} violations)"

    print("\n" + "=" * 70)
    print("RECOVA BATCH RUNNER — RESULTS SUMMARY")
    print("=" * 70)
    print(f"  Run at:                {results['run_at']}")
    print(f"  Total events:          {results['total_events']}")
    print(f"  Revenue at risk:       INR {results['revenue_at_risk_inr']:,.2f}")
    print(f"  Amount recovered:      INR {results['recovered_inr']:,.2f}")
    print(f"  Overall recovery rate: {results['recovery_rate'] * 100:.1f}%")
    if results["avg_time_to_recovery_minutes"] is not None:
        print(f"  Avg time-to-recovery:  {results['avg_time_to_recovery_minutes']:.1f} min")
    print()
    print("  Decision distribution:")
    for k, v in sorted(results["decision_counts"].items()):
        print(f"    {k:<30} {v}")
    print()
    print("  Classifications:")
    for k, v in sorted(results["classification_counts"].items()):
        print(f"    {k:<30} {v}")
    print()
    print("  Stopped by rule:")
    for k, v in sorted(results["stopped_by_rule"].items()):
        print(f"    {k:<40} {v}")
    print()
    print("  Recovery by type:")
    for r in results["recovery_by_type"]:
        rate = r["recovery_rate"] * 100
        print(
            f"    {r['classification'] or 'unknown':<10} "
            f"total={r['total']:>3}  "
            f"at_risk=INR {r['at_risk_inr']:>8,.2f}  "
            f"recovered=INR {r['recovered_inr']:>8,.2f}  "
            f"rate={rate:.1f}%"
        )
    print()
    print(f"  INVARIANT CHECK (no hard-decline retries): {inv_str}")
    print("=" * 70)


def main():
    Path(os.path.dirname(BATCH_RESULTS_PATH)).mkdir(parents=True, exist_ok=True)

    db.init_db()

    N_EVENTS = 75  # adjust between 50-100 as needed
    N_SUBSCRIPTIONS = 15  # distribute across several subscriptions

    subscription_ids = make_subscription_ids(N_SUBSCRIPTIONS)

    logger.info("=" * 70)
    logger.info("ReCova Batch Runner — injecting %d synthetic events", N_EVENTS)
    logger.info("=" * 70)

    injected = 0
    skipped = 0
    results_list = []

    for i in range(N_EVENTS):
        profile = random.choice(_WEIGHTED_PROFILES)
        subscription_id = random.choice(subscription_ids)
        age_days = profile.get("age_days_override", random.uniform(0, 2))

        event_data = build_synthetic_event(profile, subscription_id, age_days=age_days)
        event_id = insert_synthetic_event_with_time(event_data)

        if event_id is None:
            skipped += 1
            continue

        injected += 1
        result = process_failed_payment(event_id)
        results_list.append(result)

        # Simulate realistic downstream recovery: ~40% of soft retries or customer reminder actions succeed
        if result.action_log_id and result.classification_id:
            cls_row = db.get_classification(result.classification_id)
            if cls_row and dict(cls_row).get("classification") == "soft":
                if random.random() < 0.40:
                    db.record_recovery(
                        event_id=event_id,
                        detail="Simulated customer settlement / successful retry payment",
                    )

        if (i + 1) % 10 == 0:
            logger.info("  Progress: %d/%d events processed", i + 1, N_EVENTS)

    logger.info("Injected: %d | Skipped (dup): %d", injected, skipped)

    # Gather metrics
    metrics = db.get_summary_metrics()
    invariant_passed, violation_count, violations = run_invariant_check()
    avg_recovery_time = compute_avg_time_to_recovery()
    stopped_by_rule = compute_stopped_by_rule()

    # Enrich recovery_by_type with INR values
    recovery_by_type_enriched = []
    for r in metrics["recovery_by_type"]:
        recovery_by_type_enriched.append({
            **r,
            "at_risk_inr": r["at_risk"] / 100,
            "recovered_inr": r["recovered"] / 100,
            "recovery_rate": round(r["recovered"] / r["at_risk"], 4) if r["at_risk"] > 0 else 0.0,
        })

    batch_results = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total_events": metrics["total_events"],
        "revenue_at_risk_paise": metrics["revenue_at_risk_paise"],
        "revenue_at_risk_inr": metrics["revenue_at_risk_paise"] / 100,
        "recovered_paise": metrics["recovered_paise"],
        "recovered_inr": metrics["recovered_paise"] / 100,
        "recovery_rate": metrics["recovery_rate"],
        "avg_time_to_recovery_minutes": avg_recovery_time,
        "decision_counts": metrics["decision_counts"],
        "classification_counts": metrics["classification_counts"],
        "stopped_by_rule": stopped_by_rule,
        "recovery_by_type": recovery_by_type_enriched,
        "invariant_check_passed": invariant_passed,
        "invariant_violations": violation_count,
        "invariant_violation_details": violations,
    }

    with open(BATCH_RESULTS_PATH, "w") as f:
        json.dump(batch_results, f, indent=2, default=str)

    print_summary(batch_results)

    if not invariant_passed:
        logger.error("")
        logger.error("❌ INVARIANT VIOLATION: %d hard-decline events received retry actions!", violation_count)
        logger.error("Violation details written to %s", BATCH_RESULTS_PATH)
        logger.error("This is a critical correctness bug — fix classifier.py or decision_engine.py before demo.")
        sys.exit(1)

    logger.info("")
    logger.info("Batch results written to %s", BATCH_RESULTS_PATH)
    logger.info("Open the Streamlit dashboard to see results: streamlit run dashboard/app.py")


if __name__ == "__main__":
    main()
