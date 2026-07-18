"""Tests voor core.weekly_retro — context-collection."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from core.weekly_retro import (
    _collect_closed_counts,
    _collect_comm_volume,
    _collect_delegations_summary,
    _collect_still_open,
    collect_weekly_retro_context,
)
from extensions.comm_intel.schema import init_comm_schema
from extensions.open_loops.schema import (
    OpenLoop,
    init_open_loops_schema,
    insert_loop,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "retro.db"
    init_open_loops_schema(p)
    init_comm_schema(p)
    return p


def _today_ts() -> int:
    return int(_time.time())


# ---- comm_volume ------------------------------------------------------

def test_comm_volume_counts_directions_and_sources(db: Path) -> None:
    now = _today_ts()
    week_start = now - 7 * 86400
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO comm_items "
            "(source, account, external_id, direction, occurred_at, body_full) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("gmail", "hg", "1", "in", now - 86400, ""),
                ("gmail", "hg", "2", "out", now - 86400, ""),
                ("slack", "ws", "3", "in", now - 86400, ""),
                ("imap", "x", "4", "in", now - 86400, ""),
                ("gmail", "hg", "5", "in", week_start - 86400, ""),  # buiten venster
            ],
        )
    vol = _collect_comm_volume(db, week_start, now + 1)
    assert vol["mails_in"] == 2   # gmail + imap
    assert vol["mails_out"] == 1
    assert vol["slack_in"] == 1
    assert vol["slack_out"] == 0


# ---- closed counts ----------------------------------------------------

def test_closed_counts_only_within_window(db: Path) -> None:
    now = _today_ts()
    week_start = now - 7 * 86400
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="incoming_question",
            who="a", title="t",
        ))
        c.execute(
            "UPDATE open_loops SET status='done', resolved_at=? WHERE id=?",
            (now - 3 * 86400, loop_id),
        )
    counts = _collect_closed_counts(db, week_start, now + 1)
    assert counts["loops"] == 1


def test_closed_counts_excludes_outside_window(db: Path) -> None:
    now = _today_ts()
    week_start = now - 7 * 86400
    with sqlite3.connect(db) as c:
        loop_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="incoming_question",
            who="a", title="t",
        ))
        c.execute(
            "UPDATE open_loops SET status='done', resolved_at=? WHERE id=?",
            (week_start - 86400, loop_id),  # vorige week
        )
    counts = _collect_closed_counts(db, week_start, now + 1)
    assert counts["loops"] == 0


# ---- still_open -------------------------------------------------------

def test_still_open_returns_oldest_first(db: Path) -> None:
    with sqlite3.connect(db) as c:
        # Twee inkomende met verschillende leeftijden
        old_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="incoming_question",
            who="a", title="oud",
        ))
        c.execute(
            "UPDATE open_loops SET created_at=? WHERE id=?",
            (_today_ts() - 30 * 86400, old_id),
        )
        new_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:2", kind="incoming_question",
            who="b", title="vers",
        ))
        c.execute(
            "UPDATE open_loops SET created_at=? WHERE id=?",
            (_today_ts() - 1 * 86400, new_id),
        )
    items = _collect_still_open(db, limit=3)
    assert len(items) == 2
    assert items[0]["title"] == "oud"


# ---- delegations_summary ---------------------------------------------

def test_delegations_summary_counts_waiting_and_overdue(db: Path) -> None:
    now = _today_ts()
    with sqlite3.connect(db) as c:
        # outgoing_request → krijgt automatisch followup_at = now+7d
        insert_loop(c, OpenLoop(
            source="comm", source_ref="x:1", kind="outgoing_request",
            who="a", title="future fu",
        ))
        # eentje waar followup_at al gepasseerd is
        overdue_id = insert_loop(c, OpenLoop(
            source="comm", source_ref="x:2", kind="outgoing_request",
            who="b", title="overdue",
        ))
        c.execute(
            "UPDATE open_loops SET followup_at=? WHERE id=?",
            (now - 86400, overdue_id),
        )

    summary = _collect_delegations_summary(db, now)
    assert summary["waiting_total"] == 2
    assert summary["overdue"] == 1


# ---- end-to-end context -----------------------------------------------

def test_collect_returns_required_keys(db: Path) -> None:
    ctx = collect_weekly_retro_context(db_path=db)
    for key in (
        "now", "week_start", "comm_volume", "closed_count",
        "still_open", "delegations_summary", "sales_summary", "patterns",
    ):
        assert key in ctx


def test_collect_handles_empty_db_gracefully(db: Path) -> None:
    ctx = collect_weekly_retro_context(db_path=db)
    assert ctx["comm_volume"]["mails_in"] == 0
    assert ctx["closed_count"]["loops"] == 0
    assert ctx["still_open"] == []
