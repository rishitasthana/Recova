"""
recova/db.py — SQLite schema and connection helpers.

FK chain (audit trail): actions_log → decisions → classifications → payment_events
Every decision in this system is traceable end-to-end through this chain.

Test isolation:
  Call ``init_db(db_path="/tmp/test.db")`` (or any temp path) to redirect ALL
  subsequent DB calls in that process to the given path. Reset by calling
  ``reset_db_path()`` or by setting the module-level ``_override_path`` to None.
  The production ``DATABASE_PATH`` and ``data/recova.db`` are never touched by
  tests that use this mechanism.
"""

import sqlite3
import os
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/recova.db")

# Module-level override set by init_db(db_path=...) for test isolation.
# When non-None, all get_connection() calls use this path instead of DATABASE_PATH.
_override_path: str | None = None


def get_db_path() -> str:
    """Return the active DB path — override (test isolation) takes priority."""
    path = _override_path if _override_path is not None else DATABASE_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def reset_db_path() -> None:
    """Clear the test-isolation override, reverting to DATABASE_PATH."""
    global _override_path
    _override_path = None


@contextmanager
def get_connection():
    """Context manager yielding a SQLite connection with FK enforcement on."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # safe for concurrent readers
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DDL = """
-- ─────────────────────────────────────────────────────────────────────────────
-- Table 1: payment_events
-- Raw inbound webhook payloads. Root of the FK audit chain.
-- razorpay_event_id has a UNIQUE constraint — webhook idempotency guard.
-- A duplicate delivery is caught here and skipped, never processed twice.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payment_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    razorpay_event_id   TEXT    NOT NULL UNIQUE,  -- idempotency key
    payment_id          TEXT,
    subscription_id     TEXT,
    customer_id         TEXT,
    amount              INTEGER,                  -- in paise (1 INR = 100 paise)
    currency            TEXT    DEFAULT 'INR',
    status              TEXT,                     -- 'failed' | 'captured'
    error_code          TEXT,
    error_description   TEXT,
    error_reason        TEXT,
    error_source        TEXT,
    error_step          TEXT,
    raw_payload         TEXT,                     -- full JSON from Razorpay
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Table 2: classifications
-- One row per processed payment_event. Stores what was decided about the
-- decline type and why.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS classifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_event_id    INTEGER NOT NULL REFERENCES payment_events(id),
    classification      TEXT    NOT NULL CHECK(classification IN ('hard', 'soft', 'unknown')),
    confidence          REAL    NOT NULL,          -- 1.0 | 0.7 | 0.5
    reason              TEXT    NOT NULL,          -- human-readable lookup result
    error_code          TEXT,
    error_reason        TEXT,
    lookup_source       TEXT,                     -- 'confirmed_table' | 'fallback_table' | 'reason_substring' | 'default'
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Table 3: decisions
-- One row per classification. Stores which rule fired and why.
-- rule_triggered: short machine-readable name (e.g. "rule_2_hard_stop")
-- rule_explanation: the full one-sentence justification
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    classification_id   INTEGER NOT NULL REFERENCES classifications(id),
    decision            TEXT    NOT NULL CHECK(decision IN (
                            'retry_now', 'retry_scheduled',
                            'send_reminder', 'escalate_promise_to_pay', 'stop'
                        )),
    retry_at            TIMESTAMP,               -- set when decision = retry_scheduled
    rule_triggered      TEXT    NOT NULL,
    rule_explanation    TEXT    NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Table 4: actions_log
-- One row per executed action. Stores what was actually done and its outcome.
-- This is the leaf of the audit chain.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS actions_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         INTEGER NOT NULL REFERENCES decisions(id),
    action_type         TEXT    NOT NULL,         -- mirrors decision value
    payload             TEXT,                    -- JSON: what was sent (mock or real)
    outcome             TEXT    NOT NULL CHECK(outcome IN ('success', 'failure', 'api_error', 'skipped', 'logged')),
    outcome_detail      TEXT,                    -- raw API response or error message
    executed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common query patterns (dashboard + batch runner)
CREATE INDEX IF NOT EXISTS idx_events_subscription ON payment_events(subscription_id);
CREATE INDEX IF NOT EXISTS idx_events_payment ON payment_events(payment_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON payment_events(created_at);
CREATE INDEX IF NOT EXISTS idx_actions_type ON actions_log(action_type);
CREATE INDEX IF NOT EXISTS idx_actions_outcome ON actions_log(outcome);
CREATE INDEX IF NOT EXISTS idx_decisions_decision ON decisions(decision);
"""


def init_db(db_path: str | None = None) -> None:
    """
    Create all tables and indexes. Safe to call multiple times (idempotent).

    Args:
        db_path: Optional filesystem path for the SQLite database.
                 When supplied, ALL subsequent DB calls in this process are
                 redirected to this path until ``reset_db_path()`` is called.
                 Pass ``None`` (default) to use the path from DATABASE_PATH env
                 var (or ``data/recova.db`` if the env var is unset).
                 Typical use: ``init_db(db_path=tmp_path)`` inside a pytest
                 fixture for full test isolation.
    """
    global _override_path
    if db_path is not None:
        _override_path = db_path
    with get_connection() as conn:
        conn.executescript(DDL)
    print(f"[db] Database initialised at {get_db_path()}")


def get_retry_count(subscription_id: str) -> int:
    """
    Count prior retry attempts for a subscription from the audit log.

    Derived from actions_log joined back to payment_events — no separate
    counter column. The audit trail is the single source of truth.

    A 'retry attempt' = any actions_log row where action_type is
    'retry_now' or 'retry_scheduled' (regardless of outcome).
    """
    sql = """
        SELECT COUNT(*) as cnt
        FROM actions_log al
        JOIN decisions d      ON al.decision_id       = d.id
        JOIN classifications c ON d.classification_id = c.id
        JOIN payment_events pe ON c.payment_event_id  = pe.id
        WHERE pe.subscription_id = ?
          AND al.action_type IN ('retry_now', 'retry_scheduled')
    """
    with get_connection() as conn:
        row = conn.execute(sql, (subscription_id,)).fetchone()
        return row["cnt"] if row else 0


def insert_payment_event(event_data: dict) -> int | None:
    """
    Insert a payment event. Returns the new row id, or None if duplicate.
    The UNIQUE constraint on razorpay_event_id is the idempotency guard.
    """
    sql = """
        INSERT OR IGNORE INTO payment_events
            (razorpay_event_id, payment_id, subscription_id, customer_id,
             amount, currency, status, error_code, error_description,
             error_reason, error_source, error_step, raw_payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            event_data.get("razorpay_event_id"),
            event_data.get("payment_id"),
            event_data.get("subscription_id"),
            event_data.get("customer_id"),
            event_data.get("amount"),
            event_data.get("currency", "INR"),
            event_data.get("status"),
            event_data.get("error_code"),
            event_data.get("error_description"),
            event_data.get("error_reason"),
            event_data.get("error_source"),
            event_data.get("error_step"),
            event_data.get("raw_payload"),
        ))
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid
        return None  # duplicate — IGNORE fired


def get_payment_event(event_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM payment_events WHERE id = ?", (event_id,)
        ).fetchone()


def insert_classification(data: dict) -> int:
    sql = """
        INSERT INTO classifications
            (payment_event_id, classification, confidence, reason,
             error_code, error_reason, lookup_source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            data["payment_event_id"],
            data["classification"],
            data["confidence"],
            data["reason"],
            data.get("error_code"),
            data.get("error_reason"),
            data.get("lookup_source", "unknown"),
        ))
        return cur.lastrowid


def get_classification(classification_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM classifications WHERE id = ?", (classification_id,)
        ).fetchone()


def insert_decision(data: dict) -> int:
    sql = """
        INSERT INTO decisions
            (classification_id, decision, retry_at, rule_triggered, rule_explanation)
        VALUES (?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            data["classification_id"],
            data["decision"],
            data.get("retry_at"),
            data["rule_triggered"],
            data["rule_explanation"],
        ))
        return cur.lastrowid


def get_decision(decision_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()


def insert_action_log(data: dict) -> int:
    sql = """
        INSERT INTO actions_log
            (decision_id, action_type, payload, outcome, outcome_detail)
        VALUES (?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cur = conn.execute(sql, (
            data["decision_id"],
            data["action_type"],
            data.get("payload"),
            data["outcome"],
            data.get("outcome_detail"),
        ))
        return cur.lastrowid


def get_full_pipeline_view(limit: int = 100) -> list[sqlite3.Row]:
    """
    Returns the full event → classification → decision → action chain
    as a flat view. Used by the dashboard and batch runner.
    """
    sql = """
        SELECT
            pe.id            AS event_id,
            pe.payment_id,
            pe.subscription_id,
            pe.amount,
            pe.currency,
            pe.status,
            pe.error_code,
            pe.error_reason,
            pe.created_at    AS event_time,
            c.classification,
            c.confidence,
            c.reason         AS classification_reason,
            c.lookup_source,
            d.decision,
            d.rule_triggered,
            d.rule_explanation,
            d.retry_at,
            al.action_type,
            al.outcome,
            al.outcome_detail,
            al.executed_at
        FROM payment_events pe
        LEFT JOIN classifications c  ON c.payment_event_id  = pe.id
        LEFT JOIN decisions d        ON d.classification_id = c.id
        LEFT JOIN actions_log al     ON al.decision_id      = d.id
        WHERE pe.status = 'failed'
        ORDER BY pe.created_at DESC
        LIMIT ?
    """
    with get_connection() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def get_summary_metrics() -> dict:
    """Compute recovery metrics across all processed events."""
    with get_connection() as conn:
        total_events = conn.execute(
            "SELECT COUNT(*) FROM payment_events WHERE status = 'failed'"
        ).fetchone()[0]

        revenue_at_risk = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payment_events WHERE status = 'failed'"
        ).fetchone()[0]

        # Recovered = retry actions that returned outcome='success'
        recovered_row = conn.execute("""
            SELECT COALESCE(SUM(pe.amount), 0)
            FROM actions_log al
            JOIN decisions d      ON al.decision_id       = d.id
            JOIN classifications c ON d.classification_id = c.id
            JOIN payment_events pe ON c.payment_event_id  = pe.id
            WHERE al.action_type IN ('retry_now', 'retry_scheduled')
              AND al.outcome = 'success'
        """).fetchone()
        recovered = recovered_row[0] if recovered_row else 0

        decision_counts = conn.execute("""
            SELECT decision, COUNT(*) as cnt
            FROM decisions
            GROUP BY decision
        """).fetchall()

        classification_counts = conn.execute("""
            SELECT classification, COUNT(*) as cnt
            FROM classifications
            GROUP BY classification
        """).fetchall()

        recovery_by_type = conn.execute("""
            SELECT
                c.classification,
                COUNT(DISTINCT pe.id)              AS total,
                COALESCE(SUM(pe.amount), 0)        AS at_risk,
                COALESCE(SUM(CASE WHEN al.outcome='success'
                               AND al.action_type IN ('retry_now','retry_scheduled')
                               THEN pe.amount ELSE 0 END), 0) AS recovered
            FROM payment_events pe
            LEFT JOIN classifications c  ON c.payment_event_id = pe.id
            LEFT JOIN decisions d        ON d.classification_id = c.id
            LEFT JOIN actions_log al     ON al.decision_id = d.id
            WHERE pe.status = 'failed'
            GROUP BY c.classification
        """).fetchall()

    return {
        "total_events": total_events,
        "revenue_at_risk_paise": revenue_at_risk,
        "recovered_paise": recovered,
        "recovery_rate": round(recovered / revenue_at_risk, 4) if revenue_at_risk > 0 else 0.0,
        "decision_counts": {r["decision"]: r["cnt"] for r in decision_counts},
        "classification_counts": {r["classification"]: r["cnt"] for r in classification_counts},
        "recovery_by_type": [dict(r) for r in recovery_by_type],
    }
