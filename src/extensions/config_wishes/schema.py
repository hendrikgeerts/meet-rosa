"""Config-wishes: persistente opslag van the user's structurele
preferences/wensen die hij in chat met Rosa kenbaar maakt.

Achtergrond: Rosa antwoordde voorheen "Genoteerd!" zonder eindopslag.
Met deze tabel wordt elke "kun je voortaan ..." / "ik wil dat je ..."
echt vastgelegd, voor periodieke review (dayclose surface, dashboard
list).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

VALID_STATUS = ("open", "wip", "done", "dismissed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_wishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    source_handle TEXT,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','wip','done','dismissed')),
    resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_config_wishes_status
    ON config_wishes(status);
CREATE INDEX IF NOT EXISTS idx_config_wishes_created
    ON config_wishes(created_at);
"""


def init_config_wishes_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def insert_wish(
    conn: sqlite3.Connection, *,
    title: str, body: str | None = None,
    source_handle: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO config_wishes(title, body, source_handle) "
        "VALUES (?, ?, ?)",
        (title.strip()[:200], (body or "").strip() or None, source_handle),
    )
    return int(cur.lastrowid)


def list_wishes(
    conn: sqlite3.Connection, *,
    status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM config_wishes"
    params: list[Any] = []
    if status:
        if status not in VALID_STATUS:
            raise ValueError(f"invalid status: {status!r}")
        sql += " WHERE status = ?"
        params.append(status)
    sql += (" ORDER BY status='open' DESC, status='wip' DESC, "
             "created_at DESC LIMIT ?")
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_wish(
    conn: sqlite3.Connection, wish_id: int,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM config_wishes WHERE id=?", (wish_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def update_wish_status(
    conn: sqlite3.Connection, wish_id: int, status: str,
) -> bool:
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status!r}")
    resolved = "strftime('%s','now')" if status in ("done", "dismissed") else "NULL"
    cur = conn.execute(
        f"UPDATE config_wishes SET status=?, resolved_at={resolved} "
        f"WHERE id=?",
        (status, wish_id),
    )
    return cur.rowcount > 0


def count_open(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM config_wishes WHERE status IN ('open','wip')",
    ).fetchone()
    return int(row[0]) if row else 0


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "source_handle": r["source_handle"],
        "title": r["title"],
        "body": r["body"],
        "status": r["status"],
        "resolved_at": r["resolved_at"],
    }
