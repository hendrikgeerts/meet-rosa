"""Persistente state voor scheduler-jobs (briefing, midday, dayclose, ...).

Houdt per job-naam bij wanneer hij voor het laatst is gefired. Wordt
gebruikt voor catch-up logica: als de daemon herstart vlak na een
geplande fire-tijd, wil je niet stilletjes de tick van die dag skippen.

Schema is bewust generiek (job_name TEXT primary key) zodat nieuwe
scheduler-jobs niets aan deze tabel hoeven veranderen.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_state (
    job_name TEXT PRIMARY KEY,
    last_fired_at INTEGER NOT NULL,
    notes TEXT
);
"""


def init_scheduler_state_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA)
        conn.commit()


def get_last_fired(
    conn: sqlite3.Connection, job_name: str, *, tz=None,
) -> datetime | None:
    row = conn.execute(
        "SELECT last_fired_at FROM scheduler_state WHERE job_name=?",
        (job_name,),
    ).fetchone()
    if row is None:
        return None
    ts = int(row[0])
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def set_last_fired(
    conn: sqlite3.Connection, job_name: str, when: datetime, *,
    notes: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO scheduler_state(job_name, last_fired_at, notes) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(job_name) DO UPDATE SET "
        "  last_fired_at=excluded.last_fired_at, notes=excluded.notes",
        (job_name, int(when.timestamp()), notes),
    )
    conn.commit()
