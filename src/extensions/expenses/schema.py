"""expenses tabel — één rij per verwerkt receipt-PDF."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# Categorieën zijn intentionally beperkt — past op zakelijk Mac/SaaS bedrijf.
CATEGORIES = (
    "travel", "software", "hardware", "marketing", "office",
    "meals", "subcontractors", "other",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    vendor TEXT,
    receipt_date INTEGER,             -- unix seconds, datum op de bon
    amount_cents INTEGER,             -- inclusief BTW
    vat_cents INTEGER,                -- BTW-bedrag (kan 0)
    currency TEXT NOT NULL DEFAULT 'EUR',
    category TEXT,
    description TEXT,
    raw_text TEXT,                    -- eerste ~3000 chars van PDF, voor audit
    processed_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    confidence REAL                   -- 0.0-1.0 zelfgerapporteerd door Claude
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category);
"""


def init_expenses_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def prune_old_expenses(conn: sqlite3.Connection, *, days: int) -> int:
    """Drop expenses with `receipt_date` older than `days`. Default in
    settings is 2555 days (~7 years, NL fiscal retention requirement).
    Use a generous default so the accountant always has the trail.
    """
    if days <= 0:
        return 0
    cutoff_row = conn.execute(
        "SELECT strftime('%s','now') - ? * 86400", (days,),
    ).fetchone()
    cutoff = int(cutoff_row[0])
    cur = conn.execute(
        "DELETE FROM expenses WHERE receipt_date IS NOT NULL AND receipt_date < ?",
        (cutoff,),
    )
    return cur.rowcount or 0


def already_seen(conn: sqlite3.Connection, *, source_path: str, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM expenses WHERE source_path=? OR content_hash=? LIMIT 1",
        (source_path, content_hash),
    ).fetchone()
    return row is not None


def insert_expense(
    conn: sqlite3.Connection,
    *,
    source_path: str, content_hash: str,
    vendor: str | None, receipt_date: int | None,
    amount_cents: int | None, vat_cents: int | None,
    currency: str, category: str | None,
    description: str | None, raw_text: str | None,
    confidence: float | None,
) -> int | None:
    try:
        cur = conn.execute(
            """
            INSERT INTO expenses (source_path, content_hash, vendor, receipt_date,
                                   amount_cents, vat_cents, currency, category,
                                   description, raw_text, confidence)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_path, content_hash, vendor, receipt_date,
             amount_cents, vat_cents, currency, category,
             description, raw_text, confidence),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def list_recent(
    conn: sqlite3.Connection, *, days: int = 30,
    category: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    import time as _time
    cutoff = int(_time.time()) - days * 86400
    sql = (
        "SELECT * FROM expenses "
        "WHERE COALESCE(receipt_date, processed_at) >= ?"
    )
    params: list[Any] = [cutoff]
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY COALESCE(receipt_date, processed_at) DESC LIMIT ?"
    params.append(limit)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_for_period(
    conn: sqlite3.Connection, *, start_unix: int, end_unix: int,
) -> list[dict[str, Any]]:
    """Voor maandelijkse export."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM expenses "
        "WHERE COALESCE(receipt_date, processed_at) >= ? "
        "AND COALESCE(receipt_date, processed_at) < ? "
        "ORDER BY COALESCE(receipt_date, processed_at) ASC",
        (start_unix, end_unix),
    ).fetchall()
    return [dict(r) for r in rows]
