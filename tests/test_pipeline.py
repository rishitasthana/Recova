"""
tests/test_pipeline.py — Integration tests for recova/pipeline.py.

Tests the full process_failed_payment() orchestration end-to-end:

  Happy path:
    - All three stages (classify → decide → execute) complete successfully
    - PipelineResult fields are correctly populated
    - Full FK audit chain is written to all four tables
    - Core correctness invariant: hard declines never produce retry actions

  Error propagation:
    - Missing event_id → result.error set, no partial ids
    - Non-failed status (captured) → classify() returns None → result.error
    - classifier.classify raises → result.error, no decision or action ids
    - decision_engine.decide raises → result.error, classification_id preserved
    - execution.execute raises → result.error, both upstream ids preserved

All tests use the test_db fixture (isolated temp DB).
Razorpay API calls in the retry execution branch are mocked offline.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Helper: insert a minimal payment_events row
# ─────────────────────────────────────────────────────────────────────────────

def _insert_event(
    db_module,
    *,
    error_code: str,
    status: str = "failed",
    amount: int = 99900,
    unique_suffix: str = "001",
) -> int:
    """Insert one payment_events row and return its id."""
    event_id = db_module.insert_payment_event({
        "razorpay_event_id": f"evt_pipe_{unique_suffix}",
        "payment_id": f"pay_pipe_{unique_suffix}",
        "subscription_id": f"sub_pipe_{unique_suffix}",
        "customer_id": "cust_pipe_001",
        "amount": amount,
        "currency": "INR",
        "status": status,
        "error_code": error_code,
        "error_description": f"{error_code} decline",
        "error_reason": error_code,
        "error_source": "bank",
        "error_step": "payment_authorization",
        "raw_payload": "{}",
    })
    assert event_id is not None, "Event insert returned None (duplicate?)"
    return event_id


def _mock_razorpay():
    """Return a mock Razorpay client that always succeeds on order.create."""
    client = MagicMock()
    client.order.create.return_value = {
        "id": "order_pipe_mock",
        "status": "created",
        "amount": 99900,
    }
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineHappyPath:

    def test_full_pipeline_returns_all_ids(self, test_db):
        """
        process_failed_payment on a valid failed event must populate
        classification_id, decision_id, and action_log_id in PipelineResult
        with result.error=None.
        """
        import recova.db as db_module
        from recova import execution
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(db_module, error_code="insufficient_funds", unique_suffix="allids")

        with patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            result = process_failed_payment(event_id)

        assert result.error is None, f"Unexpected pipeline error: {result.error}"
        assert result.event_id == event_id
        assert isinstance(result.classification_id, int) and result.classification_id > 0
        assert isinstance(result.decision_id, int) and result.decision_id > 0
        assert isinstance(result.action_log_id, int) and result.action_log_id > 0

    def test_pipeline_writes_full_audit_chain(self, test_db):
        """
        After process_failed_payment, all four tables must contain linked rows.
        FK chain: actions_log → decisions → classifications → payment_events.
        """
        import recova.db as db_module
        from recova import execution
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(db_module, error_code="card_blacklisted", unique_suffix="chain")

        with patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            result = process_failed_payment(event_id)

        assert result.error is None

        # classification links back to event
        cls_row = db_module.get_classification(result.classification_id)
        assert cls_row is not None
        assert cls_row["payment_event_id"] == event_id

        # decision links back to classification
        dec_row = db_module.get_decision(result.decision_id)
        assert dec_row is not None
        assert dec_row["classification_id"] == result.classification_id

        # actions_log links back to decision
        with db_module.get_connection() as conn:
            act_row = conn.execute(
                "SELECT * FROM actions_log WHERE id = ?", (result.action_log_id,)
            ).fetchone()
        assert act_row is not None
        assert act_row["decision_id"] == result.decision_id

    def test_soft_decline_gets_classification_soft(self, test_db):
        """A known soft error code must produce classification='soft'."""
        import recova.db as db_module
        from recova import execution
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(db_module, error_code="insufficient_funds", unique_suffix="soft_cls")

        with patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            result = process_failed_payment(event_id)

        cls_row = db_module.get_classification(result.classification_id)
        assert cls_row["classification"] == "soft"

    def test_hard_decline_gets_classification_hard(self, test_db):
        """A known hard error code must produce classification='hard'."""
        import recova.db as db_module
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(db_module, error_code="card_expired", unique_suffix="hard_cls")
        result = process_failed_payment(event_id)

        cls_row = db_module.get_classification(result.classification_id)
        assert cls_row["classification"] == "hard"

    def test_hard_decline_never_produces_retry_action(self, test_db):
        """
        CORE CORRECTNESS INVARIANT: no hard decline may ever produce a retry
        action. This test runs the full pipeline for every known hard code
        and asserts the action_type is never retry_now or retry_scheduled.
        """
        import recova.db as db_module
        from recova.pipeline import process_failed_payment

        hard_codes = [
            "card_blacklisted", "lost_card", "stolen_card", "pickup_card",
            "card_expired", "do_not_honor", "restricted_card", "invalid_card",
        ]
        retry_actions = {"retry_now", "retry_scheduled"}

        for i, code in enumerate(hard_codes):
            event_id = _insert_event(
                db_module, error_code=code, unique_suffix=f"inv_{i}"
            )
            result = process_failed_payment(event_id)
            assert result.error is None, f"Pipeline errored for code={code}: {result.error}"

            with db_module.get_connection() as conn:
                act = conn.execute(
                    "SELECT action_type FROM actions_log WHERE id = ?",
                    (result.action_log_id,),
                ).fetchone()

            assert act["action_type"] not in retry_actions, (
                f"INVARIANT VIOLATION: hard code '{code}' produced action "
                f"'{act['action_type']}'. Hard declines must never retry."
            )

    def test_pipeline_deduplicates_via_event_id(self, test_db):
        """
        Inserting the same razorpay_event_id twice via insert_payment_event
        returns None on the second call. This ensures the idempotency guard
        works at the DB layer (pipeline is not called twice for same event).
        """
        import recova.db as db_module

        event_data = {
            "razorpay_event_id": "evt_dedup_001",
            "payment_id": "pay_dedup_001",
            "subscription_id": "sub_dedup_001",
            "customer_id": "cust_dedup_001",
            "amount": 99900,
            "currency": "INR",
            "status": "failed",
            "error_code": "insufficient_funds",
            "error_description": "Insufficient funds.",
            "error_reason": "insufficient_funds",
            "error_source": "customer",
            "error_step": "payment_authorization",
            "raw_payload": "{}",
        }

        first_id = db_module.insert_payment_event(event_data)
        second_id = db_module.insert_payment_event(event_data)   # duplicate

        assert first_id is not None
        assert second_id is None, "Duplicate insert must return None (UNIQUE constraint)"


# ─────────────────────────────────────────────────────────────────────────────
# Error propagation — each stage must fail gracefully
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineErrorPropagation:

    def test_nonexistent_event_sets_error(self, test_db):
        """process_failed_payment on a missing event_id must set result.error."""
        from recova.pipeline import process_failed_payment

        result = process_failed_payment(event_id=999999)

        assert result.error is not None
        assert result.classification_id is None
        assert result.decision_id is None
        assert result.action_log_id is None

    def test_captured_status_sets_error(self, test_db):
        """
        A payment_event with status='captured' is not a failure — classify()
        returns None and the pipeline must set result.error without crashing.
        """
        import recova.db as db_module
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(
            db_module, error_code="", status="captured", unique_suffix="captured"
        )
        result = process_failed_payment(event_id)

        assert result.error is not None, (
            "A captured payment is not a failure and must not pass classify()"
        )
        assert result.classification_id is None

    def test_classifier_exception_is_caught(self, test_db):
        """
        If classifier.classify raises, result.error must capture the message
        and the pipeline must not propagate the exception to the caller.
        """
        import recova.db as db_module
        from recova import classifier
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(
            db_module, error_code="insufficient_funds", unique_suffix="cls_exc"
        )

        with patch.object(classifier, "classify", side_effect=RuntimeError("DB corruption")):
            result = process_failed_payment(event_id)

        assert result.error is not None
        assert "DB corruption" in result.error
        assert result.classification_id is None
        assert result.decision_id is None
        assert result.action_log_id is None

    def test_decision_engine_exception_is_caught(self, test_db):
        """
        If decision_engine.decide raises, result.error is set.
        classification_id must still be populated — classify() succeeded
        before the crash and its row is preserved in the DB.
        """
        import recova.db as db_module
        from recova import decision_engine
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(
            db_module, error_code="insufficient_funds", unique_suffix="de_exc"
        )

        with patch.object(decision_engine, "decide", side_effect=RuntimeError("rule crash")):
            result = process_failed_payment(event_id)

        assert result.error is not None
        assert "rule crash" in result.error
        assert result.classification_id is not None, (
            "classify() succeeded before the crash; classification_id must be set"
        )
        assert result.decision_id is None
        assert result.action_log_id is None

    def test_execution_exception_is_caught(self, test_db):
        """
        If execution.execute raises, result.error is set.
        Both classification_id and decision_id must be populated — the first
        two stages completed successfully before the crash.
        """
        import recova.db as db_module
        from recova import execution
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(
            db_module, error_code="insufficient_funds", unique_suffix="ex_exc"
        )

        with patch.object(execution, "execute", side_effect=RuntimeError("network down")):
            result = process_failed_payment(event_id)

        assert result.error is not None
        assert "network down" in result.error
        assert result.classification_id is not None
        assert result.decision_id is not None
        assert result.action_log_id is None

    def test_pipeline_error_does_not_corrupt_prior_rows(self, test_db):
        """
        When execution raises, the classification and decision rows written
        by the earlier stages must still be readable in the DB.
        """
        import recova.db as db_module
        from recova import execution
        from recova.pipeline import process_failed_payment

        event_id = _insert_event(
            db_module, error_code="insufficient_funds", unique_suffix="no_corrupt"
        )

        with patch.object(execution, "execute", side_effect=RuntimeError("crash")):
            result = process_failed_payment(event_id)

        # classification row must be intact
        cls_row = db_module.get_classification(result.classification_id)
        assert cls_row is not None
        assert cls_row["classification"] in ("soft", "hard", "unknown")

        # decision row must be intact
        dec_row = db_module.get_decision(result.decision_id)
        assert dec_row is not None
        assert dec_row["rule_triggered"] is not None
