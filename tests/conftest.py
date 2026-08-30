"""
tests/conftest.py — Shared pytest fixtures for the ReCova test suite.

Provides fully isolated, per-test SQLite databases so tests never touch
data/recova.db (or any real batch-run data).

How it works:
  1. Each test that requests the ``test_db`` fixture gets a fresh temp-file DB.
  2. ``init_db(db_path=...)`` in recova/db.py sets a module-level override so
     every subsequent ``get_connection()`` call in that process uses the temp
     path. The fixture tears down by calling ``reset_db_path()`` after each test.
  3. Tests that do NOT use ``test_db`` (e.g. pure-function tests in
     test_classifier.py and test_decision_engine.py) are unaffected — they
     never call into the DB at all, so no override is needed.

Fixtures exported:
  test_db              — isolated DB (temp file), fresh per test
  sample_payment_event — inserts one payment_events row, returns its row id
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make recova importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# Core fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_db():
    """
    Create a fresh, isolated SQLite DB in a temp file for each test.
    Never touches data/recova.db.

    init_db(db_path=...) sets the module-level _override_path so all
    get_connection() calls transparently hit the temp DB for the duration
    of this test. reset_db_path() restores the default at teardown.
    """
    import recova.db as db_module

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # Redirect all DB I/O to the temp file
    db_module.init_db(db_path=path)

    yield path  # the path is rarely needed; isolation is the point

    # Teardown: reset override so subsequent tests use the real DB path
    db_module.reset_db_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass  # already removed


@pytest.fixture
def sample_payment_event(test_db):
    """
    Insert one minimal payment_events row for tests that need a starting point.
    Returns the new row id.

    Depends on test_db so the real DB is never touched.
    """
    import recova.db as db_module

    event_id = db_module.insert_payment_event({
        "razorpay_event_id": "evt_conftest_001",
        "payment_id": "pay_conftest_001",
        "subscription_id": "sub_conftest_001",
        "customer_id": "cust_conftest_001",
        "amount": 99900,          # ₹999 in paise
        "currency": "INR",
        "status": "failed",
        "error_code": "insufficient_funds",
        "error_description": "Insufficient funds.",
        "error_reason": "insufficient_funds",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "raw_payload": "{}",
    })
    return event_id
