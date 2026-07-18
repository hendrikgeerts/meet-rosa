"""Tests voor delegation-tracker: followup_at + 7d default voor
outgoing_request/meeting_action_other, scheduler-tick, en tools."""
from __future__ import annotations

import sqlite3
import time as _t
from pathlib import Path

import pytest

from extensions.open_loops.schema import (
    OpenLoop,
    delegations_due_for_followup,
    extend_followup,
    init_open_loops_schema,
    insert_loop,
    mark_followup_pinged,
)
from extensions.open_loops.tools import LOOPS_HANDLERS


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "loops.db"
    init_open_loops_schema(p)
    return p


# ---- followup_at default ---------------------------------------------

def test_outgoing_request_gets_default_followup(db: Path) -> None:
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="outgoing_request", who="anouk@klant.nl",
            title="Vroeg om Q3-cijfers",
        ))
        row = c.execute(
            "SELECT followup_at, created_at FROM open_loops WHERE id=?",
            (loop_id,),
        ).fetchone()
    followup_at, created_at = row
    assert followup_at is not None
    # ~7 dagen verschil
    assert abs((followup_at - created_at) - 7 * 86400) < 5


def test_meeting_action_other_gets_default_followup(db: Path) -> None:
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="plaud", source_ref="meeting:1:other:slug",
            kind="meeting_action_other", who="Piet",
            title="Stuurt vrijdag goedkeuring",
        ))
        row = c.execute(
            "SELECT followup_at FROM open_loops WHERE id=?",
            (loop_id,),
        ).fetchone()
    assert row[0] is not None


def test_incoming_question_has_no_followup(db: Path) -> None:
    """Incoming-loops zijn ACTIES VOOR Hendrik — geen delegation, geen
    followup."""
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:xyz",
            kind="incoming_question", who="x@y.nl",
            title="Wanneer kun je?",
        ))
        row = c.execute(
            "SELECT followup_at FROM open_loops WHERE id=?",
            (loop_id,),
        ).fetchone()
    assert row[0] is None


# ---- delegations_due_for_followup ------------------------------------

def test_due_returns_only_pending_loops_past_followup(db: Path) -> None:
    past = int(_t.time()) - 86400
    future = int(_t.time()) + 86400
    with sqlite3.connect(db) as c:
        # Past followup, niet gepingd → should match
        loop1 = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="outgoing_request",
            who="a", title="due now",
        ))
        c.execute("UPDATE open_loops SET followup_at=? WHERE id=?",
                   (past, loop1))
        # Future followup → no match
        insert_loop(c, OpenLoop(
            source="comm", source_ref="x:2", kind="outgoing_request",
            who="b", title="not yet",
        ))
        c.execute("UPDATE open_loops SET followup_at=? WHERE id IN (2)",
                   (future,))
        # Past followup, AL gepingd → no match
        loop3 = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:3", kind="outgoing_request",
            who="c", title="already pinged",
        ))
        c.execute(
            "UPDATE open_loops SET followup_at=?, followup_pinged_at=? "
            "WHERE id=?", (past, int(_t.time()), loop3),
        )

        due = delegations_due_for_followup(c, now_ts=int(_t.time()))
    assert len(due) == 1
    assert due[0]["id"] == loop1


def test_mark_followup_pinged_updates_marker(db: Path) -> None:
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="outgoing_request",
            who="a", title="t",
        ))
        mark_followup_pinged(c, [loop_id])
        row = c.execute(
            "SELECT followup_pinged_at FROM open_loops WHERE id=?",
            (loop_id,),
        ).fetchone()
    assert row[0] is not None


def test_extend_followup_shifts_date_and_resets_pinged(db: Path) -> None:
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="outgoing_request",
            who="a", title="t",
        ))
        original = c.execute(
            "SELECT followup_at FROM open_loops WHERE id=?", (loop_id,),
        ).fetchone()[0]
        mark_followup_pinged(c, [loop_id])
        ok = extend_followup(c, loop_id=loop_id, extra_days=7)
        new_row = c.execute(
            "SELECT followup_at, followup_pinged_at FROM open_loops WHERE id=?",
            (loop_id,),
        ).fetchone()
    assert ok is True
    assert new_row[0] > original  # later in time
    assert new_row[1] is None      # reset


# ---- tools -----------------------------------------------------------

def test_tool_delegations_list_returns_outgoing(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="outgoing_request",
            who="anouk", title="vraag review",
        ))
        insert_loop(c, OpenLoop(
            source="comm", source_ref="x:2", kind="incoming_question",
            who="piet", title="kun je dit",
        ))
    out = LOOPS_HANDLERS["delegations_list"](db, {})
    # Alleen outgoing_request / meeting_action_other tellen
    assert out["count"] == 1
    assert out["items"][0]["who"] == "anouk"


def test_tool_extend_followup_changes_date(db: Path) -> None:
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="outgoing_request",
            who="a", title="t",
        ))
    out = LOOPS_HANDLERS["delegation_extend_followup"](
        db, {"loop_id": loop_id, "extra_days": 14},
    )
    assert out["ok"] is True
    assert out["extended_by_days"] == 14


def test_tool_extend_followup_missing_id_errors(db: Path) -> None:
    out = LOOPS_HANDLERS["delegation_extend_followup"](db, {})
    assert "error" in out
