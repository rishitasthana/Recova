"""
tests/test_db.py — Unit tests for recova/db.py helpers.

Tests in this file exercise DB-layer functions that no other test file covers:
  - get_retry_count() derivation from the audit log (0 / 1 / 3 retries, scoping)
  - rule precedence validated end-to-end through decide() with real DB rows
    (stale + hard → rule_1_stale_stop, not rule_2 or rule_3)

All tests use the ``test_db`` fixture from conftest.py, so they never touch
data/recova.db.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a full audit chain row for retry_count tests
# ─────────────────────────────────────────────────────────────────────────────

def _insert_retry_action(db_module, subscription_id: str, action_type: str) -> None:
    """
    Insert a minimal payment_event → classification → decision → actions_log
    chain so that get_retry_count(subscription_id) counts it correctly.

    ``action_type`` must be 'retry_now' or 'retry_scheduled'.
    """
    import uuid

    event_id = db_module.insert_payment_event({
        "razorpay_event_id": f"evt_retry_{uuid.uuid4().hex[:12]}",
        "payment_id": f"pay_retry_{uuid.uuid4().hex[:12]}",
        "subscription_id": subscription_id,
        "customer_id": "cust_retry_001",
        "amount": 99900,
        "currency": "INR",
        "status": "failed",
        "error_code": "insufficient_funds",
        "error_description": "Insufficient funds.",
        "error_reason": "insufficient_funds",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "raw_payload": "{}",
    })
    assert event_id is not None, "insert_payment_event returned None (duplicate?)"

    cls_id = db_module.insert_classification({
        "payment_event_id": event_id,
        "classification": "soft",
        "confidence": 0.7,
        "reason": "fallback soft",
        "error_code": "insufficient_funds",
        "error_reason": "insufficient_funds",
        "lookup_source": "fallback_table",
    })

    dec_id = db_module.insert_decision({
        "classification_id": cls_id,
        "decision": action_type,
        "retry_at": None,
        "rule_triggered": "rule_6_soft_retry_scheduled",
        "rule_explanation": "Transient soft decline; schedule retry.",
    })

    db_module.insert_action_log({
        "decision_id": dec_id,
        "action_type": action_type,
        "payload": None,
        "outcome": "success",
        "outcome_detail": "retry order created",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tests: get_retry_count derivation
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryCountDerivation:
    """
    Exercises db.get_retry_count(subscription_id) against a fresh isolated DB.

    This function derives the retry count from the audit log (no separate
    counter column), so the JOIN across all four tables must work correctly.
    Flagged earlier as lacking dedicated coverage.
    """

    def test_zero_retries_for_new_subscription(self, test_db):
        """A subscription with no prior actions should return retry_count == 0."""
        import recova.db as db_module

        count = db_module.get_retry_count("sub_brand_new_xyz")
        assert count == 0, f"Expected 0 retries for unknown subscription, got {count}"

    def test_one_retry_now_counts_as_one(self, test_db):
        """One retry_now action log entry should yield retry_count == 1."""
        import recova.db as db_module

        sub_id = "sub_one_retry_now"
        _insert_retry_action(db_module, sub_id, "retry_now")

        count = db_module.get_retry_count(sub_id)
        assert count == 1, f"Expected 1, got {count}"

    def test_one_retry_scheduled_counts_as_one(self, test_db):
        """retry_scheduled also counts as a retry attempt."""
        import recova.db as db_module

        sub_id = "sub_one_scheduled"
        _insert_retry_action(db_module, sub_id, "retry_scheduled")

        count = db_module.get_retry_count(sub_id)
        assert count == 1, f"Expected 1, got {count}"

    def test_three_retries_count_correctly(self, test_db):
        """Three retry actions across different events must yield retry_count == 3."""
        import recova.db as db_module

        sub_id = "sub_three_retries"
        _insert_retry_action(db_module, sub_id, "retry_now")
        _insert_retry_action(db_module, sub_id, "retry_scheduled")
        _insert_retry_action(db_module, sub_id, "retry_scheduled")

        count = db_module.get_retry_count(sub_id)
        assert count == 3, f"Expected 3, got {count}"

    def test_retry_count_is_subscription_scoped(self, test_db):
        """retry_count for sub_a must not be polluted by retries for sub_b."""
        import recova.db as db_module

        sub_a = "sub_count_scope_a"
        sub_b = "sub_count_scope_b"
        _insert_retry_action(db_module, sub_a, "retry_now")
        _insert_retry_action(db_module, sub_b, "retry_now")
        _insert_retry_action(db_module, sub_b, "retry_scheduled")

        assert db_module.get_retry_count(sub_a) == 1, "sub_a should have 1 retry"
        assert db_module.get_retry_count(sub_b) == 2, "sub_b should have 2 retries"

    def test_non_retry_actions_do_not_count(self, test_db):
        """stop / send_reminder / escalate actions must NOT increment retry_count."""
        import recova.db as db_module

        sub_id = "sub_non_retry_actions"

        event_id = db_module.insert_payment_event({
            "razorpay_event_id": "evt_stop_only",
            "payment_id": "pay_stop_001",
            "subscription_id": sub_id,
            "customer_id": "cust_stop",
            "amount": 99900,
            "currency": "INR",
            "status": "failed",
            "error_code": "card_blacklisted",
            "error_description": "Card blacklisted.",
            "error_reason": "card_blacklisted",
            "error_source": "bank",
            "error_step": "payment_authorization",
            "raw_payload": "{}",
        })
        cls_id = db_module.insert_classification({
            "payment_event_id": event_id,
            "classification": "hard",
            "confidence": 0.7,
            "reason": "fallback hard",
            "error_code": "card_blacklisted",
            "error_reason": "card_blacklisted",
            "lookup_source": "fallback_table",
        })
        dec_id = db_module.insert_decision({
            "classification_id": cls_id,
            "decision": "stop",
            "retry_at": None,
            "rule_triggered": "rule_2_hard_permanent_stop",
            "rule_explanation": "Card permanently unusable.",
        })
        db_module.insert_action_log({
            "decision_id": dec_id,
            "action_type": "stop",
            "payload": None,
            "outcome": "skipped",
            "outcome_detail": "Recovery stopped.",
        })

        count = db_module.get_retry_count(sub_id)
        assert count == 0, (
            f"stop action must not count as a retry; expected 0, got {count}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helper: insert payment_events row with backdated created_at
# ─────────────────────────────────────────────────────────────────────────────

def _insert_stale_event_with_classification(
    db_module, age_days: float, error_code: str, classification: str, unique_suffix: str
) -> int:
    """
    Insert a payment_event backdated by ``age_days`` and classify it.
    Returns the classification row id for passing directly to decide().
    """
    import sqlite3 as _sqlite3
    from datetime import datetime, timedelta, timezone

    event_time = datetime.now(timezone.utc) - timedelta(days=age_days)
    event_time_str = event_time.strftime("%Y-%m-%d %H:%M:%S")

    active_path = db_module.get_db_path()
    conn = _sqlite3.connect(active_path)
    conn.row_factory = _sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        """INSERT OR IGNORE INTO payment_events
           (razorpay_event_id, payment_id, subscription_id, customer_id,
            amount, currency, status, error_code, error_description,
            error_reason, error_source, error_step, raw_payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"evt_{unique_suffix}",
            f"pay_{unique_suffix}",
            f"sub_{unique_suffix}",
            "cust_stale_001",
            99900, "INR", "failed",
            error_code,
            f"{error_code} decline",
            error_code,
            "bank",
            "payment_authorization",
            "{}",
            event_time_str,
        ),
    )
    conn.commit()
    event_id = cur.lastrowid
    conn.close()

    cls_id = db_module.insert_classification({
        "payment_event_id": event_id,
        "classification": classification,
        "confidence": 0.7,
        "reason": f"test: {error_code}",
        "error_code": error_code,
        "error_reason": error_code,
        "lookup_source": "fallback_table",
    })
    return cls_id


# ─────────────────────────────────────────────────────────────────────────────
# Tests: rule precedence validated end-to-end through decide()
# ─────────────────────────────────────────────────────────────────────────────

class TestRulePrecedenceWithDB:
    """
    Confirms "first match wins" ordering is correctly implemented — not just
    documented — by calling decide() against real DB rows and checking
    which rule_triggered was written to the decisions table.

    These tests complement the pure-function tests in test_decision_engine.py
    (which call _evaluate_rules directly) by validating the full decide()
    code path including DB reads and writes.
    """

    def test_stale_hard_permanent_hits_rule_1_not_rule_2(self, test_db):
        """
        Event: 20-day-old (stale) + card_blacklisted (permanently unrecoverable hard).
        Expected rule: rule_1_stale_stop.
        Wrong result would be rule_2_hard_permanent_stop.
        """
        import recova.db as db_module
        from recova import decision_engine

        cls_id = _insert_stale_event_with_classification(
            db_module, age_days=20, error_code="card_blacklisted",
            classification="hard", unique_suffix="stale_perm_hard"
        )
        decision_id = decision_engine.decide(cls_id)
        assert decision_id is not None, "decide() returned None — check DB state"

        row = db_module.get_decision(decision_id)
        assert row["rule_triggered"] == "rule_1_stale_stop", (
            f"Expected rule_1_stale_stop (staleness must check first), "
            f"got '{row['rule_triggered']}'. Rule precedence is broken."
        )
        assert row["decision"] == "stop"

    def test_stale_hard_other_hits_rule_1_not_rule_3(self, test_db):
        """
        Event: 20-day-old (stale) + card_expired (non-permanent hard, normally → rule_3).
        Expected rule: rule_1_stale_stop.
        Wrong result would be rule_3_hard_other_remind.
        """
        import recova.db as db_module
        from recova import decision_engine

        cls_id = _insert_stale_event_with_classification(
            db_module, age_days=20, error_code="card_expired",
            classification="hard", unique_suffix="stale_card_expired"
        )
        decision_id = decision_engine.decide(cls_id)
        assert decision_id is not None

        row = db_module.get_decision(decision_id)
        assert row["rule_triggered"] == "rule_1_stale_stop", (
            f"Expected rule_1_stale_stop, got '{row['rule_triggered']}'. "
            f"Stale check must fire before hard-decline classification."
        )

    def test_fresh_hard_permanent_hits_rule_2_not_rule_1(self, test_db):
        """
        Control: fresh (1 day) + card_blacklisted.
        Expected rule: rule_2_hard_permanent_stop.
        Confirms rule_1 does NOT over-fire on fresh events.
        """
        import recova.db as db_module
        from recova import decision_engine

        cls_id = _insert_stale_event_with_classification(
            db_module, age_days=1, error_code="card_blacklisted",
            classification="hard", unique_suffix="fresh_perm_hard"
        )
        decision_id = decision_engine.decide(cls_id)
        assert decision_id is not None

        row = db_module.get_decision(decision_id)
        assert row["rule_triggered"] == "rule_2_hard_permanent_stop", (
            f"Expected rule_2_hard_permanent_stop for fresh permanently-unrecoverable "
            f"hard decline, got '{row['rule_triggered']}'."
        )

    def test_fresh_hard_other_hits_rule_3_not_rule_1(self, test_db):
        """
        Control: fresh (1 day) + card_expired.
        Expected rule: rule_3_hard_other_remind.
        Confirms rule_3 fires correctly when the event is not stale.
        """
        import recova.db as db_module
        from recova import decision_engine

        cls_id = _insert_stale_event_with_classification(
            db_module, age_days=1, error_code="card_expired",
            classification="hard", unique_suffix="fresh_card_expired"
        )
        decision_id = decision_engine.decide(cls_id)
        assert decision_id is not None

        row = db_module.get_decision(decision_id)
        assert row["rule_triggered"] == "rule_3_hard_other_remind", (
            f"Expected rule_3_hard_other_remind for fresh non-permanent hard decline, "
            f"got '{row['rule_triggered']}'."
        )
