"""
recova/models.py — Pydantic models for type safety across all pipeline layers.

These are data-transfer objects; they do not touch the DB directly.
"""

from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Payment Events
# ─────────────────────────────────────────────────────────────────────────────

class PaymentEventIn(BaseModel):
    """Parsed fields extracted from a Razorpay webhook payload."""
    razorpay_event_id: str
    payment_id: Optional[str] = None
    subscription_id: Optional[str] = None
    customer_id: Optional[str] = None
    amount: Optional[int] = None          # paise
    currency: str = "INR"
    status: Literal["failed", "captured"]
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    error_reason: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    raw_payload: str                       # full JSON string


class PaymentEventRecord(PaymentEventIn):
    """As stored in the DB."""
    id: int
    created_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: Classifications
# ─────────────────────────────────────────────────────────────────────────────

ClassificationType = Literal["hard", "soft", "unknown"]
LookupSource = Literal["confirmed_table", "fallback_table", "reason_substring", "default"]

class ClassificationRecord(BaseModel):
    id: int
    payment_event_id: int
    classification: ClassificationType
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    error_code: Optional[str] = None
    error_reason: Optional[str] = None
    lookup_source: LookupSource = "default"
    created_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: Decisions
# ─────────────────────────────────────────────────────────────────────────────

DecisionType = Literal[
    "retry_now",
    "retry_scheduled",
    "send_reminder",
    "escalate_promise_to_pay",
    "stop",
]

class DecisionRecord(BaseModel):
    id: int
    classification_id: int
    decision: DecisionType
    retry_at: Optional[datetime] = None
    rule_triggered: str    # short machine-readable name e.g. "rule_2_hard_stop"
    rule_explanation: str  # full one-sentence justification
    created_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: Actions Log
# ─────────────────────────────────────────────────────────────────────────────

ActionOutcome = Literal["success", "failure", "api_error", "skipped", "logged"]

class ActionLogRecord(BaseModel):
    id: int
    decision_id: int
    action_type: str
    payload: Optional[str] = None     # JSON string
    outcome: ActionOutcome
    outcome_detail: Optional[str] = None
    executed_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline result (aggregates all four layers for a single event)
# ─────────────────────────────────────────────────────────────────────────────

class PipelineResult(BaseModel):
    event_id: int
    classification_id: Optional[int] = None
    decision_id: Optional[int] = None
    action_log_id: Optional[int] = None
    classification: Optional[ClassificationType] = None
    decision: Optional[DecisionType] = None
    outcome: Optional[ActionOutcome] = None
    error: Optional[str] = None       # set if any stage raised an exception


# ─────────────────────────────────────────────────────────────────────────────
# Webhook payload wrapper (used by FastAPI endpoint)
# ─────────────────────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    """Minimal structure Razorpay sends for payment events."""
    event: str
    payload: dict


# ─────────────────────────────────────────────────────────────────────────────
# Batch results (written to data/batch_results.json by the batch runner)
# ─────────────────────────────────────────────────────────────────────────────

class RecoveryByType(BaseModel):
    classification: Optional[str]
    total: int
    at_risk_paise: int
    recovered_paise: int
    recovery_rate: float


class BatchResults(BaseModel):
    run_at: datetime
    total_events: int
    revenue_at_risk_paise: int
    revenue_at_risk_inr: float
    recovered_paise: int
    recovered_inr: float
    recovery_rate: float
    avg_time_to_recovery_minutes: Optional[float]
    decision_counts: dict[str, int]
    classification_counts: dict[str, int]
    stopped_by_rule: dict[str, int]
    recovery_by_type: list[RecoveryByType]
    invariant_check_passed: bool          # hard declines never got a retry
    invariant_violations: int
