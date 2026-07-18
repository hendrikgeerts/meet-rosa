"""Schema voor todoist-sync mapping-tabel."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS todoist_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_kind TEXT NOT NULL CHECK(local_kind IN ('reminder','open_loop')),
    local_id INTEGER NOT NULL,
    todoist_id TEXT NOT NULL,
    pushed_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_synced_at INTEGER,
    completed_at_remote INTEGER,
    UNIQUE(local_kind, local_id),
    UNIQUE(todoist_id)
);
CREATE INDEX IF NOT EXISTS idx_todoist_local
    ON todoist_links(local_kind, local_id);

CREATE TABLE IF NOT EXISTS todoist_push_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id INTEGER NOT NULL UNIQUE,        -- één queue-entry per open_loop
    kind TEXT NOT NULL,                     -- incoming_question / incoming_task / meeting_action_self
    label TEXT NOT NULL,                    -- rosa-mail / rosa-slack / rosa-meeting
    title TEXT NOT NULL,                    -- wat zou de Todoist-task heten
    due_at INTEGER,                         -- optionele deadline uit de loop
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','rejected')),
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    decided_at INTEGER,
    todoist_id TEXT                         -- gevuld na approve+push
);
CREATE INDEX IF NOT EXISTS idx_push_queue_state
    ON todoist_push_queue(state, created_at);
"""


def init_todoist_sync_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def get_link_by_local(
    conn: sqlite3.Connection, *, kind: str, local_id: int,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM todoist_links WHERE local_kind=? AND local_id=?",
        (kind, local_id),
    ).fetchone()
    return dict(row) if row else None


def get_link_by_remote(
    conn: sqlite3.Connection, *, todoist_id: str,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM todoist_links WHERE todoist_id=?",
        (todoist_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_link(
    conn: sqlite3.Connection, *, kind: str, local_id: int, todoist_id: str,
) -> bool:
    try:
        conn.execute(
            "INSERT INTO todoist_links (local_kind, local_id, todoist_id) "
            "VALUES (?,?,?)",
            (kind, local_id, todoist_id),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def mark_completed_remote(
    conn: sqlite3.Connection, *, todoist_id: str,
) -> None:
    conn.execute(
        "UPDATE todoist_links SET completed_at_remote=strftime('%s','now') "
        "WHERE todoist_id=?",
        (todoist_id,),
    )


def touch_synced(conn: sqlite3.Connection, *, todoist_id: str) -> None:
    conn.execute(
        "UPDATE todoist_links SET last_synced_at=strftime('%s','now') "
        "WHERE todoist_id=?",
        (todoist_id,),
    )


# --- todoist_push_queue helpers ---------------------------------------

def queue_enqueue_loop(
    conn: sqlite3.Connection, *,
    loop_id: int, kind: str, label: str, title: str,
    due_at: int | None = None,
) -> bool:
    """Insert open_loop in de review-queue. Stil-no-op als 'ie al bestaat
    (loop_id UNIQUE)."""
    try:
        conn.execute(
            "INSERT INTO todoist_push_queue "
            "(loop_id, kind, label, title, due_at) VALUES (?,?,?,?,?)",
            (loop_id, kind, label, title.strip()[:500], due_at),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def queue_list_pending(
    conn: sqlite3.Connection, *, limit: int = 30,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM todoist_push_queue WHERE state='pending' "
        "ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def queue_get(
    conn: sqlite3.Connection, queue_id: int,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM todoist_push_queue WHERE id=?", (queue_id,),
    ).fetchone()
    return dict(row) if row else None


def queue_mark_approved(
    conn: sqlite3.Connection, *,
    queue_id: int, todoist_id: str,
) -> None:
    conn.execute(
        "UPDATE todoist_push_queue SET state='approved', "
        "decided_at=strftime('%s','now'), todoist_id=? WHERE id=?",
        (todoist_id, queue_id),
    )


def queue_mark_rejected(
    conn: sqlite3.Connection, queue_id: int,
) -> None:
    conn.execute(
        "UPDATE todoist_push_queue SET state='rejected', "
        "decided_at=strftime('%s','now') WHERE id=?",
        (queue_id,),
    )


def queue_pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM todoist_push_queue WHERE state='pending'",
    ).fetchone()
    return int(row[0]) if row else 0
