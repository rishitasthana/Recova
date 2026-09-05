"""
tests/test_new_features.py — Integration and unit tests for new ReCova features:
  - Recovery resolution and settlement loop
  - Rules summary reporting
  - Interactive simulation API (/simulate)
  - Recovery settlement API (/recover)
  - Export endpoints (/export/csv, /export/json)
  - payment.captured webhook recovery resolution
"""

import json
import pytest
from fastapi.testclient import TestClient

from recova import db
from api.main import app


@pytest.fixture
def client(test_db):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_record_recovery_by_event_id(test_db):
    """Test marking an action as recovered by event_id."""
    event_id = db.insert_payment_event({
        "razorpay_event_id": "evt_rec_test_1",
        "payment_id": "pay_rec_test_1",
        "subscription_id": "sub_rec_test_1",
        "amount": 99900,
        "status": "failed",
        "error_code": "insufficient_funds",
    })
    cls_id = db.insert_classification({
        "payment_event_id": event_id,
        "classification": "soft",
        "confidence": 1.0,
        "reason": "Test soft",
        "lookup_source": "confirmed_table",
    })
    dec_id = db.insert_decision({
        "classification_id": cls_id,
        "decision": "retry_scheduled",
        "rule_triggered": "rule_6_soft_retry_scheduled",
        "rule_explanation": "Test scheduled",
    })
    action_id = db.insert_action_log({
        "decision_id": dec_id,
        "action_type": "retry_scheduled",
        "outcome": "logged",
        "outcome_detail": "Scheduled retry",
    })

    # Prior metrics: recovered should be 0
    m_before = db.get_summary_metrics()
    assert m_before["recovered_paise"] == 0

    # Settle recovery
    updated = db.record_recovery(event_id=event_id, detail="Customer paid")
    assert updated == action_id

    # Post metrics: recovered should be 99900
    m_after = db.get_summary_metrics()
    assert m_after["recovered_paise"] == 99900
    assert m_after["recovery_rate"] > 0.0


def test_get_rules_summary(test_db):
    """Test retrieving rule statistics."""
    summary = db.get_rules_summary()
    assert len(summary) >= 6
    rules = [r["rule"] for r in summary]
    assert "rule_1_stale_stop" in rules
    assert "rule_2_hard_permanent_stop" in rules
    assert "rule_5_soft_low_amount_retry_now" in rules


def test_api_simulate(client):
    """Test POST /simulate endpoint."""
    resp = client.post("/simulate", json={
        "amount": 49900,
        "error_code": "insufficient_funds",
        "customer_id": "cust_test_api",
        "subscription_id": "sub_test_api",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processed"
    assert data["classification"]["classification"] == "soft"
    # Amount 499 is <= 500 so rule_5 should fire for attempt 0
    assert data["decision"]["decision"] == "retry_now"


def test_api_recover(client):
    """Test POST /recover endpoint."""
    # First simulate an event
    sim_resp = client.post("/simulate", json={
        "amount": 99900,
        "error_code": "insufficient_funds",
    })
    event_id = sim_resp.json()["event_id"]

    # Now recover it
    rec_resp = client.post("/recover", json={
        "event_id": event_id,
        "note": "Payment link completed",
    })
    assert rec_resp.status_code == 200
    assert rec_resp.json()["status"] == "recovered"

    # Verify metrics updated
    metrics_resp = client.get("/metrics")
    assert metrics_resp.json()["recovered_paise"] == 99900


def test_api_rules(client):
    """Test GET /rules endpoint."""
    resp = client.get("/rules")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 6


def test_api_export_csv_and_json(client):
    """Test /export/csv and /export/json endpoints."""
    # Simulate an event so data exists
    client.post("/simulate", json={"amount": 99900, "error_code": "insufficient_funds"})

    # Test CSV
    csv_resp = client.get("/export/csv")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "payment_id" in csv_resp.text

    # Test JSON
    json_resp = client.get("/export/json")
    assert json_resp.status_code == 200
    data = json_resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_webhook_payment_captured_settlement(client):
    """Test that payment.captured webhook automatically resolves pending recovery."""
    sub_id = "sub_webhook_captured_test"
    # Create a failed event on this subscription
    sim_resp = client.post("/simulate", json={
        "amount": 99900,
        "subscription_id": sub_id,
        "error_code": "insufficient_funds",
    })
    assert sim_resp.status_code == 200

    # Verify initially recovered is 0
    assert client.get("/metrics").json()["recovered_paise"] == 0

    # Deliver payment.captured webhook for the same subscription
    payload = {
        "event": "payment.captured",
        "id": "evt_captured_test_123",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_captured_test_123",
                    "subscription_id": sub_id,
                    "amount": 99900,
                    "status": "captured",
                }
            }
        }
    }
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"
    assert resp.json()["recovered_action_id"] is not None

    # Now verify metrics show recovered amount
    assert client.get("/metrics").json()["recovered_paise"] == 99900
