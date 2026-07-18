"""Daily reminder generator for English collocations practice.

Sends the user a short iMessage when cards are due. Composed so the scheduler
can call it without any LLM/Gmail context. Returns `None` when nothing is
due — the scheduler then skips the send entirely (no spammy "0 cards" pings).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def generate_english_reminder(db_path: Path) -> str | None:
    """Returns the reminder text, or None if nothing to remind about."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            state = conn.execute(
                "SELECT s.active_card_id, c.collocation "
                "FROM english_state s "
                "LEFT JOIN english_cards c ON c.id = s.active_card_id "
                "WHERE s.singleton = 1"
            ).fetchone()
            due_total = conn.execute(
                "SELECT COUNT(*) FROM english_cards "
                "WHERE next_due_at <= strftime('%s','now')"
            ).fetchone()[0]
    except sqlite3.OperationalError:
        return None  # schema not initialised yet — silently skip

    if state and state["active_card_id"]:
        # the user bailed mid-session yesterday; nudge him to finish that card.
        return (
            f"You still have an open English card: \"{state['collocation']}\"."
            " Reply with your sentence to grade it, or say 'skip' / 'stop'."
        )

    if due_total <= 0:
        return None

    if due_total == 1:
        return (
            "Good morning. 1 English collocation is due. "
            "Reply 'practice' to start."
        )
    return (
        f"Good morning. {due_total} English collocations are due. "
        "Reply 'practice' to start."
    )
