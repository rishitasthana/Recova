"""
recova/classifier.py — Classification layer.

Classifies a failed payment as 'hard' or 'soft' decline using a two-tier
lookup table:
  1. confirmed_table  — codes confirmed by running discover_decline_codes.py
                        against the live Razorpay test API.
  2. fallback_table   — industry-standard codes NOT yet confirmed against
                        live test mode. Marked clearly; treat with less trust.

Confidence levels:
  1.0  — code found in confirmed_table
  0.7  — code found in fallback_table, OR matched via error_reason substring
  0.5  — unknown code; defaults to 'soft' (never defaults to 'hard')

Defaulting unknown codes to 'soft' is intentional: a false 'hard' means
giving up on recoverable revenue, which is the worse failure mode for a
revenue-recovery system.
"""

import json
import os
import logging
from typing import Optional

from recova import db

logger = logging.getLogger(__name__)

DISCOVERED_CODES_PATH = os.getenv("DISCOVERED_CODES_PATH", "data/discovered_codes.json")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIRMED codes — populated at runtime from data/discovered_codes.json
# (output of scripts/discover_decline_codes.py).
# Keys are Razorpay error_code values; values are 'hard' or 'soft'.
# ─────────────────────────────────────────────────────────────────────────────
_CONFIRMED_HARD: set[str] = set()
_CONFIRMED_SOFT: set[str] = set()

# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK codes — not confirmed against live test mode.
# Drawn from Razorpay docs + standard card network decline reason codes.
# Used only when the confirmed table doesn't match.
# ─────────────────────────────────────────────────────────────────────────────
FALLBACK_HARD_CODES: set[str] = {
    # card_expired — card past its validity date; user must update
    "card_expired",
    # card_blacklisted — card flagged by issuer; cannot retry
    "card_blacklisted",
    # lost_card — reported lost; retry is harmful
    "lost_card",
    # stolen_card — reported stolen; retry is harmful
    "stolen_card",
    # do_not_honor — issuer blanket decline, often permanent
    "do_not_honor",
    # restricted_card — card has restrictions preventing this transaction type
    "restricted_card",
    # invalid_card — card number is invalid
    "invalid_card",
    # pickup_card — issuer instructed card pickup; cannot retry
    "pickup_card",
    # card_velocity_exceeded — fraud velocity limit; treat as hard
    "card_velocity_exceeded",
    # BAD_REQUEST_ERROR from Razorpay when card details are simply wrong
    "BAD_REQUEST_ERROR",
}

FALLBACK_SOFT_CODES: set[str] = {
    # insufficient_funds — account balance low; retriable after payday/reload
    "insufficient_funds",
    # issuer_unavailable — bank system temporarily down
    "issuer_unavailable",
    # payment_timeout — network or bank timeout; retriable
    "payment_timeout",
    # gateway_error — payment gateway error; retriable
    "gateway_error",
    # bank_timeout — bank response timed out; retriable
    "bank_timeout",
    # network_error — generic network issue; retriable
    "network_error",
    # transaction_not_permitted — sometimes issuer policy, sometimes transient
    "transaction_not_permitted",
    # GATEWAY_ERROR from Razorpay — gateway-side transient error
    "GATEWAY_ERROR",
    # SERVER_ERROR from Razorpay — Razorpay internal transient error
    "SERVER_ERROR",
}

# ─────────────────────────────────────────────────────────────────────────────
# Reason substring matching — for when error_code is absent or generic.
# These match against error_reason (lowercase). First match wins.
# ─────────────────────────────────────────────────────────────────────────────
REASON_HARD_SUBSTRINGS: list[str] = [
    "expired", "blacklist", "lost", "stolen", "invalid card",
    "do not honor", "restricted", "pickup",
]

REASON_SOFT_SUBSTRINGS: list[str] = [
    "insufficient", "funds", "timeout", "unavailable",
    "gateway error", "network", "try again", "temporary",
]

# Hard-decline codes that should NEVER trigger a retry — correctness invariant
PERMANENTLY_UNRECOVERABLE_CODES: set[str] = {
    "card_blacklisted", "lost_card", "stolen_card", "pickup_card",
}


def _load_confirmed_codes() -> None:
    """Load confirmed decline codes from discover_decline_codes.py output."""
    global _CONFIRMED_HARD, _CONFIRMED_SOFT
    if not os.path.exists(DISCOVERED_CODES_PATH):
        logger.warning(
            "[classifier] %s not found — using fallback table only. "
            "Run scripts/discover_decline_codes.py first.",
            DISCOVERED_CODES_PATH,
        )
        return

    with open(DISCOVERED_CODES_PATH, "r") as f:
        data = json.load(f)

    confirmed = data.get("confirmed_codes", {})
    _CONFIRMED_HARD = set(confirmed.get("hard", []))
    _CONFIRMED_SOFT = set(confirmed.get("soft", []))
    logger.info(
        "[classifier] Loaded confirmed codes: %d hard, %d soft",
        len(_CONFIRMED_HARD), len(_CONFIRMED_SOFT),
    )


# Load confirmed codes on module import
_load_confirmed_codes()


def classify(event_id: int) -> Optional[int]:
    """
    Classify a payment_event by its decline type and write to classifications.

    Returns the classification row id, or None if the event is not found
    or is not a failed payment.
    """
    event = db.get_payment_event(event_id)
    if event is None:
        logger.error("[classifier] Event %d not found", event_id)
        return None

    if event["status"] != "failed":
        logger.info("[classifier] Event %d is not a failure (status=%s) — skip", event_id, event["status"])
        return None

    error_code: Optional[str] = event["error_code"]
    error_reason: Optional[str] = (event["error_reason"] or "").lower()

    classification, confidence, reason, lookup_source = _lookup(error_code, error_reason)

    row_id = db.insert_classification({
        "payment_event_id": event_id,
        "classification": classification,
        "confidence": confidence,
        "reason": reason,
        "error_code": error_code,
        "error_reason": event["error_reason"],
        "lookup_source": lookup_source,
    })

    logger.info(
        "[classifier] event=%d code=%s → %s (confidence=%.1f, source=%s)",
        event_id, error_code, classification, confidence, lookup_source,
    )
    return row_id


def _lookup(
    error_code: Optional[str],
    error_reason_lower: str,
) -> tuple[str, float, str, str]:
    """
    Returns (classification, confidence, reason, lookup_source).

    Lookup order:
      1. Confirmed hard table   → hard, 1.0
      2. Confirmed soft table   → soft, 1.0
      3. Fallback hard table    → hard, 0.7
      4. Fallback soft table    → soft, 0.7
      5. Reason substring hard  → hard, 0.7
      6. Reason substring soft  → soft, 0.7
      7. Default                → soft, 0.5  (never defaults to hard)
    """
    if error_code:
        if error_code in _CONFIRMED_HARD:
            return ("hard", 1.0,
                    f"error_code '{error_code}' found in confirmed hard-decline table",
                    "confirmed_table")
        if error_code in _CONFIRMED_SOFT:
            return ("soft", 1.0,
                    f"error_code '{error_code}' found in confirmed soft-decline table",
                    "confirmed_table")
        if error_code in FALLBACK_HARD_CODES:
            return ("hard", 0.7,
                    f"error_code '{error_code}' found in fallback hard-decline table (not confirmed against live test mode)",
                    "fallback_table")
        if error_code in FALLBACK_SOFT_CODES:
            return ("soft", 0.7,
                    f"error_code '{error_code}' found in fallback soft-decline table (not confirmed against live test mode)",
                    "fallback_table")

    # Substring match on error_reason
    if error_reason_lower:
        for substr in REASON_HARD_SUBSTRINGS:
            if substr in error_reason_lower:
                return ("hard", 0.7,
                        f"error_reason contains '{substr}' → classified as hard decline",
                        "reason_substring")
        for substr in REASON_SOFT_SUBSTRINGS:
            if substr in error_reason_lower:
                return ("soft", 0.7,
                        f"error_reason contains '{substr}' → classified as soft decline",
                        "reason_substring")

    # Unknown — default to soft (see module docstring for rationale)
    return ("soft", 0.5,
            f"error_code '{error_code}' not in any lookup table; defaulted to soft",
            "default")


def reload_confirmed_codes() -> None:
    """Re-read discovered_codes.json (useful after running Step 0 mid-session)."""
    _load_confirmed_codes()
