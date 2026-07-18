"""decisions tabel — één rij per gelogde beslissing."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    attendees TEXT,                 -- JSON array van strings
    source_ref TEXT,                -- bv. 'plaud:meeting:7' / 'gmail:thread:abc' / 'manual'
    decided_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    logged_by TEXT NOT NULL DEFAULT 'owner',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','superseded','reverted')),
    tags TEXT                       -- JSON: {category, project_slugs:[]}
);
CREATE INDEX IF NOT EXISTS idx_decisions_decided ON decisions(decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
"""


def init_decisions_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Backfill: add tags column if missing (migrate older DBs)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "tags" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN tags TEXT")


def update_decision_tags(
    conn: sqlite3.Connection, decision_id: int, tags: dict[str, Any],
) -> bool:
    cur = conn.execute(
        "UPDATE decisions SET tags=? WHERE id=?",
        (json.dumps(tags, ensure_ascii=False), decision_id),
    )
    return cur.rowcount > 0


def insert_decision(
    conn: sqlite3.Connection,
    *,
    title: str, body: str,
    attendees: list[str] | None = None,
    source_ref: str | None = None,
    decided_at: int | None = None,
) -> int:
    if decided_at is not None:
        cur = conn.execute(
            "INSERT INTO decisions (title, body, attendees, source_ref, decided_at) "
            "VALUES (?,?,?,?,?)",
            (title, body,
             json.dumps(attendees or [], ensure_ascii=False),
             source_ref, decided_at),
        )
    else:
        cur = conn.execute(
            "INSERT INTO decisions (title, body, attendees, source_ref) "
            "VALUES (?,?,?,?)",
            (title, body,
             json.dumps(attendees or [], ensure_ascii=False),
             source_ref),
        )
    return cur.lastrowid or 0


def search_decisions(
    conn: sqlite3.Connection, *, query: str,
    days: int | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM decisions WHERE status = 'active'"
    params: list[Any] = []
    if query:
        sql += " AND (title LIKE ? OR body LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like])
    if days is not None:
        import time as _time
        sql += " AND decided_at >= ?"
        params.append(int(_time.time()) - days * 86400)
    sql += " ORDER BY decided_at DESC LIMIT ?"
    params.append(limit)
    conn.row_factory = sqlite3.Row
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def recent_decisions(
    conn: sqlite3.Connection, *, days: int = 7, limit: int = 20,
) -> list[dict[str, Any]]:
    return search_decisions(conn, query="", days=days, limit=limit)


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    try:
        attendees = json.loads(r["attendees"] or "[]")
    except (ValueError, TypeError):
        attendees = []
    try:
        tags = json.loads(r["tags"]) if r["tags"] else {}
    except (ValueError, TypeError, IndexError):
        tags = {}
    return {
        "id": r["id"],
        "title": r["title"],
        "body": r["body"],
        "attendees": attendees,
        "source_ref": r["source_ref"],
        "decided_at": r["decided_at"],
        "status": r["status"],
        "tags": tags,
    }


def supersede_decision(
    conn: sqlite3.Connection, decision_id: int, *, replaced_by: str,
) -> bool:
    cur = conn.execute(
        "UPDATE decisions SET status='superseded', body = body || "
        "'\n\n[SUPERSEDED] ' || ? WHERE id=? AND status='active'",
        (replaced_by, decision_id),
    )
    return cur.rowcount > 0
