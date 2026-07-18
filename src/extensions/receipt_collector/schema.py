"""Receipt-collector schemas.

Drie tabellen:
  - receipt_runs           — één rij per kwartaal-batch
  - receipt_run_items      — één rij per Excel-transactie binnen een run
  - vendor_strategies      — geheugen: per vendor 'waar komen de bonnen vandaan'
                              (mail-from-pattern of portal-instructie). Groeit
                              elke kwartaal-run als the user nieuwe vendors
                              uitlegt — volgende keer minder handwerk.
"""
from __future__ import annotations

import json
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

VALID_RUN_STATUS = ("running", "needs_input", "completed", "failed")
VALID_ITEM_STATUS = (
    "pending",          # niet gezocht
    "matched",          # bon gevonden in mail
    "needs_portal",     # vendor zit in portal-only — the user moet downloaden
    "unknown_vendor",   # geen strategie + geen mail-match
    "manual_resolved",  # the user heeft handmatig de PDF in run-folder gezet
    "physical_only",    # vendor levert alleen fysieke bonnetjes — geen mail-search
    "ignored",          # vendor expliciet uitgesloten (test-subscription, etc.)
)
VALID_SOURCE_KIND = ("email", "portal", "manual", "physical", "ignore")


SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    excel_path TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    period_label TEXT,                  -- bv. 'Q2-2026' (afgeleid uit excel-naam)
    started_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    completed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running','needs_input','completed','failed')),
    transaction_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    needs_portal_count INTEGER NOT NULL DEFAULT 0,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    date_window_start INTEGER,          -- afgeleid uit excel: oudste txn - marge
    date_window_end INTEGER,            -- afgeleid uit excel: jongste txn + marge
    notes TEXT
);

CREATE TABLE IF NOT EXISTS receipt_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES receipt_runs(id) ON DELETE CASCADE,
    row_idx INTEGER NOT NULL,
    transaction_date INTEGER NOT NULL,  -- unix ts
    vendor_raw TEXT NOT NULL,           -- exact zoals in excel
    vendor_canonical TEXT,              -- gematcht aan vendor_strategies.name
    amount_cents INTEGER NOT NULL,      -- euro * 100, signed
    currency TEXT NOT NULL DEFAULT 'EUR',
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','matched','needs_portal',
                          'unknown_vendor','manual_resolved',
                          'physical_only','ignored')),
    matched_via TEXT,                   -- 'gmail' | 'imap:hendrikdpm' | 'manual' | etc
    match_score REAL,                   -- 0.0-1.0 confidence
    attachment_path TEXT,               -- relatief tov output_dir
    source_message_id TEXT,             -- Gmail msg-id of IMAP UID@account
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_items_run ON receipt_run_items(run_id);
CREATE INDEX IF NOT EXISTS idx_run_items_status ON receipt_run_items(status);

CREATE TABLE IF NOT EXISTS vendor_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    aliases TEXT,                       -- JSON array of strings (matchen op excel-vendor)
    source_kind TEXT NOT NULL
        CHECK(source_kind IN ('email','portal','manual','physical','ignore')),
    email_query_hint TEXT,              -- bv. 'from:billing@aws.amazon.com'
    portal_url TEXT,
    portal_notes TEXT,                  -- vrije tekst: 'log in → Billing → Invoices'
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_vendor_name ON vendor_strategies(name);

CREATE TABLE IF NOT EXISTS pdf_request_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name TEXT NOT NULL,
    sent_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    to_address TEXT NOT NULL,
    subject TEXT,
    body_excerpt TEXT,
    gmail_message_id TEXT,
    UNIQUE(vendor_name, sent_at)
);
CREATE INDEX IF NOT EXISTS idx_pdf_request_vendor
    ON pdf_request_sent(vendor_name, sent_at);
"""


def init_receipt_collector_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_check_constraints(conn)


def _migrate_check_constraints(conn: sqlite3.Connection) -> None:
    """In-place table-rebuild voor bestaande databases waar de CHECK
    constraints nog de oude enum-waarden hebben. Idempotent: detecteert
    op basis van de stored CREATE-SQL of migratie nodig is."""
    _maybe_rebuild_with_check(
        conn,
        table="vendor_strategies",
        marker="'physical'",  # nieuw enum-element dat in nieuwe schema zit
        old_columns_select="id, name, aliases, source_kind, email_query_hint, "
                            "portal_url, portal_notes, created_at, updated_at, "
                            "last_used_at",
        new_table_sql="""
            CREATE TABLE vendor_strategies_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                aliases TEXT,
                source_kind TEXT NOT NULL
                    CHECK(source_kind IN ('email','portal','manual','physical','ignore')),
                email_query_hint TEXT,
                portal_url TEXT,
                portal_notes TEXT,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                last_used_at INTEGER
            )
        """,
        post_indexes=["CREATE INDEX IF NOT EXISTS idx_vendor_name "
                       "ON vendor_strategies(name)"],
    )
    _maybe_rebuild_with_check(
        conn,
        table="receipt_run_items",
        marker="'physical_only'",
        old_columns_select="id, run_id, row_idx, transaction_date, vendor_raw, "
                            "vendor_canonical, amount_cents, currency, "
                            "description, status, matched_via, match_score, "
                            "attachment_path, source_message_id, notes",
        new_table_sql="""
            CREATE TABLE receipt_run_items_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES receipt_runs(id) ON DELETE CASCADE,
                row_idx INTEGER NOT NULL,
                transaction_date INTEGER NOT NULL,
                vendor_raw TEXT NOT NULL,
                vendor_canonical TEXT,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'EUR',
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','matched','needs_portal',
                                      'unknown_vendor','manual_resolved',
                                      'physical_only','ignored')),
                matched_via TEXT,
                match_score REAL,
                attachment_path TEXT,
                source_message_id TEXT,
                notes TEXT
            )
        """,
        post_indexes=[
            "CREATE INDEX IF NOT EXISTS idx_run_items_run "
            "ON receipt_run_items(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_run_items_status "
            "ON receipt_run_items(status)",
        ],
    )


def _maybe_rebuild_with_check(
    conn: sqlite3.Connection, *,
    table: str, marker: str, old_columns_select: str,
    new_table_sql: str, post_indexes: list[str],
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or row[0] is None:
        return
    if marker in row[0]:
        return  # already migrated
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        conn.executescript(new_table_sql.strip())
        conn.execute(
            f"INSERT INTO {table}_new ({old_columns_select}) "
            f"SELECT {old_columns_select} FROM {table}"
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        for ix in post_indexes:
            conn.execute(ix)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


# --- runs ----------------------------------------------------------------

def insert_run(
    conn: sqlite3.Connection, *,
    excel_path: str, output_dir: str, period_label: str | None,
    date_window_start: int, date_window_end: int,
    transaction_count: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO receipt_runs (excel_path, output_dir, period_label, "
        "transaction_count, date_window_start, date_window_end) "
        "VALUES (?,?,?,?,?,?)",
        (excel_path, output_dir, period_label, transaction_count,
         date_window_start, date_window_end),
    )
    return cur.lastrowid or 0


def update_run_counts(
    conn: sqlite3.Connection, run_id: int, *,
    matched: int, needs_portal: int, unknown: int,
    status: str, completed: bool = False,
) -> None:
    completed_at = int(_time.time()) if completed else None
    conn.execute(
        "UPDATE receipt_runs SET matched_count=?, needs_portal_count=?, "
        "unknown_count=?, status=?, completed_at=COALESCE(?, completed_at) "
        "WHERE id=?",
        (matched, needs_portal, unknown, status, completed_at, run_id),
    )


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM receipt_runs WHERE id=?",
                        (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(
    conn: sqlite3.Connection, *, limit: int = 20,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM receipt_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- run items ------------------------------------------------------------

def insert_run_item(
    conn: sqlite3.Connection, *,
    run_id: int, row_idx: int, transaction_date: int,
    vendor_raw: str, amount_cents: int, currency: str = "EUR",
    description: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO receipt_run_items (run_id, row_idx, transaction_date, "
        "vendor_raw, amount_cents, currency, description) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, row_idx, transaction_date, vendor_raw,
         amount_cents, currency, description),
    )
    return cur.lastrowid or 0


def update_run_item(
    conn: sqlite3.Connection, item_id: int, *,
    status: str | None = None,
    matched_via: str | None = None,
    match_score: float | None = None,
    attachment_path: str | None = None,
    source_message_id: str | None = None,
    vendor_canonical: str | None = None,
    notes: str | None = None,
) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    for k, v in (
        ("status", status), ("matched_via", matched_via),
        ("match_score", match_score), ("attachment_path", attachment_path),
        ("source_message_id", source_message_id),
        ("vendor_canonical", vendor_canonical), ("notes", notes),
    ):
        if v is not None:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return False
    params.append(item_id)
    cur = conn.execute(
        f"UPDATE receipt_run_items SET {', '.join(sets)} WHERE id=?",
        params,
    )
    return cur.rowcount > 0


def list_run_items(
    conn: sqlite3.Connection, run_id: int,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM receipt_run_items WHERE run_id=? ORDER BY row_idx ASC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- vendor strategies ----------------------------------------------------

def upsert_vendor_strategy(
    conn: sqlite3.Connection, *,
    name: str, source_kind: str,
    aliases: list[str] | None = None,
    email_query_hint: str | None = None,
    portal_url: str | None = None,
    portal_notes: str | None = None,
) -> int:
    if source_kind not in VALID_SOURCE_KIND:
        raise ValueError(f"invalid source_kind: {source_kind!r}")
    aliases_json = json.dumps(aliases or [], ensure_ascii=False)
    now = int(_time.time())
    cur = conn.execute(
        "INSERT INTO vendor_strategies (name, aliases, source_kind, "
        "email_query_hint, portal_url, portal_notes, updated_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "aliases=excluded.aliases, source_kind=excluded.source_kind, "
        "email_query_hint=excluded.email_query_hint, "
        "portal_url=excluded.portal_url, "
        "portal_notes=excluded.portal_notes, updated_at=excluded.updated_at",
        (name, aliases_json, source_kind, email_query_hint,
         portal_url, portal_notes, now),
    )
    return cur.lastrowid or _vendor_id_by_name(conn, name) or 0


def _vendor_id_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM vendor_strategies WHERE name=?",
                        (name,)).fetchone()
    return int(row[0]) if row else None


def find_vendor_strategy(
    conn: sqlite3.Connection, *, vendor_text: str,
) -> dict[str, Any] | None:
    """Match excel-vendor-text tegen canonical name OR aliases (LIKE)."""
    conn.row_factory = sqlite3.Row
    text = vendor_text.strip().lower()
    if not text:
        return None
    rows = conn.execute("SELECT * FROM vendor_strategies").fetchall()
    for r in rows:
        if text == r["name"].lower():
            return _vendor_to_dict(r)
        try:
            aliases = [a.lower() for a in json.loads(r["aliases"] or "[]")]
        except (ValueError, TypeError):
            aliases = []
        if any(a in text or text in a for a in aliases if a):
            return _vendor_to_dict(r)
    return None


def list_vendor_strategies(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM vendor_strategies ORDER BY name ASC",
    ).fetchall()
    return [_vendor_to_dict(r) for r in rows]


def mark_vendor_used(conn: sqlite3.Connection, vendor_id: int) -> None:
    conn.execute(
        "UPDATE vendor_strategies SET last_used_at=? WHERE id=?",
        (int(_time.time()), vendor_id),
    )


def _vendor_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    try:
        aliases = json.loads(r["aliases"] or "[]")
    except (ValueError, TypeError):
        aliases = []
    return {
        "id": r["id"],
        "name": r["name"],
        "aliases": aliases,
        "source_kind": r["source_kind"],
        "email_query_hint": r["email_query_hint"],
        "portal_url": r["portal_url"],
        "portal_notes": r["portal_notes"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "last_used_at": r["last_used_at"],
    }
