"""plaud_meetings tabel — één rij per geanalyseerd Plaud-transcript.

Analyse-output (samenvatting / deelnemers / besluiten / open vragen)
landt hier; actiepunten gaan naar open_loops zodat ze door de standaard
follow-up workflow worden opgepakt.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS plaud_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id INTEGER NOT NULL UNIQUE
        REFERENCES plaud_transcripts(id),
    analyzed_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    summary TEXT,
    participants TEXT,        -- JSON array of strings
    decisions TEXT,           -- JSON array of strings
    open_questions TEXT,      -- JSON array of strings
    actions_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plaud_meetings_analyzed
    ON plaud_meetings(analyzed_at DESC);
"""


def init_plaud_meetings_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


@dataclass
class MeetingAnalysis:
    summary: str
    participants: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    actions_for_hendrik: list[dict[str, Any]] = field(default_factory=list)
    actions_for_others: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


def insert_meeting(
    conn: sqlite3.Connection,
    *, transcript_id: int, analysis: MeetingAnalysis, actions_count: int,
) -> int | None:
    """Returns the inserted row id, or None if a meeting already exists for
    this transcript (UNIQUE constraint on transcript_id)."""
    try:
        cur = conn.execute(
            """
            INSERT INTO plaud_meetings
              (transcript_id, summary, participants, decisions, open_questions, actions_count)
            VALUES (?,?,?,?,?,?)
            """,
            (
                transcript_id, analysis.summary,
                json.dumps(analysis.participants, ensure_ascii=False),
                json.dumps(analysis.decisions, ensure_ascii=False),
                json.dumps(analysis.open_questions, ensure_ascii=False),
                actions_count,
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def find_unanalyzed_transcripts(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    """Transcripts that don't yet have a corresponding plaud_meetings row."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.id, t.title, t.body, t.recorded_at
        FROM plaud_transcripts t
        LEFT JOIN plaud_meetings m ON m.transcript_id = t.id
        WHERE m.id IS NULL
        ORDER BY t.recorded_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
