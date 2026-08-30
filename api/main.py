"""
api/main.py — FastAPI application: webhook receiver + read endpoints.

Webhook idempotency:
  Razorpay can redeliver the same webhook. The UNIQUE constraint on
  payment_events.razorpay_event_id catches duplicates at the DB layer.
  db.insert_payment_event() returns None for a duplicate; the endpoint
  returns 200 immediately without reprocessing.

Concurrency note (documented limitation):
  The webhook receiver and the batch runner both write through the same
  pipeline. For this buildathon demo they are designed to run sequentially
  — do not fire live webhooks while a batch run is in progress. This is a
  known limitation, not a bug, and is documented in the README.
"""

import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from recova import db
from recova.pipeline import process_failed_payment

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB on startup."""
    db.init_db()
    logger.info("[main] ReCova API started")
    yield
    logger.info("[main] ReCova API shutting down")


app = FastAPI(
    title="ReCova — AI Revenue Recovery Agent",
    description="Detects, classifies, and recovers failed Razorpay subscription payments.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify Razorpay webhook signature.
    HMAC-SHA256 of the raw request body using the webhook secret.
    """
    if not secret:
        logger.warning("[webhook] RAZORPAY_WEBHOOK_SECRET not set — skipping signature check")
        return True  # allow in dev mode
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_event_data(event_type: str, payload: dict, raw_body: str) -> dict:
    """Extract structured fields from a Razorpay webhook payload."""
    payment = payload.get("payment", {}).get("entity", {})
    subscription = payload.get("subscription", {}).get("entity", {})

    error = payment.get("error_code") and {
        "code": payment.get("error_code"),
        "description": payment.get("error_description"),
        "reason": payment.get("error_reason"),
        "source": payment.get("error_source"),
        "step": payment.get("error_step"),
    }

    return {
        "razorpay_event_id": payload.get("id") or f"evt_{payment.get('id', 'unknown')}",
        "payment_id": payment.get("id"),
        "subscription_id": (
            payment.get("subscription_id")
            or subscription.get("id")
        ),
        "customer_id": payment.get("customer_id"),
        "amount": payment.get("amount"),
        "currency": payment.get("currency", "INR"),
        "status": "failed" if event_type == "payment.failed" else "captured",
        "error_code": (error or {}).get("code"),
        "error_description": (error or {}).get("description"),
        "error_reason": (error or {}).get("reason"),
        "error_source": (error or {}).get("source"),
        "error_step": (error or {}).get("step"),
        "raw_payload": raw_body,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health():
    """Liveness check."""
    return {"status": "ok", "service": "recova"}


@app.post("/webhook", tags=["webhook"])
async def receive_webhook(request: Request):
    """
    Razorpay webhook receiver.

    Handles:
      - payment.failed   → classify → decide → execute recovery action
      - payment.captured → record only (for comparison/metrics)

    Returns 200 immediately in all cases (Razorpay will retry on non-2xx).
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not _verify_webhook_signature(body, signature, WEBHOOK_SECRET):
        logger.warning("[webhook] Invalid signature — rejected")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type: str = data.get("event", "")
    payload: dict = data.get("payload", {})

    if event_type not in ("payment.failed", "payment.captured"):
        logger.debug("[webhook] Ignored event type: %s", event_type)
        return Response(status_code=200)

    event_data = _extract_event_data(event_type, payload, body.decode("utf-8"))

    # Insert — returns None if this is a duplicate delivery
    event_id = db.insert_payment_event(event_data)
    if event_id is None:
        logger.info(
            "[webhook] Duplicate event %s — skipped",
            event_data["razorpay_event_id"],
        )
        return {"status": "duplicate", "skipped": True}

    logger.info(
        "[webhook] Stored event %s (type=%s payment=%s)",
        event_data["razorpay_event_id"], event_type, event_data["payment_id"],
    )

    # Process failed payments through the full pipeline
    if event_type == "payment.failed":
        result = process_failed_payment(event_id)
        if result.error:
            # Pipeline error is logged but we still return 200 so Razorpay doesn't retry
            logger.error("[webhook] Pipeline error for event %d: %s", event_id, result.error)
        return {
            "status": "processed",
            "event_id": event_id,
            "classification_id": result.classification_id,
            "decision_id": result.decision_id,
            "action_log_id": result.action_log_id,
            "pipeline_error": result.error,
        }

    return {"status": "recorded", "event_id": event_id}


@app.get("/events", tags=["data"])
async def get_events(limit: int = 50):
    """
    Return the last N events with their full pipeline chain.
    Used by the Streamlit dashboard for its live feed.
    """
    rows = db.get_full_pipeline_view(limit=limit)
    return [dict(r) for r in rows]


@app.get("/metrics", tags=["data"])
async def get_metrics():
    """Return aggregate recovery metrics. Used by the dashboard KPI tiles."""
    return db.get_summary_metrics()
