"""Reminders: Claude can set them as a tool, a scheduler thread fires them via iMessage."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_TZ = ZoneInfo("Europe/Amsterdam")


SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL,
    remind_at INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    sent_at INTEGER,
    cancelled_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(remind_at) WHERE sent_at IS NULL AND cancelled_at IS NULL;
"""


def init_reminders_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def add_reminder(conn: sqlite3.Connection, *, handle: str, remind_at: datetime, body: str) -> int:
    ts = int(remind_at.astimezone(DEFAULT_TZ).timestamp())
    cur = conn.execute(
        "INSERT INTO reminders (handle, remind_at, body) VALUES (?, ?, ?)",
        (handle, ts, body),
    )
    rid = cur.lastrowid
    assert rid is not None
    log.info("reminder #%d queued for %s to %s: %s", rid, remind_at.isoformat(), handle, body[:60])
    return rid


def list_pending(
    conn: sqlite3.Connection,
    handle: str | None = None,
    *,
    include_history: bool = False,
    history_days: int = 30,
    query: str | None = None,
) -> list[dict]:
    """List reminders. Default: alleen pending (niet sent/cancelled).
    Bij include_history=True ook sent+cancelled in laatste history_days
    — handig voor 'wat was mijn ordernummer' Q&A.
    Bij query: extra LIKE-filter op body."""
    sql = "SELECT id, handle, remind_at, body, sent_at, cancelled_at FROM reminders WHERE 1=1"
    params: list[object] = []
    if not include_history:
        sql += " AND sent_at IS NULL AND cancelled_at IS NULL"
    else:
        # Cap historie zodat lijst niet eindeloos groeit.
        import time as _time
        sql += " AND remind_at >= ?"
        params.append(int(_time.time()) - history_days * 86400)
    if handle:
        sql += " AND handle = ?"
        params.append(handle)
    if query:
        sql += " AND body LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY remind_at DESC"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        status = (
            "cancelled" if r["cancelled_at"] else
            "sent" if r["sent_at"] else "pending"
        )
        out.append({
            "id": r["id"],
            "handle": r["handle"],
            "remind_at": datetime.fromtimestamp(r["remind_at"], DEFAULT_TZ).isoformat(),
            "body": r["body"],
            "status": status,
        })
    return out


def cancel_reminder(conn: sqlite3.Connection, reminder_id: int) -> bool:
    cur = conn.execute(
        "UPDATE reminders SET cancelled_at = strftime('%s','now') "
        "WHERE id = ? AND sent_at IS NULL AND cancelled_at IS NULL",
        (reminder_id,),
    )
    return cur.rowcount > 0


def due_now(conn: sqlite3.Connection) -> list[dict]:
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, handle, body FROM reminders "
        "WHERE remind_at <= ? AND sent_at IS NULL AND cancelled_at IS NULL "
        "ORDER BY remind_at ASC LIMIT 20",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_sent(conn: sqlite3.Connection, reminder_id: int) -> None:
    conn.execute(
        "UPDATE reminders SET sent_at = strftime('%s','now') WHERE id = ?",
        (reminder_id,),
    )
