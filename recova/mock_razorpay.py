"""
recova/mock_razorpay.py — Mock Razorpay client for offline/demo operation.

All decline reasons are sourced verbatim from Razorpay's public documentation:
  https://razorpay.com/docs/payment-gateway/rainy-day/errors/error-reasons/

This is the SINGLE SOURCE OF TRUTH for decline code classification.
classifier.py imports directly from here to avoid duplicating the list.

Why a mock layer?
  Razorpay Test Mode requires PAN/KYC verification — not always available during
  a buildathon. This module lets the full pipeline run end-to-end with zero live
  API calls while still demonstrating real integration architecture.

  USE_MOCK_RAZORPAY=false in .env to switch to the real Razorpay API.

Retry success probability (MOCK_RETRY_SUCCESS_RATE):
  Set to 80% — a reasonable assumption for a transient soft decline that has
  had some time to resolve (insufficient funds after payday, bank back online).
  This probability is an assumption, not a measured value. Documented here so
  the dashboard's recovery rate is clearly attributable to the simulation model.

Webhook payload shape:
  build_webhook_payload() produces the exact JSON structure that api/main.py's
  _extract_event_data() parser reads. Key field paths:
    data["event"]                               → event type
    data["payload"]["id"]                       → razorpay_event_id
    data["payload"]["payment"]["entity"]["id"]  → payment_id
    data["payload"]["payment"]["entity"]["subscription_id"]  → subscription_id (direct)
    data["payload"]["payment"]["entity"]["error_reason"]     → error_reason (discriminator)
    data["payload"]["payment"]["entity"]["error_code"]       → error_code (coarse bucket)
"""

import os
import random
import uuid
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# Real Razorpay decline taxonomy
# Source: https://razorpay.com/docs/payment-gateway/rainy-day/errors/error-reasons/
# ─────────────────────────────────────────────────────────────────────────────

HARD_DECLINE_REASONS: dict[str, str] = {
    # Permanent problems — retrying without customer action will not help.
    "card_expired":
        "The customer is making the payment with an expired card.",
    "card_number_invalid":
        "The customer has entered an incorrect card number which is not part of any BIN/IIN.",
    "card_type_invalid":
        "The customer is using an unsupported card type to complete the payment.",
    "card_not_enrolled":
        "Card holder has not registered for 3D secure service.",
    "debit_instrument_blocked":
        "The customer is using a blocked card. The card could have been blocked by the "
        "issuer or by the customer themselves.",
    "debit_instrument_inactive":
        "The customer is using an inactive or frozen card.",
    "card_declined":
        "Issuer bank declined the card due to checks at their end; exact reason not "
        "shared with Razorpay.",
    "payment_declined":
        "Issuer bank or gateway declined the payment for business or technical reasons "
        "not communicated to Razorpay.",
    "invalid_vpa":
        "The customer is using an invalid or unregistered VPA.",
    "merchant_not_activated":
        "Merchant is not activated with the gateway.",
}

SOFT_DECLINE_REASONS: dict[str, str] = {
    # Temporary problems — a scheduled retry has a real chance of succeeding.
    "insufficient_funds":
        "The customer does not have sufficient funds in the account to complete the payment.",
    "bank_not_available":
        "Bank is not available due to a downtime or a technical issue.",
    "bank_technical_error":
        "The issuing bank was facing technical problems at the moment the payment was attempted.",
    "gateway_technical_error":
        "Payment failed due to a technical error at the gateway.",
    "issuer_technical_error":
        "A technical error at the issuer.",
    "server_error":
        "Technical error at Razorpay's server.",
    "payment_timed_out":
        "The customer did not complete the transaction within the specified time.",
    "request_timed_out":
        "The request timed out.",
    "bank_cutoff_in_progress":
        "Bank CBS cutoff is in progress — a periodic event at the bank's end.",
    "authentication_failed":
        "3D secure or OTP authentication failed; customer may have cancelled or mistyped.",
    "transaction_daily_limit_exceeded":
        "The customer has exceeded the daily transaction limit set on the card.",
    "payment_declined_due_to_high_traffic":
        "Occurs during high-TPS scenarios where the bank is unable to serve requests.",
}

# Codes where even a reminder won't help — the underlying instrument is permanently
# unusable without bank/customer intervention beyond just updating a card.
# Used by decision_engine.py rule_2 to determine when to stop vs. send_reminder.
PERMANENTLY_UNRECOVERABLE_CODES: frozenset[str] = frozenset({
    "debit_instrument_blocked",    # blocked by bank — user must call issuer
    "debit_instrument_inactive",   # frozen card — must be reactivated
    "merchant_not_activated",      # gateway config issue — not customer-solvable
})


# ─────────────────────────────────────────────────────────────────────────────
# Scenarios — used by batch_runner and simulate_payments in mock mode
#
# Weights produce a realistic payment mix:
#   ~60% success (payment.captured — recorded but no pipeline)
#   ~25% soft decline (retriable)
#   ~15% hard decline (permanent)
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS: list[dict] = [
    # ── Successful payments (weight 60 total) ────────────────────────────────
    {"label": "success",                 "outcome": "success",
     "weight": 60},

    # ── Soft declines (weight 25 total) ──────────────────────────────────────
    {"label": "soft_insufficient_funds", "outcome": "failed",
     "reason": "insufficient_funds",
     "source": "customer", "step": "payment_authorization",
     "amount_paise": 99900,
     "weight": 10},

    {"label": "soft_bank_technical_error", "outcome": "failed",
     "reason": "bank_technical_error",
     "source": "bank", "step": "payment_authorization",
     "amount_paise": 99900,
     "weight": 6},

    {"label": "soft_gateway_technical_error", "outcome": "failed",
     "reason": "gateway_technical_error",
     "source": "gateway", "step": "payment_authorization",
     "amount_paise": 49900,   # ₹499 — qualifies for retry_now (≤ ₹500)
     "weight": 5},

    {"label": "soft_authentication_failed", "outcome": "failed",
     "reason": "authentication_failed",
     "source": "customer", "step": "payment_authentication",
     "amount_paise": 29900,   # ₹299 — qualifies for retry_now
     "weight": 4},

    # ── Hard declines (weight 15 total) ──────────────────────────────────────
    {"label": "hard_card_expired",       "outcome": "failed",
     "reason": "card_expired",
     "source": "customer", "step": "payment_authorization",
     "amount_paise": 99900,
     "weight": 6},

    {"label": "hard_debit_instrument_blocked", "outcome": "failed",
     "reason": "debit_instrument_blocked",
     "source": "issuer", "step": "payment_authorization",
     "amount_paise": 99900,
     "weight": 5},

    {"label": "hard_card_declined",      "outcome": "failed",
     "reason": "card_declined",
     "source": "issuer", "step": "payment_authorization",
     "amount_paise": 99900,
     "weight": 4},
]

# Build a weighted flat list (used by random.choice() in batch_runner)
WEIGHTED_SCENARIOS: list[dict] = []
for _s in SCENARIOS:
    WEIGHTED_SCENARIOS.extend([_s] * _s["weight"])


# ─────────────────────────────────────────────────────────────────────────────
# Mock retry success probability
# ─────────────────────────────────────────────────────────────────────────────

MOCK_RETRY_SUCCESS_RATE: float = 0.80
"""
80% of mock retries succeed.

Assumption: a soft decline (insufficient funds, bank timeout, gateway error)
that is being retried after some backoff is likely resolved. 80% is a
conservative estimate used for simulation only; real rates vary by reason code.

This probability is documented here and in the README so dashboard recovery
rates are clearly attributable to the simulation model rather than live data.
"""


# ─────────────────────────────────────────────────────────────────────────────
# ID generation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hex(n: int = 12) -> str:
    """Short random hex suffix for mock IDs."""
    return uuid.uuid4().hex[:n]


def mock_payment_id() -> str:
    return f"pay_mock_{_hex()}"


def mock_order_id() -> str:
    return f"order_mock_{_hex()}"


def mock_event_id(payment_id: str) -> str:
    return f"evt_mock_{payment_id[9:]}"  # strip "pay_mock_" prefix


def mock_subscription_id(index: int) -> str:
    return f"sub_mock_{index:03d}"


def mock_customer_id(index: int) -> str:
    return f"cust_mock_{index:03d}"


# ─────────────────────────────────────────────────────────────────────────────
# Payload builders
# ─────────────────────────────────────────────────────────────────────────────

def build_error_object(scenario: dict) -> dict:
    """
    Build a Razorpay-shaped error object for a failed-payment scenario.

    Razorpay uses two coarse error_code buckets:
      BAD_REQUEST_ERROR — customer-originating errors (wrong details, expired card)
      GATEWAY_ERROR     — bank/gateway-originating transient errors

    The granular error is always in error_reason.
    """
    reason = scenario["reason"]
    source = scenario.get("source", "customer")

    is_hard = reason in HARD_DECLINE_REASONS
    description = (
        HARD_DECLINE_REASONS[reason] if is_hard
        else SOFT_DECLINE_REASONS.get(reason, f"Payment declined: {reason}")
    )

    error_code = "BAD_REQUEST_ERROR" if source == "customer" else "GATEWAY_ERROR"

    return {
        "code":        error_code,
        "description": description,
        "field":       None,
        "source":      source,
        "step":        scenario.get("step", "payment_authorization"),
        "reason":      reason,
        "metadata":    {},
    }


def build_webhook_payload(
    scenario: dict,
    payment_id: str,
    order_id: str,
    subscription_id: str,
    amount: int,
) -> dict:
    """
    Build a full payment.failed or payment.captured webhook payload that matches
    the exact field paths read by api/main.py's _extract_event_data():

      data["event"]                                        → event type
      data["payload"]["id"]                                → razorpay_event_id
      data["payload"]["payment"]["entity"]["id"]           → payment_id
      data["payload"]["payment"]["entity"]["subscription_id"] → subscription_id
      data["payload"]["payment"]["entity"]["error_reason"] → error_reason
      data["payload"]["payment"]["entity"]["error_code"]   → error_code
    """
    event_type = (
        "payment.captured" if scenario["outcome"] == "success"
        else "payment.failed"
    )

    entity: dict = {
        "id":              payment_id,
        "order_id":        order_id,
        "subscription_id": subscription_id,   # direct field — not nested in notes
        "amount":          amount,
        "currency":        "INR",
        "status":          "captured" if scenario["outcome"] == "success" else "failed",
    }

    if scenario["outcome"] != "success":
        error = build_error_object(scenario)
        error["metadata"] = {"payment_id": payment_id, "order_id": order_id}
        entity.update({
            "error_code":        error["code"],
            "error_description": error["description"],
            "error_source":      error["source"],
            "error_step":        error["step"],
            "error_reason":      error["reason"],
        })

    return {
        "event": event_type,
        "payload": {
            "id": mock_event_id(payment_id),
            "payment": {
                "entity": entity,
            },
        },
    }


def create_mock_order(
    amount: int,
    currency: str,
    receipt: str,
    notes: dict,
) -> dict:
    """
    Return a fake Razorpay order object with realistic field shape.
    Used by execution.py in mock mode for retry_now / retry_scheduled actions.

    80% of calls succeed (MOCK_RETRY_SUCCESS_RATE); 20% simulate an API error
    by raising an exception, so the execution layer's api_error path is exercised.
    """
    if random.random() > MOCK_RETRY_SUCCESS_RATE:
        raise RuntimeError(
            f"[mock] Simulated Razorpay API error for receipt {receipt} "
            f"(20% random failure to exercise api_error path)"
        )

    order_id = mock_order_id()
    return {
        "id":         order_id,
        "entity":     "order",
        "amount":     amount,
        "currency":   currency,
        "receipt":    receipt,
        "status":     "created",
        "notes":      notes,
        "created_at": int(datetime.now(timezone.utc).timestamp()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Mock discover: write discovered_codes.json from public docs
# ─────────────────────────────────────────────────────────────────────────────

def write_discovered_codes_from_public_docs(output_path: str) -> None:
    """
    Write data/discovered_codes.json directly from the documented taxonomy
    (no live API call). Tagged lookup_source='razorpay_public_docs' — honest
    audit trail; these codes were NOT confirmed against the live test API.

    Called by scripts/discover_decline_codes.py when USE_MOCK_RAZORPAY=true.
    """
    import json
    import time
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "razorpay_public_docs",
        "source_url": "https://razorpay.com/docs/payment-gateway/rainy-day/errors/error-reasons/",
        "note": (
            "Generated by recova/mock_razorpay.py from Razorpay's public documentation. "
            "These codes were NOT confirmed against the live test API "
            "(USE_MOCK_RAZORPAY=true). Set USE_MOCK_RAZORPAY=false and run "
            "scripts/discover_decline_codes.py with real credentials to get "
            "live-confirmed codes."
        ),
        "confirmed_codes": {
            "hard": sorted(HARD_DECLINE_REASONS.keys()),
            "soft": sorted(SOFT_DECLINE_REASONS.keys()),
            "ambiguous": [],
        },
        "raw_probe_results": [],
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Mock seed: generate seed_data.json without live API calls
# ─────────────────────────────────────────────────────────────────────────────

MOCK_CUSTOMERS: list[dict] = [
    {"name": "Priya Sharma",  "email": "priya.sharma@recova-demo.com",  "contact": "9876500001"},
    {"name": "Rahul Verma",   "email": "rahul.verma@recova-demo.com",   "contact": "9876500002"},
    {"name": "Ananya Singh",  "email": "ananya.singh@recova-demo.com",  "contact": "9876500003"},
    {"name": "Karan Mehta",   "email": "karan.mehta@recova-demo.com",   "contact": "9876500004"},
    {"name": "Deepa Nair",    "email": "deepa.nair@recova-demo.com",    "contact": "9876500005"},
]

MOCK_PLAN = {
    "id":          "plan_mock_recova_001",
    "name":        "ReCova Pro — Monthly",
    "amount_paise": 99900,
    "period":      "monthly",
    "interval":    1,
}


def write_seed_data(output_path: str) -> None:
    """
    Write data/seed_data.json with deterministic mock IDs — no Razorpay API calls.
    Called by scripts/seed_razorpay.py when USE_MOCK_RAZORPAY=true.
    """
    import json
    import time
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    subscriptions = []
    for i, customer in enumerate(MOCK_CUSTOMERS, start=1):
        subscriptions.append({
            "subscription_id": mock_subscription_id(i),
            "customer_id":     mock_customer_id(i),
            "customer_name":   customer["name"],
            "customer_email":  customer["email"],
            "plan_id":         MOCK_PLAN["id"],
            "status":          "active",
        })

    seed_data = {
        "created_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mock_mode":     True,
        "plan":          MOCK_PLAN,
        "customers": [
            {
                "id":    mock_customer_id(i + 1),
                "name":  c["name"],
                "email": c["email"],
            }
            for i, c in enumerate(MOCK_CUSTOMERS)
        ],
        "subscriptions": subscriptions,
    }

    with open(output_path, "w") as f:
        json.dump(seed_data, f, indent=2)
