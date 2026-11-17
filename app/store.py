import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import DATABASE_PATH, TIMEZONE

LOCAL_TZ = timezone(timedelta(hours=8)) if TIMEZONE == "Asia/Shanghai" else timezone.utc


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _raw_connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def connect():
    """Open an autocommit connection with the schema guaranteed to exist."""
    db = _raw_connect()
    for statement in SCHEMA:
        db.execute(statement)
    return db


@contextmanager
def transaction():
    """Open an explicit write transaction.

    Every statement inside the block is either committed together or rolled
    back together.  This is what makes a head switch and a batch switch one
    atomic operation.
    """
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()


SCHEMA = (
    # Legacy content-addressed run ledger.  Kept for backwards compatibility.
    "CREATE TABLE IF NOT EXISTS runs ("
    "run_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, "
    "accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL)",

    # Immutable snapshots of the three facts a reconciliation depends on.
    "CREATE TABLE IF NOT EXISTS dataset_versions ("
    "version_id TEXT PRIMARY KEY, scope TEXT NOT NULL, version_no INTEGER NOT NULL, "
    "input_sha256 TEXT NOT NULL, rule_version TEXT NOT NULL, created_at TEXT NOT NULL, "
    "UNIQUE(scope, version_no), UNIQUE(scope, input_sha256))",
    "CREATE TABLE IF NOT EXISTS dataset_lines ("
    "version_id TEXT NOT NULL, doc_type TEXT NOT NULL, doc_no TEXT NOT NULL, "
    "line_no INTEGER NOT NULL, sku TEXT NOT NULL, tax_rate TEXT NOT NULL, "
    "qty TEXT NOT NULL, amount TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'normal', "
    "PRIMARY KEY(version_id, doc_type, doc_no, line_no))",
    "CREATE TABLE IF NOT EXISTS dataset_heads ("
    "scope TEXT PRIMARY KEY, version_id TEXT NOT NULL, updated_at TEXT NOT NULL)",

    # A batch is bound to the order/acceptance/tax versions it was received at.
    "CREATE TABLE IF NOT EXISTS batches ("
    "batch_id TEXT PRIMARY KEY, version_no INTEGER NOT NULL UNIQUE, "
    "input_sha256 TEXT NOT NULL, input_json TEXT NOT NULL, registry_sha256 TEXT NOT NULL DEFAULT '', "
    "rule_version TEXT NOT NULL, order_version TEXT NOT NULL, "
    "acceptance_version TEXT NOT NULL, tax_version TEXT NOT NULL, "
    "status TEXT NOT NULL, checkpoint INTEGER NOT NULL DEFAULT 0, total_items INTEGER NOT NULL DEFAULT 0, "
    "accepted INTEGER NOT NULL DEFAULT 0, quarantined INTEGER NOT NULL DEFAULT 0, "
    "posted INTEGER NOT NULL DEFAULT 0, carried INTEGER NOT NULL DEFAULT 0, "
    "created_at TEXT NOT NULL, committed_at TEXT)",
    "CREATE TABLE IF NOT EXISTS batch_plan ("
    "batch_id TEXT NOT NULL, seq INTEGER NOT NULL, item_key TEXT NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(batch_id, seq), UNIQUE(batch_id, item_key))",
    "CREATE TABLE IF NOT EXISTS batch_items ("
    "batch_id TEXT NOT NULL, seq INTEGER NOT NULL, item_key TEXT NOT NULL, "
    "doc_type TEXT NOT NULL, doc_no TEXT NOT NULL, line_no INTEGER NOT NULL, "
    "business_key TEXT NOT NULL, status TEXT NOT NULL, reason_code TEXT, "
    "amount TEXT NOT NULL, qty TEXT NOT NULL, posted INTEGER NOT NULL DEFAULT 0, "
    "evidence TEXT, PRIMARY KEY(batch_id, seq), UNIQUE(batch_id, item_key))",
    "CREATE TABLE IF NOT EXISTS batch_differences ("
    "batch_id TEXT NOT NULL, business_key TEXT NOT NULL, line_no INTEGER NOT NULL, "
    "reason_code TEXT NOT NULL, amount_cny TEXT NOT NULL, qty TEXT NOT NULL DEFAULT '0', "
    "PRIMARY KEY(batch_id, business_key, line_no, reason_code))",
    "CREATE TABLE IF NOT EXISTS batch_current ("
    "scope TEXT PRIMARY KEY, batch_id TEXT NOT NULL)",

    # Invoice evidence is pinned to the three-way criteria at registration
    # and is never updated afterwards.
    "CREATE TABLE IF NOT EXISTS invoice_registrations ("
    "invoice_no TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, rule_version TEXT NOT NULL, "
    "order_version TEXT NOT NULL, acceptance_version TEXT NOT NULL, tax_version TEXT NOT NULL, "
    "business_key TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL, "
    "evidence_sha256 TEXT NOT NULL, batch_id TEXT NOT NULL, registered_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ledger_entries ("
    "doc_type TEXT NOT NULL, doc_no TEXT NOT NULL, line_no INTEGER NOT NULL, "
    "business_key TEXT NOT NULL, amount TEXT NOT NULL, qty TEXT NOT NULL, batch_id TEXT NOT NULL, "
    "order_version TEXT NOT NULL, acceptance_version TEXT NOT NULL, tax_version TEXT NOT NULL, "
    "posted_at TEXT NOT NULL, PRIMARY KEY(doc_type, doc_no, line_no))",

    # Audit trail for order revisions (committed or refused).
    "CREATE TABLE IF NOT EXISTS order_revisions ("
    "revision_id TEXT PRIMARY KEY, order_no TEXT NOT NULL, from_version TEXT, to_version TEXT, "
    "batch_id TEXT, status TEXT NOT NULL, reason_code TEXT, message TEXT, created_at TEXT NOT NULL)",
)


def init_db():
    with connect() as db:
        for statement in SCHEMA:
            db.execute(statement)


def record_run(manifest: dict) -> bool:
    init_db()
    with connect() as db:
        before = db.total_changes
        db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)", (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]))
        return db.total_changes > before


def run_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
