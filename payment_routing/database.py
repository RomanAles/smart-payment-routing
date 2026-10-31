"""Canonical SQLite schema, compatible with existing Ruby/Sequel databases."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import uuid
from .utils import ROOT

DEFAULT_PATH = ROOT / "db/operations.db"
CLEAR_ORDER = ("routing_attempts", "eligible_providers", "provider_skip_reasons", "routing_decisions", "operations_history", "operations_queue", "providers")
PROVIDER_COLUMNS = {
    "payment_system_id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "payment_system": "TEXT NOT NULL UNIQUE", "status": "TEXT NOT NULL DEFAULT 'active'",
    "traffic_percentage": "INTEGER NOT NULL", "priority": "INTEGER NOT NULL",
    **dict.fromkeys("limit_amount_min limit_amount_max daily_amount_limit daily_approved_amount in_progress_count_limit in_progress_count in_progress_amount_limit in_progress_amount available_requisites".split(), "INTEGER"),
    "conversion_24h": "REAL", "avg_latency_sec": "INTEGER", "banks": "TEXT DEFAULT '[]'",
    "exclude_banks": "BOOLEAN DEFAULT 0", "provider_margin_pct": "REAL", "merchant_margin_pct": "REAL",
    "allow_negative_agreement": "BOOLEAN DEFAULT 0", "note": "TEXT", "volume_share_pct": "REAL",
    "requests_per_minute_limit": "REAL", "daily_turnover_min": "INTEGER", "daily_turnover_max": "INTEGER",
    "preferred_range_min": "INTEGER", "preferred_range_max": "INTEGER",
}
QUEUE_COLUMNS = dict(operation_id="TEXT PRIMARY KEY", created_at="TIMESTAMP NOT NULL", amount="INTEGER NOT NULL", bank="TEXT NOT NULL", card_brand="TEXT", payout_requisite_sbp_phone="TEXT", payout_requisite_bank_name="TEXT")
HISTORY_COLUMNS = dict(operation_id="TEXT PRIMARY KEY", created_at="TIMESTAMP NOT NULL", amount="INTEGER NOT NULL", bank="TEXT NOT NULL", card_brand="TEXT", payment_system_id="INTEGER NOT NULL REFERENCES providers(payment_system_id)", status="TEXT NOT NULL", latency_sec="INTEGER")
DECISION_COLUMNS = dict(operation_id="TEXT PRIMARY KEY REFERENCES operations_queue(operation_id)", selected_payment_system_id="INTEGER NOT NULL REFERENCES providers(payment_system_id)", simulated_result="TEXT NOT NULL", latency_sec="INTEGER NOT NULL", created_at="TIMESTAMP NOT NULL")
ATTEMPT_COLUMNS = dict(attempt_id="INTEGER PRIMARY KEY AUTOINCREMENT", operation_id="TEXT NOT NULL REFERENCES routing_decisions(operation_id)", payment_system_id="INTEGER NOT NULL REFERENCES providers(payment_system_id)", attempt_number="INTEGER NOT NULL", decision="TEXT NOT NULL", reason="TEXT", created_at="TIMESTAMP NOT NULL")
ELIGIBLE_COLUMNS = dict(operation_id="TEXT NOT NULL REFERENCES operations_queue(operation_id)", payment_system_id="INTEGER NOT NULL REFERENCES providers(payment_system_id)", is_eligible="BOOLEAN NOT NULL", checked_at="TIMESTAMP NOT NULL")
SKIP_COLUMNS = dict(skip_reason_id="INTEGER PRIMARY KEY AUTOINCREMENT", operation_id="TEXT NOT NULL REFERENCES operations_queue(operation_id)", payment_system_id="INTEGER NOT NULL REFERENCES providers(payment_system_id)", reason="TEXT NOT NULL", created_at="TIMESTAMP NOT NULL")
TABLE_COLUMNS = {"operations_queue": QUEUE_COLUMNS, "providers": PROVIDER_COLUMNS, "operations_history": HISTORY_COLUMNS, "routing_decisions": DECISION_COLUMNS, "routing_attempts": ATTEMPT_COLUMNS, "eligible_providers": ELIGIBLE_COLUMNS, "provider_skip_reasons": SKIP_COLUMNS}
ADDITIONS = {"providers": {"daily_approved_date": "TEXT", "daily_utc_offset": "INTEGER"}, "routing_decisions": {"explanation": "TEXT"}, "routing_attempts": {"details": "TEXT", "dispatched_at": "TIMESTAMP"}}
STATS_COLUMNS = {**dict.fromkeys("actual_count_share_pct actual_volume_share_pct approved_volume_share_pct count_target_fulfillment_pct volume_target_fulfillment_pct count_share_gap_pp volume_share_gap_pp approval_rate_pct rejection_rate_pct expiration_rate_pct approved_amount_pct terminal_approval_rate_pct".split(), "REAL"), "stats_calculated_at": "TEXT", "stats_window_sec": "INTEGER"}


def connect(path=DEFAULT_PATH, readonly=False):
    if path is None:
        path = ":memory:"
    if readonly:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    else:
        db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if readonly:
        db.execute("PRAGMA query_only = ON")
    return db


@contextmanager
def transaction(db, immediate=False):
    # Savepoints preserve atomicity when import/log/state operations are nested.
    nested = db.in_transaction
    name = "sp_" + uuid.uuid4().hex
    db.execute(f"SAVEPOINT {name}" if nested else ("BEGIN IMMEDIATE" if immediate else "BEGIN"))
    try:
        yield db
        db.execute(f"RELEASE SAVEPOINT {name}" if nested else "COMMIT")
    except BaseException:
        if nested:
            db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            db.execute(f"RELEASE SAVEPOINT {name}")
        else:
            db.rollback()
        raise


def columns(db, table):
    return [row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')]


def create_schema(db):
    constraints = {
        "routing_attempts": ["UNIQUE(operation_id, payment_system_id)", "UNIQUE(operation_id, attempt_number)"],
        "eligible_providers": ["PRIMARY KEY(operation_id, payment_system_id)"],
        "provider_skip_reasons": ["UNIQUE(operation_id, payment_system_id, reason)"],
    }
    with transaction(db):
        for table, fields in TABLE_COLUMNS.items():
            definitions = [f'"{name}" {kind}' for name, kind in fields.items()] + constraints.get(table, [])
            db.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(definitions)})')
        for table, names in {
            "providers": ["payment_system", "status", "priority"],
            "operations_history": ["payment_system_id", "status", "created_at"],
            "routing_decisions": ["selected_payment_system_id", "created_at"],
            "routing_attempts": ["operation_id", "payment_system_id", "attempt_number", "decision", "created_at"],
            "eligible_providers": ["operation_id", "payment_system_id", "is_eligible"],
            "provider_skip_reasons": ["operation_id", "payment_system_id", "created_at"],
        }.items():
            for name in names:
                db.execute(f'CREATE INDEX IF NOT EXISTS "{table}_{name}_index" ON "{table}" ("{name}")')
        upgrade_schema(db)
    return db


def upgrade_schema(db):
    for table, fields in ADDITIONS.items():
        existing = columns(db, table)
        for name, kind in fields.items():
            if name not in existing:
                db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {kind}')


def upsert(db, table, values, key):
    if table not in TABLE_COLUMNS or not set(values) <= set(columns(db, table)):
        raise ValueError(f"Invalid columns for {table}")
    names = list(values)
    updates = [name for name in names if name != key]
    assignment = ", ".join(f'"{name}"=excluded."{name}"' for name in updates)
    quoted = ", ".join(f'"{name}"' for name in names)
    db.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({", ".join("?" for _ in names)}) ON CONFLICT("{key}") DO UPDATE SET {assignment}', list(values.values()))


def rows(db, sql, bindings=()):
    return [dict(row) for row in db.execute(sql, bindings)]


def clear_all(db):
    with transaction(db):
        for table in CLEAR_ORDER:
            db.execute(f'DELETE FROM "{table}"')
