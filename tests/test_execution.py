"""
tests/test_execution.py — Unit tests for the execution layer.

Tests cover all four execution branches:
  - stop:                    logs reason, no API call
  - send_reminder:           logs mock email payload
  - escalate_promise_to_pay: logs mock escalation ticket
  - retry_now / retry_scheduled: Razorpay API call (mocked)

All tests use the ``test_db`` fixture from conftest.py so they never touch
data/recova.db.

Razorpay API calls are mocked via unittest.mock.patch so no credentials are
required and tests run fully offline.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a full audit chain and return the decision id
# ─────────────────────────────────────────────────────────────────────────────

def _build_full_chain(
    db_module,
    *,
    decision_type: str,
    error_code: str,
    classification: str,
    amount: int = 99900,
    subscription_id: str = "sub_exec_001",
    unique_suffix: str = "",
) -> int:
    """
    Insert payment_event → classification → decision and return the decision id.
    Tests pass this id directly to execution.execute().
    """
    uid = unique_suffix or f"{decision_type}_{error_code}"
    event_id = db_module.insert_payment_event({
        "razorpay_event_id": f"evt_exec_{uid}",
        "payment_id": f"pay_exec_{uid}",
        "subscription_id": subscription_id,
        "customer_id": "cust_exec_001",
        "amount": amount,
        "currency": "INR",
        "status": "failed",
        "error_code": error_code,
        "error_description": f"{error_code} decline",
        "error_reason": error_code,
        "error_source": "bank",
        "error_step": "payment_authorization",
        "raw_payload": "{}",
    })
    assert event_id is not None, "insert_payment_event returned None (duplicate?)"

    cls_id = db_module.insert_classification({
        "payment_event_id": event_id,
        "classification": classification,
        "confidence": 0.7,
        "reason": f"fallback: {error_code}",
        "error_code": error_code,
        "error_reason": error_code,
        "lookup_source": "fallback_table",
    })

    dec_id = db_module.insert_decision({
        "classification_id": cls_id,
        "decision": decision_type,
        "retry_at": None,
        "rule_triggered": "rule_test",
        "rule_explanation": "Test rule explanation for execution tests.",
    })
    return dec_id


# ─────────────────────────────────────────────────────────────────────────────
# Stop branch
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteStop:
    """execute() with decision='stop' must log a skipped outcome, no API calls."""

    def test_stop_writes_skipped_outcome(self, test_db):
        """stop decision must write outcome='skipped' to actions_log."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="stop",
            error_code="card_blacklisted",
            classification="hard",
            unique_suffix="stop_blacklisted",
        )
        action_id = execution.execute(dec_id)
        assert action_id is not None

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["action_type"] == "stop"
        assert row["outcome"] == "skipped"
        assert row["decision_id"] == dec_id

    def test_stop_payload_contains_rule_and_payment(self, test_db):
        """stop payload JSON must include rule_triggered, payment_id, amount_inr."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="stop",
            error_code="lost_card",
            classification="hard",
            unique_suffix="stop_lost",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        payload = json.loads(row["payload"])
        assert "rule_triggered" in payload
        assert "payment_id" in payload
        assert "amount_inr" in payload

    def test_stop_returns_action_log_id(self, test_db):
        """execute() must return a non-None int for the stop branch."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="stop",
            error_code="stolen_card",
            classification="hard",
            unique_suffix="stop_stolen",
        )
        action_id = execution.execute(dec_id)
        assert isinstance(action_id, int)
        assert action_id > 0

    def test_missing_decision_returns_none(self, test_db):
        """execute() with a nonexistent decision_id must return None, not raise."""
        from recova import execution

        result = execution.execute(decision_id=999999)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Send Reminder branch
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteSendReminder:
    """execute() with decision='send_reminder' must build and log a mock email."""

    def test_reminder_writes_logged_outcome(self, test_db):
        """send_reminder must write outcome='logged' to actions_log."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="send_reminder",
            error_code="card_expired",
            classification="hard",
            unique_suffix="remind_expired",
        )
        action_id = execution.execute(dec_id)
        assert action_id is not None

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["action_type"] == "send_reminder"
        assert row["outcome"] == "logged"
        assert row["decision_id"] == dec_id

    def test_reminder_payload_is_valid_email(self, test_db):
        """Payload must contain channel, to, subject, body, and metadata."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="send_reminder",
            error_code="do_not_honor",
            classification="hard",
            unique_suffix="remind_dnh",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        payload = json.loads(row["payload"])
        assert payload["channel"] == "email"
        assert "@" in payload["to"]
        assert len(payload["subject"]) > 10
        assert len(payload["body"]) > 20
        assert "recova_decision_id" in payload["metadata"]

    def test_expired_card_subject_mentions_expired(self, test_db):
        """card_expired error_code must produce a subject that mentions 'expired'."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="send_reminder",
            error_code="card_expired",
            classification="hard",
            unique_suffix="remind_exp_subject",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        payload = json.loads(row["payload"])
        assert "expired" in payload["subject"].lower(), (
            "Subject for card_expired must mention 'expired' to give users clear context"
        )

    def test_non_expired_code_uses_generic_subject(self, test_db):
        """Non-expired error codes must use the generic payment-method subject."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="send_reminder",
            error_code="do_not_honor",
            classification="hard",
            unique_suffix="remind_generic",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        payload = json.loads(row["payload"])
        # Generic subject should NOT say expired
        assert "expired" not in payload["subject"].lower()
        assert "payment" in payload["subject"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Escalate branch
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteEscalate:
    """execute() with decision='escalate_promise_to_pay' must build a ticket."""

    def test_escalate_writes_logged_outcome(self, test_db):
        """escalate_promise_to_pay must write outcome='logged'."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="escalate_promise_to_pay",
            error_code="insufficient_funds",
            classification="soft",
            unique_suffix="esc_soft",
        )
        action_id = execution.execute(dec_id)
        assert action_id is not None

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["action_type"] == "escalate_promise_to_pay"
        assert row["outcome"] == "logged"

    def test_escalate_payload_is_valid_ticket(self, test_db):
        """Escalation payload must be a well-formed ticket JSON."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="escalate_promise_to_pay",
            error_code="issuer_unavailable",
            classification="soft",
            amount=150000,   # ₹1500 → high priority
            unique_suffix="esc_highval",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        payload = json.loads(row["payload"])
        assert payload["ticket_type"] == "promise_to_pay_escalation"
        assert payload["amount_inr"] == 1500.0
        assert "suggested_script" in payload
        assert "suggested_contact_window" in payload
        assert "recova_decision_id" in payload

    def test_high_value_escalation_is_high_priority(self, test_db):
        """Amounts ≥ ₹1000 (100_000 paise) must yield priority='high'."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="escalate_promise_to_pay",
            error_code="insufficient_funds",
            classification="soft",
            amount=100000,   # exactly ₹1000
            unique_suffix="esc_threshold",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert json.loads(row["payload"])["priority"] == "high"

    def test_low_value_escalation_is_medium_priority(self, test_db):
        """Amounts < ₹1000 must yield priority='medium'."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="escalate_promise_to_pay",
            error_code="insufficient_funds",
            classification="soft",
            amount=50000,   # ₹500
            unique_suffix="esc_lowval",
        )
        action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert json.loads(row["payload"])["priority"] == "medium"


# ─────────────────────────────────────────────────────────────────────────────
# Retry branch (Razorpay API mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteRetry:
    """execute() for retry_now/retry_scheduled calls Razorpay — API is mocked."""

    @staticmethod
    def _mock_success_client():
        mock_order = {"id": "order_test_abc", "status": "created", "amount": 99900}
        mock_client = MagicMock()
        mock_client.order.create.return_value = mock_order
        return mock_client

    @staticmethod
    def _mock_failure_client(message: str = "API connection refused"):
        mock_client = MagicMock()
        mock_client.order.create.side_effect = Exception(message)
        return mock_client

    def test_retry_now_success_writes_success_outcome(self, test_db):
        """retry_now with a successful Razorpay call must write outcome='success'."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="retry_now",
            error_code="insufficient_funds",
            classification="soft",
            unique_suffix="retry_now_ok",
        )

        with patch.object(execution, "_get_razorpay_client", return_value=self._mock_success_client()):
            action_id = execution.execute(dec_id)

        assert action_id is not None

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["action_type"] == "retry_now"
        assert row["outcome"] == "success"

        detail = json.loads(row["outcome_detail"])
        assert detail["razorpay_order_id"] == "order_test_abc"
        assert detail["status"] == "created"

    def test_retry_scheduled_success_writes_success_outcome(self, test_db):
        """retry_scheduled with a successful API call must write outcome='success'."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="retry_scheduled",
            error_code="issuer_unavailable",
            classification="soft",
            unique_suffix="retry_sched_ok",
        )

        with patch.object(execution, "_get_razorpay_client", return_value=self._mock_success_client()):
            action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["outcome"] == "success"

    def test_retry_api_error_does_not_raise(self, test_db):
        """When Razorpay raises, execute() must catch it and return an action_log id."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="retry_now",
            error_code="insufficient_funds",
            classification="soft",
            unique_suffix="retry_err",
        )

        with patch.object(execution, "_get_razorpay_client", return_value=self._mock_failure_client()):
            action_id = execution.execute(dec_id)

        assert action_id is not None, "execute() must not raise on API error"

    def test_retry_api_error_writes_api_error_outcome(self, test_db):
        """When Razorpay raises, outcome must be 'api_error' and detail must include the message."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="retry_now",
            error_code="insufficient_funds",
            classification="soft",
            unique_suffix="retry_err2",
        )

        with patch.object(execution, "_get_razorpay_client", return_value=self._mock_failure_client("timeout on order")):
            action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome, outcome_detail FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["outcome"] == "api_error"
        assert "timeout on order" in row["outcome_detail"]

    def test_retry_payload_contains_order_fields(self, test_db):
        """Retry payload must include amount, currency, receipt, and notes."""
        import recova.db as db_module
        from recova import execution

        dec_id = _build_full_chain(
            db_module,
            decision_type="retry_now",
            error_code="insufficient_funds",
            classification="soft",
            amount=49900,
            unique_suffix="retry_payload",
        )

        with patch.object(execution, "_get_razorpay_client", return_value=self._mock_success_client()):
            action_id = execution.execute(dec_id)

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        payload = json.loads(row["payload"])
        assert payload["amount"] == 49900
        assert payload["currency"] == "INR"
        assert "receipt" in payload
        assert "notes" in payload
        assert "recova_decision_id" in payload["notes"]


# ─────────────────────────────────────────────────────────────────────────────
# Unknown decision type fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteUnknownDecisionType:
    """An unrecognised decision type must produce outcome='failure', not raise."""

    def test_unknown_decision_writes_failure_outcome(self, test_db):
        """
        Validates the fallback else-branch in execute().
        We monkey-patch get_decision to return a fake dict with an unknown
        decision type while leaving all other DB calls intact.
        """
        import recova.db as db_module
        from recova import execution

        # Insert a valid chain with a DB-legal decision ('stop')
        dec_id = _build_full_chain(
            db_module,
            decision_type="stop",
            error_code="card_blacklisted",
            classification="hard",
            unique_suffix="unknown_type",
        )

        # Wrap get_decision to inject an unknown decision type for this id only
        original_get_decision = db_module.get_decision

        def _fake_get_decision(did):
            row = original_get_decision(did)
            if row is None:
                return None
            d = dict(row)
            if d["id"] == dec_id:
                d["decision"] = "teleport_customer"   # unknown type
            return d

        db_module.get_decision = _fake_get_decision
        try:
            action_id = execution.execute(dec_id)
        finally:
            db_module.get_decision = original_get_decision

        assert action_id is not None

        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT outcome, action_type FROM actions_log WHERE id = ?", (action_id,)
            ).fetchone()

        assert row["outcome"] == "failure"
        assert row["action_type"] == "teleport_customer"
