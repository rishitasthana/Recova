"""
recova/execution.py — Execution layer.

Takes a decision_id, performs the appropriate action, and writes the result
to actions_log. Every branch writes something to the log — there are no
silent no-ops.

retry_now / retry_scheduled: calls Razorpay API to create a new payment order.
send_reminder:               builds a realistic mock email payload and logs it.
escalate_promise_to_pay:     builds a mock escalation ticket and logs it.
stop:                        logs the stop reason, no API calls.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import razorpay

from recova import db

logger = logging.getLogger(__name__)

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# Receipt prefix for Razorpay orders created by ReCova
RECEIPT_PREFIX = "recova_retry"


def _get_razorpay_client() -> razorpay.Client:
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def execute(decision_id: int) -> Optional[int]:
    """
    Execute the action prescribed by the given decision.
    Returns the actions_log row id, or None if the decision is not found.
    """
    decision = db.get_decision(decision_id)
    if decision is None:
        logger.error("[execution] Decision %d not found", decision_id)
        return None
    decision = dict(decision)

    cls_row = db.get_classification(decision["classification_id"])
    event = db.get_payment_event(cls_row["payment_event_id"])
    cls_row = dict(cls_row)
    event = dict(event)

    decision_type: str = decision["decision"]
    rule_triggered: str = decision["rule_triggered"]

    if decision_type in ("retry_now", "retry_scheduled"):
        return _execute_retry(decision_id, decision, event)
    elif decision_type == "send_reminder":
        return _execute_send_reminder(decision_id, decision, event, cls_row)
    elif decision_type == "escalate_promise_to_pay":
        return _execute_escalate(decision_id, decision, event)
    elif decision_type == "stop":
        return _execute_stop(decision_id, rule_triggered, event)
    else:
        # Unknown decision type — log as failure so it's visible in the dashboard
        return db.insert_action_log({
            "decision_id": decision_id,
            "action_type": decision_type,
            "payload": None,
            "outcome": "failure",
            "outcome_detail": f"Unknown decision type: {decision_type}",
        })


# ─────────────────────────────────────────────────────────────────────────────
# Retry (retry_now and retry_scheduled)
# ─────────────────────────────────────────────────────────────────────────────

def _execute_retry(decision_id: int, decision: dict, event: dict) -> int:
    """
    Attempt to re-charge by creating a new Razorpay order for the same amount
    and linking it back to the subscription.

    In Razorpay test mode a new order is the closest equivalent to re-charging
    a subscription payment — the subscription's own /charge endpoint is only
    available in production flows. The order is created with the same amount,
    currency, and a receipt that encodes the retry context.
    """
    amount = event["amount"]
    currency = event.get("currency", "INR")
    subscription_id = event.get("subscription_id", "")
    payment_id = event.get("payment_id", "")
    decision_type = decision["decision"]

    receipt = f"{RECEIPT_PREFIX}_{decision_id}_{int(datetime.now(timezone.utc).timestamp())}"

    order_payload = {
        "amount": amount,
        "currency": currency,
        "receipt": receipt,
        "notes": {
            "recova_decision_id": str(decision_id),
            "recova_original_payment_id": payment_id,
            "recova_subscription_id": subscription_id,
            "recova_action": decision_type,
        },
    }

    try:
        client = _get_razorpay_client()
        order = client.order.create(data=order_payload)
        outcome = "success"
        outcome_detail = json.dumps({
            "razorpay_order_id": order.get("id"),
            "status": order.get("status"),
            "amount": order.get("amount"),
        })
        logger.info(
            "[execution] %s succeeded — order %s for %d %s",
            decision_type, order.get("id"), amount, currency,
        )
    except Exception as exc:
        outcome = "api_error"
        outcome_detail = str(exc)
        logger.warning(
            "[execution] %s API error for decision %d: %s",
            decision_type, decision_id, exc,
        )

    return db.insert_action_log({
        "decision_id": decision_id,
        "action_type": decision_type,
        "payload": json.dumps(order_payload),
        "outcome": outcome,
        "outcome_detail": outcome_detail,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Send Reminder
# ─────────────────────────────────────────────────────────────────────────────

def _execute_send_reminder(
    decision_id: int, decision: dict, event: dict, cls_row: dict
) -> int:
    """
    Build a realistic mock email payload. No actual email is sent —
    the payload is logged with full content so the dashboard can display it.
    """
    amount_inr = (event["amount"] or 0) / 100
    customer_id = event.get("customer_id") or "N/A"
    payment_id = event.get("payment_id") or "N/A"
    error_code = cls_row.get("error_code") or "unknown"
    reason = cls_row.get("reason") or ""

    # Map decline type to a user-appropriate action
    if "expired" in error_code.lower() or "expired" in reason.lower():
        subject = "Action Required: Your payment card has expired — please update to continue your subscription"
        action_text = "update your card details"
        card_action_url = "https://billing.example.com/update-card"
    else:
        subject = "Action Required: Your payment could not be processed — please review your card details"
        action_text = "review and update your payment method"
        card_action_url = "https://billing.example.com/payment-methods"

    body = f"""Dear Customer ({customer_id}),

We were unable to process your subscription payment of ₹{amount_inr:,.2f}.

Reference: {payment_id}
Decline reason: {error_code}

To continue your subscription without interruption, please {action_text}:
{card_action_url}

If you believe this is an error, please contact us at support@recova-demo.com.

This is an automated message from ReCova — AI Revenue Recovery.
"""

    payload = {
        "channel": "email",
        "to": f"customer_{customer_id}@demo.com",
        "subject": subject,
        "body": body,
        "metadata": {
            "recova_decision_id": decision_id,
            "payment_id": payment_id,
            "amount_inr": amount_inr,
            "rule_triggered": decision["rule_triggered"],
        },
    }

    logger.info(
        "[execution] send_reminder logged for decision %d (customer %s)",
        decision_id, customer_id,
    )

    return db.insert_action_log({
        "decision_id": decision_id,
        "action_type": "send_reminder",
        "payload": json.dumps(payload),
        "outcome": "logged",
        "outcome_detail": f"Reminder email queued for customer {customer_id} re: payment {payment_id}",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Escalate — Promise to Pay
# ─────────────────────────────────────────────────────────────────────────────

def _execute_escalate(decision_id: int, decision: dict, event: dict) -> int:
    """
    Build a realistic mock escalation ticket payload.
    Logged with full content; no external system integration required for demo.
    """
    amount_inr = (event["amount"] or 0) / 100
    customer_id = event.get("customer_id") or "N/A"
    subscription_id = event.get("subscription_id") or "N/A"
    payment_id = event.get("payment_id") or "N/A"

    retry_count = db.get_retry_count(subscription_id)

    ticket = {
        "ticket_type": "promise_to_pay_escalation",
        "priority": "high" if amount_inr >= 1000 else "medium",
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "original_payment_id": payment_id,
        "amount_inr": amount_inr,
        "prior_retry_attempts": retry_count,
        "rule_triggered": decision["rule_triggered"],
        "rule_explanation": decision["rule_explanation"],
        "suggested_contact_window": "business hours, 10:00–18:00 IST",
        "suggested_script": (
            f"Customer {customer_id} has a failed payment of ₹{amount_inr:,.2f} "
            f"on subscription {subscription_id}. {retry_count} automated retry "
            f"attempt(s) have been made. Please contact the customer to arrange "
            f"a promise-to-pay or alternative payment method."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "recova_decision_id": decision_id,
    }

    logger.info(
        "[execution] escalate_promise_to_pay logged for decision %d "
        "(customer %s, ₹%.2f, %d prior retries)",
        decision_id, customer_id, amount_inr, retry_count,
    )

    return db.insert_action_log({
        "decision_id": decision_id,
        "action_type": "escalate_promise_to_pay",
        "payload": json.dumps(ticket),
        "outcome": "logged",
        "outcome_detail": (
            f"Escalation ticket created for customer {customer_id} "
            f"— ₹{amount_inr:,.2f}, {retry_count} prior retries"
        ),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Stop
# ─────────────────────────────────────────────────────────────────────────────

def _execute_stop(decision_id: int, rule_triggered: str, event: dict) -> int:
    """
    Log that recovery was stopped and why. No API calls.
    """
    payment_id = event.get("payment_id") or "N/A"
    amount_inr = (event["amount"] or 0) / 100

    logger.info(
        "[execution] stop logged for decision %d — rule=%s payment=%s",
        decision_id, rule_triggered, payment_id,
    )

    return db.insert_action_log({
        "decision_id": decision_id,
        "action_type": "stop",
        "payload": json.dumps({
            "payment_id": payment_id,
            "amount_inr": amount_inr,
            "rule_triggered": rule_triggered,
        }),
        "outcome": "skipped",
        "outcome_detail": f"Recovery stopped — rule: {rule_triggered}",
    })
