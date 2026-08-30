"""
scripts/discover_decline_codes.py — STEP 0

Probe the real Razorpay test API to discover which error_code, error_reason,
error_source, and error_description fields are actually returned for each
test card number.

HOW IT WORKS:
  1. Creates a small Razorpay order (₹100) in test mode.
  2. Attempts payment using each test card number from the current
     Razorpay docs (https://razorpay.com/docs/payments/test-card-upi-details/).
  3. Captures the actual error fields from the response.
  4. Writes the full mapping to data/discovered_codes.json.

WHY THIS COMES FIRST:
  The classifier in recova/classifier.py is built from the output of this
  script, not from assumed mappings. If a test card returns a different code
  than expected, we catch it here instead of discovering it in production.

USAGE:
  python scripts/discover_decline_codes.py

OUTPUT:
  data/discovered_codes.json  — full card-to-error mapping + confirmed_codes
                                 section consumed by classifier.py

NOTE:
  Razorpay's test API requires that payment attempts go through the payment
  object API. The card payment object must be created and confirmed in test mode.
  This script uses the Razorpay Orders + Payments API (not the hosted checkout).
  Some test cards may require an OTP step; this script handles the standard
  test OTP (000000 or 1234) automatically.
"""

import json
import os
import sys
import time
import logging
from pathlib import Path

import razorpay
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
OUTPUT_PATH = os.getenv("DISCOVERED_CODES_PATH", "data/discovered_codes.json")

if not KEY_ID or not KEY_SECRET:
    sys.exit(
        "ERROR: RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set in .env\n"
        "Copy .env.example to .env and fill in your Razorpay test-mode credentials."
    )

client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))

# ─────────────────────────────────────────────────────────────────────────────
# Test card inventory — from Razorpay test-mode docs.
# Labels indicate the expected behaviour; actual error codes are what we capture.
# https://razorpay.com/docs/payments/test-card-upi-details/
# ─────────────────────────────────────────────────────────────────────────────
TEST_CARDS = [
    {
        "label": "success",
        "expected_type": "success",
        "number": "4111111111111111",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
    {
        "label": "insufficient_funds",
        "expected_type": "soft",
        "number": "4000000000009995",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
    {
        "label": "card_declined_generic",
        "expected_type": "soft",
        "number": "4000000000000002",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
    {
        "label": "card_declined_do_not_honor",
        "expected_type": "hard",
        "number": "4000000000000069",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
    {
        "label": "incorrect_cvc",
        "expected_type": "hard",
        "number": "4000000000000101",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "999",
        "name": "Test User",
    },
    {
        "label": "card_velocity_exceeded",
        "expected_type": "hard",
        "number": "4000000000006975",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
    {
        "label": "processing_error",
        "expected_type": "soft",
        "number": "4000000000000119",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
    {
        "label": "network_error",
        "expected_type": "soft",
        "number": "4000000000009987",
        "expiry_month": "12",
        "expiry_year": "2030",
        "cvv": "123",
        "name": "Test User",
    },
]

# Standard Razorpay test OTP
TEST_OTP = "000000"
TEST_AMOUNT = 10000  # ₹100 in paise — small enough to not matter


def create_test_order() -> dict:
    """Create a ₹100 order in test mode."""
    order = client.order.create(data={
        "amount": TEST_AMOUNT,
        "currency": "INR",
        "receipt": f"discover_{int(time.time())}",
    })
    logger.info("Created order: %s", order["id"])
    return order


def attempt_payment(order: dict, card: dict) -> dict:
    """
    Attempt a card payment via Razorpay's payment API (S2S / direct API flow).
    Returns the raw response dict including any error fields.
    """
    base_url = "https://api.razorpay.com/v1"
    auth = (KEY_ID, KEY_SECRET)

    # Step 1: Create a payment
    payment_payload = {
        "amount": order["amount"],
        "currency": order["currency"],
        "order_id": order["id"],
        "email": "test@recova-demo.com",
        "contact": "9876543210",
        "method": "card",
        "card[number]": card["number"],
        "card[name]": card["name"],
        "card[expiry_month]": card["expiry_month"],
        "card[expiry_year]": card["expiry_year"],
        "card[cvv]": card["cvv"],
    }

    with httpx.Client(auth=auth, timeout=30.0) as http:
        resp = http.post(f"{base_url}/payments/create/ajax", data=payment_payload)
        resp_data = resp.json()
        logger.debug("Payment create response: %s", resp_data)

        # Handle OTP/3DS challenge if needed
        if resp.status_code == 200 and resp_data.get("type") == "otp":
            payment_id = resp_data.get("payment_id") or resp_data.get("razorpay_payment_id")
            next_url = resp_data.get("next", {})
            if isinstance(next_url, dict):
                submit_url = next_url.get("action", f"{base_url}/payments/{payment_id}/otp/generate")
            else:
                submit_url = f"{base_url}/payments/{payment_id}/otp/verify"

            otp_resp = http.post(submit_url, data={
                "otp": TEST_OTP,
                "payment_id": payment_id,
            })
            resp_data = otp_resp.json()
            logger.debug("OTP response: %s", resp_data)

        # Fetch the final payment object to get error fields
        if "razorpay_payment_id" in resp_data or "payment_id" in resp_data:
            pid = resp_data.get("razorpay_payment_id") or resp_data.get("payment_id")
            try:
                final = http.get(f"{base_url}/payments/{pid}", auth=auth)
                return final.json()
            except Exception as e:
                logger.warning("Could not fetch final payment object: %s", e)
                return resp_data

    return resp_data


def probe_card(card: dict) -> dict:
    """Run one card through create-order → attempt-payment and return captured fields."""
    logger.info("Probing card: %s (%s)", card["label"], card["number"][-4:])
    try:
        order = create_test_order()
        time.sleep(0.5)  # be gentle with the test API
        response = attempt_payment(order, card)
        return {
            "card_label": card["label"],
            "card_number_last4": card["number"][-4:],
            "expected_type": card["expected_type"],
            "order_id": order["id"],
            "response_status": response.get("status"),
            "error_code": response.get("error_code"),
            "error_description": response.get("error_description"),
            "error_reason": response.get("error_reason"),
            "error_source": response.get("error_source"),
            "error_step": response.get("error_step"),
            "raw_response_keys": list(response.keys()),
        }
    except Exception as exc:
        logger.error("Error probing card %s: %s", card["label"], exc)
        return {
            "card_label": card["label"],
            "card_number_last4": card["number"][-4:],
            "expected_type": card["expected_type"],
            "probe_error": str(exc),
        }


def build_confirmed_codes(results: list[dict]) -> dict:
    """
    Build the confirmed_codes section from probe results.
    Only includes codes actually observed from the live test API.
    """
    hard = []
    soft = []
    ambiguous = []

    for r in results:
        code = r.get("error_code")
        if not code or r.get("probe_error"):
            continue
        expected = r.get("expected_type")
        if expected == "hard" and code not in hard:
            hard.append(code)
        elif expected == "soft" and code not in soft:
            soft.append(code)
        elif expected not in ("hard", "soft", "success"):
            ambiguous.append({"code": code, "label": r["card_label"]})

    return {"hard": hard, "soft": soft, "ambiguous": ambiguous}


def main():
    Path(os.path.dirname(OUTPUT_PATH)).mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("STEP 0: Discover real Razorpay decline codes")
    logger.info("=" * 60)
    logger.info("Probing %d test cards...", len(TEST_CARDS))

    results = []
    for card in TEST_CARDS:
        result = probe_card(card)
        results.append(result)
        logger.info(
            "  %-35s → error_code=%-30s reason=%s",
            card["label"],
            result.get("error_code", "N/A"),
            result.get("error_reason", "N/A"),
        )
        time.sleep(1.0)

    confirmed_codes = build_confirmed_codes(results)

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "Generated by scripts/discover_decline_codes.py against the live Razorpay test API. "
            "The 'confirmed_codes' section is what classifier.py uses. "
            "Review this file BEFORE building the classifier."
        ),
        "confirmed_codes": confirmed_codes,
        "raw_probe_results": results,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("")
    logger.info("=" * 60)
    logger.info("Results written to %s", OUTPUT_PATH)
    logger.info("")
    logger.info("CONFIRMED HARD codes: %s", confirmed_codes["hard"])
    logger.info("CONFIRMED SOFT codes: %s", confirmed_codes["soft"])
    logger.info("AMBIGUOUS codes:      %s", confirmed_codes["ambiguous"])
    logger.info("")
    logger.info("ACTION REQUIRED: Review %s before proceeding to Step 1.", OUTPUT_PATH)
    logger.info(
        "If any card returned a different code than expected, update the "
        "fallback tables in recova/classifier.py before building the classifier."
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
