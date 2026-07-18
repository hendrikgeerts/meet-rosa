"""Tests voor extensions.whats_open — cross-channel aggregator.

Schema-init heeft een chickengrap: comm_intel + open_loops + reminders
hebben elk hun eigen DB-init. Test maakt minimaal de schemas + zaait
realistische data; aggregator queryt erover heen."""
from __future__ import annotations

import sqlite3
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from extensions.comm_intel.schema import init_comm_schema
from extensions.open_loops.schema import (
    OpenLoop,
    init_open_loops_schema,
    insert_loop,
)
from extensions.reminders import init_reminders_schema
from extensions.whats_open.aggregator import collect_whats_open
from extensions.whats_open.tools import WHATS_OPEN_HANDLERS
from integrations.todoist import Task


@dataclass
class _FakeTodoist:
    tasks: list[Task] = field(default_factory=list)

    def list_tasks(self, *, project_id: str | None = None) -> list[Task]:
        return list(self.tasks)


def _make_task(tid: str, content: str, *, due_date: str | None = None) -> Task:
    return Task(
        id=tid, content=content, project_id="p1", is_completed=False,
        labels=[], due_date=due_date, due_datetime=None,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "wo.db"
    init_reminders_schema(p)
    init_open_loops_schema(p)
    init_comm_schema(p)
    return p


# ---- empty state ------------------------------------------------------

def test_empty_db_returns_zero_totals(db: Path) -> None:
    out = collect_whats_open(db)
    assert out["totals"]["grand_total"] == 0
    assert out["loops_inbound"] == []
    assert out["reminders_pending"] == []
    assert out["todoist"]["available"] is False


# ---- open_loops -------------------------------------------------------

def test_inbound_loops_counted_and_listed(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="piet@klant.nl",
            title="Reageert op offerte?",
        ))
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:def",
            kind="incoming_task", who="anouk@klant.nl",
            title="Vraag iets",
        ))
    out = collect_whats_open(db)
    assert out["totals"]["loops_inbound"] == 2
    assert len(out["loops_inbound"]) == 2


def test_outgoing_request_in_waiting_bucket(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:xyz",
            kind="outgoing_request", who="anouk@partner.nl",
            title="Wacht op goedkeuring",
        ))
    out = collect_whats_open(db)
    assert out["totals"]["loops_waiting"] == 1
    assert out["totals"]["loops_inbound"] == 0


def test_meeting_action_self_in_meeting_bucket(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="plaud", source_ref="meeting:1:self:slug",
            kind="meeting_action_self", who="Hendrik",
            title="Stuur Q3 cijfers",
        ))
    out = collect_whats_open(db)
    assert out["totals"]["loops_meeting"] == 1


# ---- reminders --------------------------------------------------------

def test_pending_reminders_counted_and_sorted_soonest_first(db: Path) -> None:
    future = int(_time.time()) + 3600
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h", "Later", future + 86400),
        )
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h", "Sooner", future),
        )
    out = collect_whats_open(db)
    assert out["totals"]["reminders_pending"] == 2
    assert out["reminders_pending"][0]["body"] == "Sooner"


def test_sent_reminders_not_counted(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at, sent_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            ("h", "Done", int(_time.time()) - 3600),
        )
    out = collect_whats_open(db)
    assert out["totals"]["reminders_pending"] == 0


# ---- todoist --------------------------------------------------------

def test_todoist_pulse_included_when_client_provided(db: Path) -> None:
    from datetime import datetime

    from core.timezone import current_tz
    today_iso = datetime.now(current_tz()).date().isoformat()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "vandaag", due_date=today_iso),
        _make_task("b", "achterstallig", due_date="2026-01-01"),
    ])
    out = collect_whats_open(db, todoist_client=fake, todoist_project_id="p1")
    assert out["todoist"]["available"] is True
    assert out["totals"]["todoist_today"] == 1
    assert out["totals"]["todoist_overdue"] == 1


def test_todoist_skipped_when_no_client(db: Path) -> None:
    out = collect_whats_open(db, todoist_client=None)
    assert out["todoist"]["available"] is False
    assert out["totals"]["todoist_today"] == 0


# ---- limits + totals --------------------------------------------------

def test_grand_total_sums_all_buckets(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:1",
            kind="incoming_question", who="x@y.com", title="a",
        ))
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h", "x", int(_time.time()) + 3600),
        )
    out = collect_whats_open(db)
    expected = sum(v for k, v in out["totals"].items() if k != "grand_total")
    assert out["totals"]["grand_total"] == expected


def test_per_section_limit_clamps_lists(db: Path) -> None:
    with sqlite3.connect(db) as c:
        for i in range(10):
            insert_loop(c, OpenLoop(
                source="comm", source_ref=f"gmail:hg:{i}",
                kind="incoming_question", who=f"a{i}@x.nl",
                title=f"vraag {i}",
            ))
    out = collect_whats_open(db, per_section_limit=3)
    assert out["totals"]["loops_inbound"] == 10  # total niet geclipt
    assert len(out["loops_inbound"]) == 3        # display wel


# ---- tool-handler -----------------------------------------------------

def test_tool_handler_returns_dict(db: Path) -> None:
    out = WHATS_OPEN_HANDLERS["whats_open"](db, {})
    assert "totals" in out
    assert "loops_inbound" in out
    assert "todoist" in out


def test_tool_handler_invalid_limit_falls_back_to_default(db: Path) -> None:
    out = WHATS_OPEN_HANDLERS["whats_open"](db, {"per_section_limit": "garbage"})
    assert "totals" in out
