"""meeting_preps_sent — dedup-tabel zodat we per event slechts 1× een
prep brief sturen, ook als de scheduler ticks-laag overlap heeft."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_preps_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    sent_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    minutes_before INTEGER
);
"""


def init_meeting_prep_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def already_sent(conn: sqlite3.Connection, event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM meeting_preps_sent WHERE event_id=? LIMIT 1",
        (event_id,),
    ).fetchone()
    return row is not None


def mark_sent(
    conn: sqlite3.Connection, *, event_id: str, minutes_before: int,
) -> bool:
    try:
        conn.execute(
            "INSERT INTO meeting_preps_sent (event_id, minutes_before) VALUES (?, ?)",
            (event_id, minutes_before),
        )
        return True
    except sqlite3.IntegrityError:
        return False
