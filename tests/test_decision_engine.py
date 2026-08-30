"""
tests/test_decision_engine.py — Unit tests for the decision engine.

Tests cover all 6 rules, precedence order, and the critical correctness
invariant: no hard decline may ever produce a retry action.

Rule precedence (first match wins — tested explicitly):
  1. Stale event → stop  (must fire even if decline is soft, or permanently unrecoverable hard)
  2. Hard + permanently unrecoverable → stop
  3. Hard + other → send_reminder
  4. Soft + retry_count >= MAX_RETRIES → escalate_promise_to_pay
  5. Soft + retry_count == 0 + low amount → retry_now
  6. Soft + retry_count < MAX_RETRIES (all other) → retry_scheduled
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from recova.decision_engine import (
    MAX_RETRIES,
    LOW_RISK_RETRY_THRESHOLD_PAISE,
    NO_CONTACT_AFTER_DAYS,
    PERMANENTLY_UNRECOVERABLE_CODES,
    _evaluate_rules,
)


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1: Stale events always stop
# ─────────────────────────────────────────────────────────────────────────────

class TestRule1Staleness:

    def test_stale_soft_event_stops(self):
        """A stale soft decline must stop — policy beats recovery."""
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=0,
            event_age_days=NO_CONTACT_AFTER_DAYS + 1,
        )
        assert decision == "stop"
        assert rule == "rule_1_stale_stop"

    def test_stale_hard_event_stops_via_rule1_not_rule2(self):
        """
        A stale hard decline must trigger rule_1 (staleness), not rule_2.
        Rule precedence: rule_1 fires first regardless of classification.
        """
        decision, rule, _ = _evaluate_rules(
            classification="hard",
            error_code="card_blacklisted",
            amount=99900,
            retry_count=0,
            event_age_days=NO_CONTACT_AFTER_DAYS + 5,
        )
        assert decision == "stop"
        assert rule == "rule_1_stale_stop", (
            f"Expected rule_1_stale_stop (staleness takes precedence), got {rule}"
        )

    def test_just_under_threshold_does_not_stop(self):
        """An event exactly at NO_CONTACT_AFTER_DAYS days old must NOT trigger rule 1."""
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=0,
            event_age_days=NO_CONTACT_AFTER_DAYS - 0.01,
        )
        assert rule != "rule_1_stale_stop"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 2: Permanently unrecoverable hard declines → stop
# ─────────────────────────────────────────────────────────────────────────────

class TestRule2PermanentHardStop:

    @pytest.mark.parametrize("code", list(PERMANENTLY_UNRECOVERABLE_CODES))
    def test_permanently_unrecoverable_stops(self, code):
        decision, rule, _ = _evaluate_rules(
            classification="hard",
            error_code=code,
            amount=99900,
            retry_count=0,
            event_age_days=1,
        )
        assert decision == "stop"
        assert rule == "rule_2_hard_permanent_stop"

    def test_none_code_hard_does_not_trigger_rule2(self):
        """Hard decline with no error_code should not trigger rule_2 (None not in the set)."""
        decision, rule, _ = _evaluate_rules(
            classification="hard",
            error_code=None,
            amount=99900,
            retry_count=0,
            event_age_days=1,
        )
        assert rule != "rule_2_hard_permanent_stop"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 3: Other hard declines → send_reminder
# ─────────────────────────────────────────────────────────────────────────────

class TestRule3HardReminder:

    def test_card_expired_sends_reminder(self):
        decision, rule, _ = _evaluate_rules(
            classification="hard",
            error_code="card_expired",
            amount=99900,
            retry_count=0,
            event_age_days=1,
        )
        assert decision == "send_reminder"
        assert rule == "rule_3_hard_other_remind"

    def test_do_not_honor_sends_reminder(self):
        decision, rule, _ = _evaluate_rules(
            classification="hard",
            error_code="do_not_honor",
            amount=99900,
            retry_count=0,
            event_age_days=1,
        )
        assert decision == "send_reminder"
        assert rule == "rule_3_hard_other_remind"

    def test_hard_never_produces_retry(self):
        """
        CORE CORRECTNESS INVARIANT:
        No hard decline, for any error code or retry count, must ever produce
        retry_now or retry_scheduled.
        """
        retry_decisions = {"retry_now", "retry_scheduled"}
        for code in list(PERMANENTLY_UNRECOVERABLE_CODES) + [
            "card_expired", "do_not_honor", "restricted_card", "invalid_card", None,
        ]:
            for retry_count in range(MAX_RETRIES + 2):
                decision, rule, _ = _evaluate_rules(
                    classification="hard",
                    error_code=code,
                    amount=99900,
                    retry_count=retry_count,
                    event_age_days=1,
                )
                assert decision not in retry_decisions, (
                    f"INVARIANT VIOLATION: hard decline with code='{code}', "
                    f"retry_count={retry_count} produced '{decision}'. "
                    f"Hard declines must never retry."
                )


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4: Soft + max retries exhausted → escalate
# ─────────────────────────────────────────────────────────────────────────────

class TestRule4MaxRetriesEscalate:

    def test_max_retries_escalates(self):
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=MAX_RETRIES,
            event_age_days=1,
        )
        assert decision == "escalate_promise_to_pay"
        assert rule == "rule_4_soft_max_retries_escalate"

    def test_beyond_max_retries_escalates(self):
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=MAX_RETRIES + 5,
            event_age_days=1,
        )
        assert decision == "escalate_promise_to_pay"
        assert rule == "rule_4_soft_max_retries_escalate"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 5: Soft + first attempt + low amount → retry_now
# ─────────────────────────────────────────────────────────────────────────────

class TestRule5LowRiskRetryNow:

    def test_low_amount_first_attempt_retries_now(self):
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=LOW_RISK_RETRY_THRESHOLD_PAISE,  # exactly at threshold
            retry_count=0,
            event_age_days=1,
        )
        assert decision == "retry_now"
        assert rule == "rule_5_soft_low_amount_retry_now"

    def test_above_threshold_does_not_trigger_rule5(self):
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=LOW_RISK_RETRY_THRESHOLD_PAISE + 1,
            retry_count=0,
            event_age_days=1,
        )
        assert rule != "rule_5_soft_low_amount_retry_now"

    def test_second_attempt_does_not_trigger_rule5(self):
        """Rule 5 only fires on retry_count==0."""
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=LOW_RISK_RETRY_THRESHOLD_PAISE,
            retry_count=1,
            event_age_days=1,
        )
        assert rule != "rule_5_soft_low_amount_retry_now"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 6: Soft + remaining retries → retry_scheduled
# ─────────────────────────────────────────────────────────────────────────────

class TestRule6ScheduledRetry:

    def test_high_amount_soft_schedules_retry(self):
        decision, rule, retry_at = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=LOW_RISK_RETRY_THRESHOLD_PAISE + 1,
            retry_count=0,
            event_age_days=1,
        )
        assert decision == "retry_scheduled"
        assert rule == "rule_6_soft_retry_scheduled"
        assert retry_at is not None

    def test_second_attempt_soft_schedules_retry(self):
        decision, rule, retry_at = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=1,
            event_age_days=1,
        )
        assert decision == "retry_scheduled"
        assert rule == "rule_6_soft_retry_scheduled"
        assert retry_at is not None

    def test_retry_at_is_future(self):
        from datetime import datetime, timezone
        _, _, retry_at = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=0,
            event_age_days=1,
        )
        # retry_now case — retry_at is None, that's fine
        # For the scheduled case:
        _, _, retry_at = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=LOW_RISK_RETRY_THRESHOLD_PAISE + 1,
            retry_count=0,
            event_age_days=1,
        )
        assert retry_at is not None
        assert retry_at > datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Precedence ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestRulePrecedence:

    def test_stale_beats_permanently_unrecoverable(self):
        """
        A stale + permanently unrecoverable hard event must hit rule_1,
        not rule_2. Staleness check is first.
        """
        decision, rule, _ = _evaluate_rules(
            classification="hard",
            error_code="card_blacklisted",
            amount=99900,
            retry_count=0,
            event_age_days=NO_CONTACT_AFTER_DAYS + 1,
        )
        assert rule == "rule_1_stale_stop"

    def test_stale_beats_max_retries(self):
        """A stale + exhausted-retries soft event must hit rule_1, not rule_4."""
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=99900,
            retry_count=MAX_RETRIES,
            event_age_days=NO_CONTACT_AFTER_DAYS + 1,
        )
        assert rule == "rule_1_stale_stop"

    def test_max_retries_beats_low_amount_retry_now(self):
        """Exhausted retries (rule_4) must beat low-amount retry_now (rule_5)."""
        decision, rule, _ = _evaluate_rules(
            classification="soft",
            error_code="insufficient_funds",
            amount=LOW_RISK_RETRY_THRESHOLD_PAISE,
            retry_count=MAX_RETRIES,
            event_age_days=1,
        )
        assert rule == "rule_4_soft_max_retries_escalate"
