"""Tests voor preventieve duplicate-check op set_reminder."""
from __future__ import annotations

import sqlite3
import time as _t
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from extensions import reminders
from extensions.reminders_dedup import find_similar
from integrations.todoist import Task


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "d.db"
    reminders.init_reminders_schema(p)
    return p


@dataclass
class _FakeTodoist:
    tasks: list[Task] = field(default_factory=list)

    def list_tasks(self, *, project_id=None):
        return list(self.tasks)


def _add_reminder(db: Path, *, body: str, handle: str = "h1",
                   in_seconds: int = 3600) -> int:
    with sqlite3.connect(db) as c:
        cur = c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            (handle, body, int(_t.time()) + in_seconds),
        )
    return int(cur.lastrowid)


def _mk_task(tid: str, content: str) -> Task:
    return Task(
        id=tid, content=content, project_id="p1", is_completed=False,
        labels=[], due_date=None, due_datetime=None,
    )


# ---- pending reminders --------------------------------------------------

def test_similar_pending_reminder_flagged(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering morgen")
    hits = find_similar(
        db_path=db, handle="h1",
        new_body="Bel verzekering vandaag",
    )
    assert len(hits) == 1
    assert hits[0]["source"] == "reminder"


def test_dissimilar_pending_reminder_not_flagged(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering")
    hits = find_similar(
        db_path=db, handle="h1",
        new_body="Mail boekhouder over Q3-cijfers",
    )
    assert hits == []


def test_sent_reminder_excluded(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at, sent_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            ("h1", "Bel verzekering", int(_t.time()) + 3600),
        )
    hits = find_similar(
        db_path=db, handle="h1", new_body="Bel verzekering",
    )
    assert hits == []


def test_cancelled_reminder_excluded(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at, cancelled_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            ("h1", "Bel verzekering", int(_t.time()) + 3600),
        )
    hits = find_similar(
        db_path=db, handle="h1", new_body="Bel verzekering",
    )
    assert hits == []


def test_other_handle_excluded(db: Path) -> None:
    """Reminders van andere handles horen niet in Hendrik's dedup-scope."""
    _add_reminder(db, body="Bel verzekering", handle="other")
    hits = find_similar(
        db_path=db, handle="h1", new_body="Bel verzekering",
    )
    assert hits == []


# ---- Todoist tasks ----------------------------------------------------

def test_similar_todoist_task_flagged(db: Path) -> None:
    fake = _FakeTodoist(tasks=[_mk_task("t1", "Bel verzekering")])
    hits = find_similar(
        db_path=db, handle="h1",
        new_body="Bel verzekering",
        todoist_client=fake, todoist_project_id="p1",
    )
    assert len(hits) == 1
    assert hits[0]["source"] == "todoist"
    assert hits[0]["id"] == "t1"


def test_no_todoist_client_only_checks_reminders(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering")
    hits = find_similar(
        db_path=db, handle="h1", new_body="Bel verzekering",
    )
    assert len(hits) == 1
    assert hits[0]["source"] == "reminder"


def test_short_body_skipped(db: Path) -> None:
    """Bodies < 4 chars zijn te ambigu voor dedup."""
    _add_reminder(db, body="X")
    hits = find_similar(db_path=db, handle="h1", new_body="X")
    assert hits == []


def test_max_hits_respected(db: Path) -> None:
    for i in range(10):
        _add_reminder(db, body=f"Bel verzekering variant {i}")
    hits = find_similar(
        db_path=db, handle="h1",
        new_body="Bel verzekering vandaag", max_hits=3,
    )
    assert len(hits) == 3
