"""Open-loops schema en helpers.

Eén rij per "iets te doen" item ongeacht herkomst — comm_intel inkomende
vraag, plaud_intel meeting-actiepunt, of een toekomstige delegate-tracker.
Status-machine: open → done/ignored/snoozed.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS open_loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                  -- 'comm' | 'plaud' | 'manual'
    source_ref TEXT,                       -- id (string) van het bronitem
    kind TEXT NOT NULL,                    -- 'incoming_question' | 'incoming_task'
                                           --  | 'meeting_action_self' | 'meeting_action_other'
                                           --  | 'outgoing_request'
    who TEXT,                              -- afzender / actor / counterparty
    title TEXT NOT NULL,
    body_excerpt TEXT,
    context TEXT,                          -- thread_ref / meeting_id / channel
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    due_at INTEGER,                        -- nullable
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','done','ignored','snoozed')),
    resolved_at INTEGER,
    resolved_via TEXT,                     -- 'reply_detected' / 'manual' /
                                           --  'snoozed' / 'aged_out'
    notes TEXT,
    action_summary TEXT,                   -- 1-zin Llama-extract: wat is concreet
                                           -- de actie/vraag (toon in briefing/
                                           -- dayclose/dashboard ipv kale subject)
    -- Delegation follow-up: voor outgoing_request + meeting_action_other
    -- zet insert_loop default followup_at = created_at + 7 dagen zodat de
    -- scheduler the user kan poken "heb je iets gehoord van X?".
    followup_at INTEGER,                   -- unix-seconds; NULL = geen follow-up
    followup_pinged_at INTEGER             -- markeer dat Rosa al gepingd heeft
);
CREATE INDEX IF NOT EXISTS idx_open_loops_status_due
    ON open_loops(status, due_at);
CREATE INDEX IF NOT EXISTS idx_open_loops_source_ref
    ON open_loops(source, source_ref);
CREATE INDEX IF NOT EXISTS idx_open_loops_context
    ON open_loops(context, status);
"""


def init_open_loops_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotente migratie voor kolommen die later zijn toegevoegd.
    SQLite ondersteunt geen 'ALTER TABLE ADD COLUMN IF NOT EXISTS'."""
    existing = {r[1] for r in conn.execute(
        "PRAGMA table_info(open_loops)").fetchall()}
    if "action_summary" not in existing:
        conn.execute("ALTER TABLE open_loops ADD COLUMN action_summary TEXT")
    if "followup_at" not in existing:
        conn.execute("ALTER TABLE open_loops ADD COLUMN followup_at INTEGER")
    if "followup_pinged_at" not in existing:
        conn.execute(
            "ALTER TABLE open_loops ADD COLUMN followup_pinged_at INTEGER",
        )


def delegations_due_for_followup(
    conn: sqlite3.Connection, *, now_ts: int, limit: int = 20,
) -> list[dict[str, Any]]:
    """Open delegations met followup_at <= now en nog niet eerder gepingd."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM open_loops "
        "WHERE status='open' AND followup_at IS NOT NULL "
        "AND followup_at <= ? AND followup_pinged_at IS NULL "
        "ORDER BY followup_at ASC LIMIT ?",
        (now_ts, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_followup_pinged(
    conn: sqlite3.Connection, loop_ids: list[int],
) -> None:
    if not loop_ids:
        return
    placeholders = ",".join("?" for _ in loop_ids)
    conn.execute(
        f"UPDATE open_loops SET followup_pinged_at = strftime('%s','now') "
        f"WHERE id IN ({placeholders})",
        loop_ids,
    )


def extend_followup(
    conn: sqlite3.Connection, *, loop_id: int, extra_days: int,
) -> bool:
    """Verschuif followup_at met N dagen + reset pinged-marker zodat
    Rosa over X dagen opnieuw kan vragen."""
    row = conn.execute(
        "SELECT followup_at FROM open_loops WHERE id=?", (loop_id,),
    ).fetchone()
    if row is None:
        return False
    base = int(row[0]) if row[0] else int(__import__("time").time())
    new_followup = base + extra_days * 86400
    cur = conn.execute(
        "UPDATE open_loops SET followup_at=?, followup_pinged_at=NULL "
        "WHERE id=?",
        (new_followup, loop_id),
    )
    return cur.rowcount > 0


def set_action_summary(
    conn: sqlite3.Connection, loop_id: int, summary: str,
) -> bool:
    """Update de 1-zin Llama-extract voor een loop. Best-effort: nooit
    een fail in de ingest-flow door deze stap."""
    cur = conn.execute(
        "UPDATE open_loops SET action_summary=? WHERE id=?",
        (summary[:300], loop_id),
    )
    return cur.rowcount > 0


@dataclass
class OpenLoop:
    source: str
    kind: str
    title: str
    source_ref: str | None = None
    who: str | None = None
    body_excerpt: str | None = None
    context: str | None = None
    due_at: int | None = None
    notes: str | None = None


_DELEGATION_KINDS = frozenset({"outgoing_request", "meeting_action_other"})
_DELEGATION_FOLLOWUP_DEFAULT_DAYS = 7


def insert_loop(conn: sqlite3.Connection, loop: OpenLoop) -> int | None:
    """Add an open loop. Returns row id, or None if duplicate (matching
    source+source_ref already exists in any status).

    Voor delegation-kinds (outgoing_request, meeting_action_other) wordt
    `followup_at` default gezet op `now + 7d` zodat de scheduler the user
    kan poken "heb je iets gehoord van X?". Andere kinds blijven None
    (geen follow-up; die wachten op een inkomende reactie van the user
    zelf)."""
    if loop.source_ref:
        existing = conn.execute(
            "SELECT id FROM open_loops WHERE source=? AND source_ref=? LIMIT 1",
            (loop.source, loop.source_ref),
        ).fetchone()
        if existing:
            return None
    followup_at: int | None = None
    if loop.kind in _DELEGATION_KINDS:
        import time as _t
        followup_at = int(_t.time()) + _DELEGATION_FOLLOWUP_DEFAULT_DAYS * 86400
    cur = conn.execute(
        """
        INSERT INTO open_loops (source, source_ref, kind, who, title,
                                body_excerpt, context, due_at, notes,
                                followup_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (loop.source, loop.source_ref, loop.kind, loop.who, loop.title,
         loop.body_excerpt, loop.context, loop.due_at, loop.notes,
         followup_at),
    )
    return cur.lastrowid


def list_open(
    conn: sqlite3.Connection,
    *, kind: str | None = None, who: str | None = None,
    days_back: int | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM open_loops WHERE status = 'open'"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if who:
        sql += " AND who LIKE ?"
        params.append(f"%{who}%")
    if days_back is not None:
        import time as _time
        sql += " AND created_at >= ?"
        params.append(int(_time.time()) - days_back * 86400)
    sql += " ORDER BY COALESCE(due_at, created_at) ASC LIMIT ?"
    params.append(limit)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def close_loop(
    conn: sqlite3.Connection, loop_id: int, *, via: str = "manual",
    notes: str | None = None,
) -> bool:
    cur = conn.execute(
        "UPDATE open_loops SET status='done', resolved_at=strftime('%s','now'), "
        "resolved_via=?, notes=COALESCE(?, notes) "
        "WHERE id = ? AND status='open'",
        (via, notes, loop_id),
    )
    return cur.rowcount > 0


def close_loops_by_context(
    conn: sqlite3.Connection, *, context: str,
    kinds: Iterable[str] = ("incoming_question", "incoming_task"),
    via: str = "reply_detected",
) -> int:
    """Close open loops in the given context (thread_ref) of the given
    kinds. Used both ways:
      - the user replies in a thread → close incoming_question/task loops
      - Counterparty replies in a thread → close outgoing_request loops
    """
    kinds_list = list(kinds)
    if not kinds_list:
        return 0
    placeholders = ",".join("?" for _ in kinds_list)
    cur = conn.execute(
        f"UPDATE open_loops SET status='done', resolved_at=strftime('%s','now'), "
        f"resolved_via=? "
        f"WHERE status='open' AND context=? AND kind IN ({placeholders})",
        (via, context, *kinds_list),
    )
    return cur.rowcount


def snooze_loop(conn: sqlite3.Connection, loop_id: int, *, until_unix: int) -> bool:
    cur = conn.execute(
        "UPDATE open_loops SET status='snoozed', due_at=? "
        "WHERE id = ? AND status='open'",
        (until_unix, loop_id),
    )
    return cur.rowcount > 0


def reopen_snoozed_due(conn: sqlite3.Connection) -> int:
    """Move snoozed loops whose due_at has passed back to status=open."""
    import time as _time
    cur = conn.execute(
        "UPDATE open_loops SET status='open' "
        "WHERE status='snoozed' AND due_at IS NOT NULL AND due_at <= ?",
        (int(_time.time()),),
    )
    return cur.rowcount
