"""
recova/decision_engine.py — Decision engine with fully explicit, traceable rules.

Rules are evaluated TOP-TO-BOTTOM. First match wins.
This precedence order is deliberate and documented:
  Rule 1 is checked first so that stale events never trigger a retry or
  reminder regardless of decline type — policy takes priority over recovery.

Every branch writes rule_triggered (short name) + rule_explanation
(full sentence) to the decisions table so any row is traceable to one rule.

retry_count is derived from db.get_retry_count() — the audit log is the
single source of truth; there is no separate mutable counter.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from recova import db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Top-of-file constants — change these to tune behaviour without touching logic
# ─────────────────────────────────────────────────────────────────────────────

MAX_RETRIES: int = 3
"""Maximum retry attempts per subscription before escalating."""

NO_CONTACT_AFTER_DAYS: int = 14
"""Abandon recovery if the original payment failure is older than this."""

RETRY_BACKOFF_HOURS: list[int] = [1, 24, 72]
"""
Retry delay schedule (hours) indexed by retry_count.
retry_count=0 → wait 1h, retry_count=1 → wait 24h, retry_count=2 → wait 72h.
Beyond MAX_RETRIES the engine escalates, not retries.
"""

LOW_RISK_RETRY_THRESHOLD_PAISE: int = 50_000
"""
₹500 in paise. Small-ticket subscription tiers under ₹500 are treated as
low-risk for an immediate retry because the cost of an extra declined attempt
is negligible relative to the recovery value (no auth fee, no customer impact
beyond one more bank request).
"""

# Codes that are permanently unrecoverable — must never be retried.
# These are a subset of hard declines; other hard declines get a reminder.
PERMANENTLY_UNRECOVERABLE_CODES: frozenset[str] = frozenset({
    "card_blacklisted",
    "lost_card",
    "stolen_card",
    "pickup_card",
})

# Hard-decline codes where the fix is user-driven (card update).
# Default for hard declines not in PERMANENTLY_UNRECOVERABLE_CODES.
HARD_DECLINE_DEFAULT_DECISION = "send_reminder"


# ─────────────────────────────────────────────────────────────────────────────
# Rule definitions (named tuples for readability in test assertions)
# ─────────────────────────────────────────────────────────────────────────────

RULES = {
    "rule_1_stale_stop": (
        "Too stale to recover; stop per NO_CONTACT_AFTER_DAYS={} day policy."
        .format(NO_CONTACT_AFTER_DAYS)
    ),
    "rule_2_hard_permanent_stop": (
        "Card permanently unusable ({}); further attempts pointless and potentially harmful."
        .format(", ".join(sorted(PERMANENTLY_UNRECOVERABLE_CODES)))
    ),
    "rule_3_hard_other_remind": (
        "Hard decline requires user action (card update/replacement); "
        "system cannot self-recover — send reminder."
    ),
    "rule_4_soft_max_retries_escalate": (
        "Soft decline but retry_count >= MAX_RETRIES={}; "
        "max retries exhausted — escalate to human/dunning flow."
        .format(MAX_RETRIES)
    ),
    "rule_5_soft_low_amount_retry_now": (
        "Soft decline, first attempt, amount <= ₹{:.0f}; "
        "low-risk immediate retry for small-ticket subscription."
        .format(LOW_RISK_RETRY_THRESHOLD_PAISE / 100)
    ),
    "rule_6_soft_retry_scheduled": (
        "Transient soft decline; schedule retry with exponential backoff "
        "(RETRY_BACKOFF_HOURS={}).".format(RETRY_BACKOFF_HOURS)
    ),
}


def decide(classification_id: int) -> Optional[int]:
    """
    Evaluate rules against a classification and write a decision to the DB.

    Returns the decision row id, or None if the classification is not found.
    """
    cls_row = db.get_classification(classification_id)
    if cls_row is None:
        logger.error("[decision] Classification %d not found", classification_id)
        return None

    # Fetch the originating payment event for context
    event = db.get_payment_event(cls_row["payment_event_id"])
    if event is None:
        logger.error("[decision] Payment event %d not found", cls_row["payment_event_id"])
        return None

    classification: str = cls_row["classification"]
    error_code: Optional[str] = cls_row["error_code"]
    amount: int = event["amount"] or 0
    subscription_id: Optional[str] = event["subscription_id"] or f"evt_{event['id']}"
    event_created_at: str = event["created_at"]

    # Derive retry count from the audit log (no separate counter column)
    retry_count: int = db.get_retry_count(subscription_id)

    # Parse event age
    event_age_days = _event_age_days(event_created_at)

    decision, rule_triggered, retry_at = _evaluate_rules(
        classification=classification,
        error_code=error_code,
        amount=amount,
        retry_count=retry_count,
        event_age_days=event_age_days,
    )

    rule_explanation = RULES[rule_triggered]

    decision_id = db.insert_decision({
        "classification_id": classification_id,
        "decision": decision,
        "retry_at": retry_at.isoformat() if retry_at else None,
        "rule_triggered": rule_triggered,
        "rule_explanation": rule_explanation,
    })

    logger.info(
        "[decision] classification=%d → %s via %s (retry_count=%d, age=%.1fd)",
        classification_id, decision, rule_triggered, retry_count, event_age_days,
    )
    return decision_id


def _evaluate_rules(
    classification: str,
    error_code: Optional[str],
    amount: int,
    retry_count: int,
    event_age_days: float,
) -> tuple[str, str, Optional[datetime]]:
    """
    Returns (decision, rule_triggered, retry_at).
    Rules evaluated in precedence order — first match wins.
    """

    # ── Rule 1: Stale event — checked first, unconditionally ─────────────────
    # A stale event must never trigger a retry or reminder regardless of
    # decline type. Policy takes priority over recovery attempt.
    if event_age_days > NO_CONTACT_AFTER_DAYS:
        return "stop", "rule_1_stale_stop", None

    # ── Rule 2: Permanently unrecoverable hard declines ───────────────────────
    # These codes mean the card will never work again; retrying is harmful.
    if classification == "hard" and error_code in PERMANENTLY_UNRECOVERABLE_CODES:
        return "stop", "rule_2_hard_permanent_stop", None

    # ── Rule 3: All other hard declines ───────────────────────────────────────
    # Card issue that requires the user to act (update card, replace card).
    # System cannot self-recover — send a reminder, never retry.
    if classification == "hard":
        return "send_reminder", "rule_3_hard_other_remind", None

    # ── Rules 4-6 apply only to soft declines ────────────────────────────────

    # ── Rule 4: Max retries exhausted ────────────────────────────────────────
    if retry_count >= MAX_RETRIES:
        return "escalate_promise_to_pay", "rule_4_soft_max_retries_escalate", None

    # ── Rule 5: Low-risk immediate retry (small amount, first attempt) ────────
    if retry_count == 0 and amount <= LOW_RISK_RETRY_THRESHOLD_PAISE:
        return "retry_now", "rule_5_soft_low_amount_retry_now", None

    # ── Rule 6: Scheduled retry with backoff (catch-all for remaining soft) ───
    backoff_index = min(retry_count, len(RETRY_BACKOFF_HOURS) - 1)
    delay_hours = RETRY_BACKOFF_HOURS[backoff_index]
    retry_at = datetime.now(timezone.utc) + timedelta(hours=delay_hours)
    return "retry_scheduled", "rule_6_soft_retry_scheduled", retry_at


def _event_age_days(created_at_str: str) -> float:
    """Return float days since event was created. Handles both tz-aware and naive strings."""
    try:
        # SQLite CURRENT_TIMESTAMP is naive UTC
        if created_at_str.endswith("Z") or "+" in created_at_str:
            dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(created_at_str).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return 0.0
