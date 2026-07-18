"""Schema voor English collocations practice — Leitner-box spaced
repetition over de bold collocations uit the user's PDF.

`english_cards`     — één rij per collocation
`english_sessions`  — één rij per oefen-sessie (wanneer/hoe gepresteerd)
`english_state`     — een single-row table met de active card_id zodat
                       de iMessage-handler weet dat het volgende antwoord
                       een evaluation moet worden, niet een normaal
                       orchestrator-turn.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS english_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collocation TEXT NOT NULL,        -- bv. "inclement weather"
    context TEXT,                     -- voorbeeldzin uit boek (indien gevonden)
    page_no INTEGER,                  -- waar in PDF
    unit_title TEXT,                  -- bv. "Strong, fixed and weak collocations"
    box INTEGER NOT NULL DEFAULT 1
        CHECK(box BETWEEN 1 AND 5),
    next_due_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    correct_count INTEGER NOT NULL DEFAULT 0,
    wrong_count INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(collocation)
);
CREATE INDEX IF NOT EXISTS idx_english_due
    ON english_cards(next_due_at);
CREATE INDEX IF NOT EXISTS idx_english_box
    ON english_cards(box);

CREATE TABLE IF NOT EXISTS english_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    ended_at INTEGER,
    cards_reviewed INTEGER NOT NULL DEFAULT 0,
    correct INTEGER NOT NULL DEFAULT 0,
    wrong INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS english_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    active_session_id INTEGER REFERENCES english_sessions(id),
    active_card_id INTEGER REFERENCES english_cards(id),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
INSERT OR IGNORE INTO english_state(singleton, active_session_id, active_card_id)
VALUES (1, NULL, NULL);
"""

# Leitner intervallen in dagen — bij correct: promote box; bij fout: terug
# naar box 1. Box 1 = morgen, box 2 = over 3 dagen, etc.
LEITNER_INTERVALS_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}


def init_english_practice_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def insert_card(
    conn: sqlite3.Connection, *,
    collocation: str, context: str | None = None,
    page_no: int | None = None, unit_title: str | None = None,
) -> int | None:
    """Insert; returns id, of None bij duplicate (collocation bestaat al)."""
    try:
        cur = conn.execute(
            "INSERT INTO english_cards (collocation, context, page_no, unit_title) "
            "VALUES (?,?,?,?)",
            (collocation.strip(), context, page_no, unit_title),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def due_cards(
    conn: sqlite3.Connection, *, limit: int = 10,
) -> list[dict[str, Any]]:
    """Kaarten die vandaag of eerder due zijn — random shuffled per box
    om variatie te krijgen."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM english_cards "
        "WHERE next_due_at <= strftime('%s','now') "
        "ORDER BY box ASC, RANDOM() LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_card(
    conn: sqlite3.Connection, card_id: int,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM english_cards WHERE id=?", (card_id,),
    ).fetchone()
    return dict(row) if row else None


def review_card(
    conn: sqlite3.Connection, card_id: int, *, correct: bool,
) -> dict[str, Any] | None:
    """Update box + next_due_at na een review. Returns updated card."""
    card = get_card(conn, card_id)
    if card is None:
        return None
    if correct:
        new_box = min(card["box"] + 1, 5)
        delta_days = LEITNER_INTERVALS_DAYS[new_box]
        conn.execute(
            "UPDATE english_cards SET box=?, "
            "next_due_at=strftime('%s','now','+' || ? || ' days'), "
            "correct_count=correct_count+1, "
            "last_reviewed_at=strftime('%s','now') WHERE id=?",
            (new_box, delta_days, card_id),
        )
    else:
        # Terug naar box 1, morgen weer
        conn.execute(
            "UPDATE english_cards SET box=1, "
            "next_due_at=strftime('%s','now','+1 day'), "
            "wrong_count=wrong_count+1, "
            "last_reviewed_at=strftime('%s','now') WHERE id=?",
            (card_id,),
        )
    return get_card(conn, card_id)


# --- session helpers -----------------------------------------------------

def start_session(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO english_sessions DEFAULT VALUES")
    sid = int(cur.lastrowid)
    conn.execute(
        "UPDATE english_state SET active_session_id=?, active_card_id=NULL, "
        "updated_at=strftime('%s','now') WHERE singleton=1",
        (sid,),
    )
    return sid


def end_session(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        "UPDATE english_sessions SET ended_at=strftime('%s','now') WHERE id=?",
        (session_id,),
    )
    conn.execute(
        "UPDATE english_state SET active_session_id=NULL, active_card_id=NULL, "
        "updated_at=strftime('%s','now') WHERE singleton=1",
    )


def set_active_card(
    conn: sqlite3.Connection, card_id: int | None,
) -> None:
    conn.execute(
        "UPDATE english_state SET active_card_id=?, "
        "updated_at=strftime('%s','now') WHERE singleton=1",
        (card_id,),
    )


def get_state(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM english_state WHERE singleton=1"
    ).fetchone()
    return dict(row) if row else {}


def increment_session_count(
    conn: sqlite3.Connection, session_id: int, *, correct: bool,
) -> None:
    field = "correct" if correct else "wrong"
    conn.execute(
        f"UPDATE english_sessions SET cards_reviewed=cards_reviewed+1, "
        f"{field}={field}+1 WHERE id=?",
        (session_id,),
    )
