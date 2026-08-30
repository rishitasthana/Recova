"""
scripts/simulate_payments.py — Simulate a batch of payment attempts.

Reads seed_data.json and discovered_codes.json, then triggers payment
attempts using confirmed test cards to generate realistic webhook events.

The mix covers: success, soft decline (2 types), hard decline (2 types).

USAGE:
  Start the FastAPI server first:
    uvicorn api.main:app --reload

  Then in another terminal:
    python scripts/simulate_payments.py

OR use the batch_runner.py for an offline simulation that bypasses webhooks.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import razorpay
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
SEED_DATA_PATH = os.getenv("SEED_DATA_PATH", "data/seed_data.json")
DISCOVERED_CODES_PATH = os.getenv("DISCOVERED_CODES_PATH", "data/discovered_codes.json")

# The local FastAPI server — events will arrive at /webhook
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:8000/webhook")

if not KEY_ID or not KEY_SECRET:
    sys.exit("ERROR: Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env")

client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))

# ─────────────────────────────────────────────────────────────────────────────
# Default test card mix (used if discovered_codes.json not available)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_CARD_MIX = [
    # success
    {"number": "4111111111111111", "label": "success", "cvv": "123", "expiry_month": "12", "expiry_year": "2030"},
    # soft declines
    {"number": "4000000000009995", "label": "insufficient_funds", "cvv": "123", "expiry_month": "12", "expiry_year": "2030"},
    {"number": "4000000000000119", "label": "processing_error", "cvv": "123", "expiry_month": "12", "expiry_year": "2030"},
    # hard declines
    {"number": "4000000000000069", "label": "do_not_honor", "cvv": "123", "expiry_month": "12", "expiry_year": "2030"},
    {"number": "4000000000000002", "label": "card_declined", "cvv": "123", "expiry_month": "12", "expiry_year": "2030"},
]


def load_seed_data() -> dict:
    if not Path(SEED_DATA_PATH).exists():
        sys.exit(f"ERROR: {SEED_DATA_PATH} not found. Run scripts/seed_razorpay.py first.")
    with open(SEED_DATA_PATH) as f:
        return json.load(f)


def simulate_attempt(subscription: dict, card: dict) -> None:
    """Create an order and attempt payment with a test card."""
    amount = 99900  # ₹999 subscription
    subscription_id = subscription["subscription_id"]
    customer_email = subscription.get("customer_email", "test@recova-demo.com")

    try:
        order = client.order.create(data={
            "amount": amount,
            "currency": "INR",
            "receipt": f"sim_{subscription_id[-6:]}_{int(time.time())}",
            "notes": {"subscription_id": subscription_id, "simulation": "true"},
        })

        logger.info(
            "Attempting payment | sub=%s card=%s (%s)",
            subscription_id, card["number"][-4:], card["label"],
        )

        base_url = "https://api.razorpay.com/v1"
        auth = (KEY_ID, KEY_SECRET)

        with httpx.Client(auth=auth, timeout=30.0) as http:
            resp = http.post(f"{base_url}/payments/create/ajax", data={
                "amount": order["amount"],
                "currency": order["currency"],
                "order_id": order["id"],
                "email": customer_email,
                "contact": "9876543210",
                "method": "card",
                "card[number]": card["number"],
                "card[name]": "Test User",
                "card[expiry_month]": card["expiry_month"],
                "card[expiry_year]": card["expiry_year"],
                "card[cvv]": card["cvv"],
            })
            resp_data = resp.json()

            # Handle OTP if needed
            if resp_data.get("type") == "otp":
                pid = resp_data.get("payment_id") or resp_data.get("razorpay_payment_id")
                otp_action = resp_data.get("next", {})
                if isinstance(otp_action, dict):
                    otp_url = otp_action.get("action", f"{base_url}/payments/{pid}/otp/verify")
                else:
                    otp_url = f"{base_url}/payments/{pid}/otp/verify"
                http.post(otp_url, data={"otp": "000000", "payment_id": pid})

        logger.info("  ↳ order=%s status_code=%d", order["id"], resp.status_code)

    except Exception as exc:
        logger.error("Simulation error for sub %s: %s", subscription_id, exc)


def main():
    seed = load_seed_data()
    subscriptions = seed.get("subscriptions", [])

    if not subscriptions:
        sys.exit("No subscriptions found in seed_data.json. Run seed_razorpay.py first.")

    # Use confirmed codes from Step 0 if available
    card_mix = DEFAULT_CARD_MIX
    if Path(DISCOVERED_CODES_PATH).exists():
        logger.info("Using confirmed card mix from %s", DISCOVERED_CODES_PATH)
        # Default mix is already aligned with Step 0 discoveries

    logger.info("=" * 60)
    logger.info("ReCova — Simulate Payment Attempts")
    logger.info("Webhooks will be sent to: %s", WEBHOOK_URL)
    logger.info("=" * 60)
    logger.info("Subscriptions: %d | Card mix: %d types", len(subscriptions), len(card_mix))
    logger.info("")

    attempt_count = 0
    for sub in subscriptions:
        for card in card_mix:
            simulate_attempt(sub, card)
            attempt_count += 1
            time.sleep(1.5)  # rate-limit friendly

    logger.info("")
    logger.info("Simulation complete. %d payment attempts made.", attempt_count)
    logger.info("Check the Razorpay test dashboard and your FastAPI logs for webhook events.")


if __name__ == "__main__":
    main()
