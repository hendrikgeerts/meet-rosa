"""SQLite schema voor comm-intel: comm_items (één rij per binnengekomen of
verzonden bericht) en comm_ingest_state (per source/account/folder hoe ver
we zijn met polleren).

Body's worden hier full-text bewaard zodat lokaal terugzoeken werkt.
Samenvatting/intent/sentiment komen van de lokale Ollama-summarizer."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS comm_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- 'gmail' | 'imap' | 'slack'
    account TEXT NOT NULL,           -- yaml-key for IMAP/Slack; 'gmail' for Gmail
    external_id TEXT NOT NULL,       -- Gmail msg-id / IMAP UID / Slack ts
    folder TEXT,                     -- IMAP folder / Slack channel name
    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
    from_addr TEXT,
    to_addrs TEXT,                   -- JSON array of strings
    cc_addrs TEXT,                   -- JSON array of strings
    subject TEXT,
    occurred_at INTEGER NOT NULL,    -- unix seconds
    ingested_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    body_full TEXT NOT NULL,
    summary TEXT,                    -- lokaal gegenereerde samenvatting (Ollama)
    intent TEXT,                     -- question/task/fyi/newsletter/social/other
    sentiment TEXT,                  -- positive/neutral/negative/urgent
    thread_ref TEXT,                 -- Gmail threadId / IMAP References / Slack thread_ts
    raw_meta TEXT                    -- JSON met overige bronspecifieke velden
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comm_external
    ON comm_items(source, account, external_id);
CREATE INDEX IF NOT EXISTS idx_comm_occurred
    ON comm_items(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_comm_from
    ON comm_items(from_addr);
CREATE INDEX IF NOT EXISTS idx_comm_thread
    ON comm_items(thread_ref);

CREATE TABLE IF NOT EXISTS comm_ingest_state (
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    last_external_id TEXT,
    last_occurred_at INTEGER,
    last_polled_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (source, account, folder)
);
"""


def init_comm_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


@dataclass
class CommItem:
    source: str
    account: str
    external_id: str
    direction: str                          # 'in' | 'out'
    occurred_at: int
    body_full: str
    folder: str | None = None
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    thread_ref: str | None = None
    raw_meta: dict[str, Any] = field(default_factory=dict)


def insert_item(
    conn: sqlite3.Connection,
    item: CommItem,
    *,
    summary: str | None = None,
    intent: str | None = None,
    sentiment: str | None = None,
) -> int | None:
    """Insert a CommItem. Returns row id, or None if duplicate (already seen)."""
    try:
        cur = conn.execute(
            """
            INSERT INTO comm_items (
              source, account, external_id, folder, direction,
              from_addr, to_addrs, cc_addrs, subject, occurred_at,
              body_full, summary, intent, sentiment, thread_ref, raw_meta
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item.source, item.account, item.external_id, item.folder, item.direction,
                item.from_addr,
                json.dumps(item.to_addrs, ensure_ascii=False),
                json.dumps(item.cc_addrs, ensure_ascii=False),
                item.subject, item.occurred_at,
                item.body_full, summary, intent, sentiment, item.thread_ref,
                json.dumps(item.raw_meta, ensure_ascii=False, default=str),
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already-seen (source, account, external_id)


def load_state(
    conn: sqlite3.Connection,
    *, source: str, account: str, folder: str = "",
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT last_external_id, last_occurred_at, last_polled_at "
        "FROM comm_ingest_state WHERE source=? AND account=? AND folder=?",
        (source, account, folder),
    ).fetchone()
    if not row:
        return None
    return {
        "last_external_id": row[0],
        "last_occurred_at": row[1],
        "last_polled_at": row[2],
    }


def upsert_state(
    conn: sqlite3.Connection,
    *, source: str, account: str, folder: str = "",
    last_external_id: str | None = None,
    last_occurred_at: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO comm_ingest_state (source, account, folder, last_external_id,
                                       last_occurred_at, last_polled_at)
        VALUES (?,?,?,?,?, strftime('%s','now'))
        ON CONFLICT(source, account, folder) DO UPDATE SET
          last_external_id = COALESCE(excluded.last_external_id, comm_ingest_state.last_external_id),
          last_occurred_at = COALESCE(excluded.last_occurred_at, comm_ingest_state.last_occurred_at),
          last_polled_at = strftime('%s','now')
        """,
        (source, account, folder, last_external_id, last_occurred_at),
    )


def item_exists(
    conn: sqlite3.Connection,
    *, source: str, account: str, external_id: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comm_items WHERE source=? AND account=? AND external_id=? LIMIT 1",
        (source, account, external_id),
    ).fetchone()
    return row is not None


def prune_old_comm_items(conn: sqlite3.Connection, *, days: int) -> int:
    """Drop comm_items + their embeddings + dependent pending_proposals
    older than `days`. Returns the number of comm_items rows removed.

    Embeddings live in the `comm_embeddings` vec0 virtual table keyed by
    rowid = comm_items.id; we delete those explicitly because vec0
    doesn't honour SQLite foreign-key cascades. pending_proposals has a
    soft FK to comm_items(id); cleaning them avoids dangling refs.

    ISO 27001 A.18.1.3 — minimal data storage.
    """
    if days <= 0:
        return 0
    cutoff_row = conn.execute(
        "SELECT strftime('%s','now') - ? * 86400", (days,),
    ).fetchone()
    cutoff = int(cutoff_row[0])

    try:
        conn.execute(
            "DELETE FROM pending_proposals "
            "WHERE comm_item_id IN ("
            "  SELECT id FROM comm_items WHERE occurred_at < ?"
            ")",
            (cutoff,),
        )
    except sqlite3.OperationalError:
        pass  # table absent — fine, nothing to clean

    try:
        conn.execute(
            "DELETE FROM comm_embeddings "
            "WHERE rowid IN ("
            "  SELECT id FROM comm_items WHERE occurred_at < ?"
            ")",
            (cutoff,),
        )
    except sqlite3.OperationalError:
        pass  # vec0 not loaded / table absent (test DBs etc.)

    cur = conn.execute(
        "DELETE FROM comm_items WHERE occurred_at < ?",
        (cutoff,),
    )
    return cur.rowcount or 0
