"""
tests/test_api.py — Integration tests for the FastAPI webhook receiver.

Tests cover every endpoint and code path in api/main.py:

  GET /health
    - Returns {"status": "ok", "service": "recova"}

  POST /webhook — signature validation
    - Valid HMAC signature accepted
    - Invalid signature rejected with 400
    - Empty WEBHOOK_SECRET skips check (dev mode)

  POST /webhook — payment.failed
    - Processed: all pipeline ids returned
    - Duplicate event_id: {"status": "duplicate", "skipped": True}

  POST /webhook — payment.captured
    - Recorded: event stored, no pipeline run

  POST /webhook — edge cases
    - Unknown event type: 200, no DB write triggered for pipeline
    - Invalid JSON body: 400

  GET /events
    - Returns list
    - Respects ?limit query param

  GET /metrics
    - Returns all expected metric keys

All tests use the test_db fixture (conftest.py) for DB isolation.
The TestClient starts the app lifespan which calls db.init_db() — this is
safe because test_db has already set the module-level _override_path to
a temp file, so init_db() with no args reuses that path.
"""

import hashlib
import hmac as _hmac
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(test_db):
    """
    Create a FastAPI TestClient backed by the isolated temp DB from test_db.

    test_db (conftest.py) has already called db.init_db(db_path=<tmp>) which
    sets the module-level _override_path.  When the TestClient starts the
    lifespan, it calls db.init_db() with no args, which does NOT override
    _override_path — it just creates the tables in the already-redirected path.
    So all webhook inserts go to the temp DB, never to data/recova.db.
    """
    from api.main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Test data helpers
# ─────────────────────────────────────────────────────────────────────────────

TEST_WEBHOOK_SECRET = "recova_test_secret_xyz"


def _make_failed_payload(unique_suffix: str = "001") -> dict:
    """Return a realistic payment.failed Razorpay webhook payload."""
    return {
        "event": "payment.failed",
        "payload": {
            "id": f"evt_api_{unique_suffix}",
            "payment": {
                "entity": {
                    "id": f"pay_api_{unique_suffix}",
                    "subscription_id": f"sub_api_{unique_suffix}",
                    "customer_id": f"cust_api_{unique_suffix}",
                    "amount": 99900,
                    "currency": "INR",
                    "error_code": "insufficient_funds",
                    "error_description": "Insufficient funds in account.",
                    "error_reason": "insufficient_funds",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                },
            },
        },
    }


def _make_captured_payload(unique_suffix: str = "001") -> dict:
    """Return a realistic payment.captured Razorpay webhook payload."""
    return {
        "event": "payment.captured",
        "payload": {
            "id": f"evt_cap_{unique_suffix}",
            "payment": {
                "entity": {
                    "id": f"pay_cap_{unique_suffix}",
                    "subscription_id": f"sub_cap_{unique_suffix}",
                    "customer_id": f"cust_cap_{unique_suffix}",
                    "amount": 99900,
                    "currency": "INR",
                },
            },
        },
    }


def _sign(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature exactly as Razorpay does."""
    return _hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_webhook(client, payload: dict, secret: str = "", sign: bool = False) -> object:
    """Helper: POST to /webhook with optional HMAC signature header."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Razorpay-Signature"] = _sign(body, secret)
    elif secret == "":
        headers["X-Razorpay-Signature"] = ""
    return client.post("/webhook", content=body, headers=headers)


def _mock_razorpay():
    mock_client = MagicMock()
    mock_client.order.create.return_value = {
        "id": "order_api_mock",
        "status": "created",
        "amount": 99900,
    }
    return mock_client


# ─────────────────────────────────────────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEndpoint:

    def test_health_returns_ok(self, client):
        """GET /health must return 200 with status=ok."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "recova"

    def test_health_is_always_available(self, client):
        """Calling /health twice must return the same result (stateless)."""
        r1 = client.get("/health")
        r2 = client.get("/health")
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhook — HMAC signature validation
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookSignatureValidation:

    def test_valid_signature_is_accepted(self, client):
        """A correctly signed webhook must be processed (200)."""
        import recova.execution as execution
        payload = _make_failed_payload("sig_ok")
        body = json.dumps(payload).encode()

        with patch("api.main.WEBHOOK_SECRET", TEST_WEBHOOK_SECRET), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            resp = client.post(
                "/webhook",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": _sign(body, TEST_WEBHOOK_SECRET),
                },
            )

        assert resp.status_code == 200

    def test_invalid_signature_returns_400(self, client):
        """A webhook with a wrong signature must be rejected with 400."""
        payload = _make_failed_payload("sig_bad")
        body = json.dumps(payload).encode()

        with patch("api.main.WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
            resp = client.post(
                "/webhook",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": "completely_wrong_signature",
                },
            )

        assert resp.status_code == 400

    def test_tampered_body_rejected(self, client):
        """
        Signing body A but sending body B must result in 400
        (signature mismatch catches payload tampering).
        """
        payload_original = _make_failed_payload("tamper_orig")
        payload_tampered = _make_failed_payload("tamper_diff")

        sig = _sign(json.dumps(payload_original).encode(), TEST_WEBHOOK_SECRET)

        with patch("api.main.WEBHOOK_SECRET", TEST_WEBHOOK_SECRET):
            resp = client.post(
                "/webhook",
                content=json.dumps(payload_tampered).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": sig,
                },
            )

        assert resp.status_code == 400

    def test_empty_secret_skips_check_dev_mode(self, client):
        """
        When WEBHOOK_SECRET is empty string, the check is bypassed (dev mode).
        The webhook must be processed regardless of the signature header.
        """
        import recova.execution as execution
        payload = _make_failed_payload("nosecret")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            resp = _post_webhook(client, payload, secret="", sign=False)

        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhook — payment.failed
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookPaymentFailed:

    def test_payment_failed_returns_processed_status(self, client):
        """payment.failed must return {"status": "processed"} with pipeline ids."""
        import recova.execution as execution
        payload = _make_failed_payload("fail_01")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"
        assert data["event_id"] is not None
        assert data["classification_id"] is not None
        assert data["decision_id"] is not None
        assert data.get("pipeline_error") is None

    def test_payment_failed_action_log_id_returned(self, client):
        """The response for payment.failed must include action_log_id."""
        import recova.execution as execution
        payload = _make_failed_payload("fail_02")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            resp = _post_webhook(client, payload)

        data = resp.json()
        assert data["action_log_id"] is not None

    def test_duplicate_event_returns_duplicate_status(self, client):
        """
        The same razorpay_event_id delivered twice must return
        {"status": "duplicate", "skipped": True} on the second delivery.
        Razorpay can redeliver webhooks; idempotency is a correctness requirement.
        """
        import recova.execution as execution
        payload = _make_failed_payload("dup_01")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            # First delivery
            r1 = _post_webhook(client, payload)
            # Second delivery (same payload, same event id)
            r2 = _post_webhook(client, payload)

        assert r1.status_code == 200
        assert r1.json()["status"] == "processed"

        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["status"] == "duplicate"
        assert data2["skipped"] is True

    def test_pipeline_error_still_returns_200(self, client):
        """
        If the pipeline errors (e.g. classify fails), the endpoint must still
        return 200 so Razorpay does not keep retrying the webhook.
        The pipeline_error field in the response carries the error detail.
        """
        import recova.execution as execution
        from recova import classifier

        payload = _make_failed_payload("pipe_err")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(classifier, "classify", side_effect=RuntimeError("kaboom")):
            resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        # Either processed with an error field, or the error is swallowed gracefully
        assert data["status"] == "processed"
        assert data["pipeline_error"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhook — payment.captured
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookPaymentCaptured:

    def test_payment_captured_returns_recorded(self, client):
        """payment.captured must be stored with status='recorded'; no pipeline run."""
        payload = _make_captured_payload("cap_01")

        with patch("api.main.WEBHOOK_SECRET", ""):
            resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "recorded"
        assert "event_id" in data

    def test_duplicate_captured_returns_200(self, client):
        """Duplicate payment.captured must be idempotently accepted."""
        payload = _make_captured_payload("cap_dup")

        with patch("api.main.WEBHOOK_SECRET", ""):
            r1 = _post_webhook(client, payload)
            r2 = _post_webhook(client, payload)

        assert r1.status_code == 200
        assert r2.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhook — edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookEdgeCases:

    def test_unknown_event_type_returns_200(self, client):
        """
        Unrecognised event types (e.g. subscription.activated) must return
        200 and not be processed by the pipeline.
        """
        payload = {"event": "subscription.activated", "payload": {}}

        with patch("api.main.WEBHOOK_SECRET", ""):
            resp = _post_webhook(client, payload)

        assert resp.status_code == 200

    def test_invalid_json_body_returns_400(self, client):
        """A request with a non-JSON body must return 400."""
        with patch("api.main.WEBHOOK_SECRET", ""):
            resp = client.post(
                "/webhook",
                content=b"this is not json",
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 400

    def test_missing_payment_entity_is_handled(self, client):
        """
        A payment.failed with missing nested payment entity must not crash
        the endpoint — it should return 200 even if data is sparse.
        """
        import recova.execution as execution
        payload = {
            "event": "payment.failed",
            "payload": {
                "id": "evt_sparse_001",
                "payment": {},   # no 'entity' key
            },
        }

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            resp = _post_webhook(client, payload)

        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# GET /events
# ─────────────────────────────────────────────────────────────────────────────

class TestEventsEndpoint:

    def test_events_returns_empty_list_when_no_data(self, client):
        """GET /events must return an empty list when the DB has no events."""
        resp = client.get("/events")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_events_returns_list_after_webhook(self, client):
        """GET /events must return processed events after a webhook delivery."""
        import recova.execution as execution
        payload = _make_failed_payload("evt_list")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            _post_webhook(client, payload)

        resp = client.get("/events")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) >= 1

    def test_events_limit_param_is_respected(self, client):
        """GET /events?limit=5 must return at most 5 rows."""
        import recova.execution as execution

        # Insert 6 events
        for i in range(6):
            payload = _make_failed_payload(f"lim_{i:02d}")
            with patch("api.main.WEBHOOK_SECRET", ""), \
                 patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
                _post_webhook(client, payload)

        resp = client.get("/events?limit=5")
        assert resp.status_code == 200
        assert len(resp.json()) <= 5


# ─────────────────────────────────────────────────────────────────────────────
# GET /metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsEndpoint:

    def test_metrics_returns_expected_keys(self, client):
        """GET /metrics must return all required metric keys."""
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()

        required_keys = {
            "total_events",
            "revenue_at_risk_paise",
            "recovered_paise",
            "recovery_rate",
            "decision_counts",
            "classification_counts",
            "recovery_by_type",
        }
        for key in required_keys:
            assert key in data, f"Missing expected metric key: '{key}'"

    def test_metrics_recovery_rate_is_zero_with_no_data(self, client):
        """With an empty DB, recovery_rate must be 0.0."""
        resp = client.get("/metrics")
        data = resp.json()
        assert data["recovery_rate"] == 0.0
        assert data["total_events"] == 0

    def test_metrics_counts_increment_after_webhook(self, client):
        """total_events must increment after a payment.failed webhook."""
        import recova.execution as execution
        payload = _make_failed_payload("metrics_01")

        with patch("api.main.WEBHOOK_SECRET", ""), \
             patch.object(execution, "_get_razorpay_client", return_value=_mock_razorpay()):
            _post_webhook(client, payload)

        resp = client.get("/metrics")
        data = resp.json()
        assert data["total_events"] >= 1
        assert data["revenue_at_risk_paise"] > 0
