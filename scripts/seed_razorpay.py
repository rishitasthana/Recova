"""
scripts/seed_razorpay.py — Create test customers, plans, and subscriptions.

Creates:
  5 test customers
  1 monthly plan (₹999)
  5 subscriptions (one per customer, 12 billing cycles)

Writes all IDs to data/seed_data.json for use by simulate_payments.py
and the batch runner.

USAGE:
  python scripts/seed_razorpay.py

Run once. Re-running is safe — it appends to seed_data.json rather than
replacing it, but Razorpay will create new objects each time.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

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

if not KEY_ID or not KEY_SECRET:
    sys.exit(
        "ERROR: Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in your .env file."
    )

client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))

CUSTOMERS = [
    {"name": "Priya Sharma",   "email": "priya.sharma@recova-demo.com",   "contact": "9876500001"},
    {"name": "Rahul Verma",    "email": "rahul.verma@recova-demo.com",    "contact": "9876500002"},
    {"name": "Ananya Singh",   "email": "ananya.singh@recova-demo.com",   "contact": "9876500003"},
    {"name": "Karan Mehta",    "email": "karan.mehta@recova-demo.com",    "contact": "9876500004"},
    {"name": "Deepa Nair",     "email": "deepa.nair@recova-demo.com",     "contact": "9876500005"},
]

PLAN_CONFIG = {
    "period": "monthly",
    "interval": 1,
    "item": {
        "name": "ReCova Pro — Monthly",
        "amount": 99900,      # ₹999 in paise
        "currency": "INR",
        "description": "Monthly SaaS subscription — ReCova Pro tier",
    },
}

SUBSCRIPTION_CONFIG = {
    "total_count": 12,        # 12 billing cycles
    "quantity": 1,
    "notes": {"source": "recova_seed_script"},
}


def create_customer(customer_data: dict) -> dict:
    customer = client.customer.create(data=customer_data)
    logger.info("Created customer: %s → %s", customer_data["name"], customer["id"])
    return customer


def create_plan() -> dict:
    plan = client.plan.create(data=PLAN_CONFIG)
    logger.info("Created plan: %s → %s (₹%.2f/month)", PLAN_CONFIG["item"]["name"], plan["id"], PLAN_CONFIG["item"]["amount"] / 100)
    return plan


def create_subscription(plan_id: str, customer_id: str, customer_name: str) -> dict:
    config = {
        **SUBSCRIPTION_CONFIG,
        "plan_id": plan_id,
        "customer_id": customer_id,
    }
    sub = client.subscription.create(data=config)
    logger.info("Created subscription for %s: %s", customer_name, sub["id"])
    return sub


def main():
    Path(os.path.dirname(SEED_DATA_PATH)).mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("ReCova — Seed Razorpay Test Data")
    logger.info("=" * 60)

    # Create customers
    customers = []
    for c in CUSTOMERS:
        try:
            customer = create_customer(c)
            customers.append(customer)
            time.sleep(0.3)
        except Exception as exc:
            logger.error("Failed to create customer %s: %s", c["name"], exc)

    # Create plan
    try:
        plan = create_plan()
        time.sleep(0.3)
    except Exception as exc:
        sys.exit(f"Failed to create plan: {exc}")

    # Create subscriptions
    subscriptions = []
    for customer in customers:
        try:
            sub = create_subscription(plan["id"], customer["id"], customer.get("name", ""))
            subscriptions.append({
                "subscription_id": sub["id"],
                "customer_id": customer["id"],
                "customer_name": customer.get("name"),
                "customer_email": customer.get("email"),
                "plan_id": plan["id"],
                "status": sub.get("status"),
            })
            time.sleep(0.3)
        except Exception as exc:
            logger.error("Failed to create subscription for %s: %s", customer.get("id"), exc)

    seed_data = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "plan": {
            "id": plan["id"],
            "name": PLAN_CONFIG["item"]["name"],
            "amount_paise": PLAN_CONFIG["item"]["amount"],
        },
        "customers": [
            {"id": c["id"], "name": c.get("name"), "email": c.get("email")}
            for c in customers
        ],
        "subscriptions": subscriptions,
    }

    with open(SEED_DATA_PATH, "w") as f:
        json.dump(seed_data, f, indent=2)

    logger.info("")
    logger.info("Seed data written to %s", SEED_DATA_PATH)
    logger.info("Customers:     %d", len(customers))
    logger.info("Plan:          %s", plan["id"])
    logger.info("Subscriptions: %d", len(subscriptions))
    logger.info("")
    logger.info("Next step: python scripts/simulate_payments.py")


if __name__ == "__main__":
    main()
