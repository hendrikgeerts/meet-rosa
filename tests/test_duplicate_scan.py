"""Tests voor weekly duplicate-scan over reminders + Todoist."""
from __future__ import annotations

import sqlite3
import time as _t
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.duplicate_scan import collect_duplicate_pairs
from extensions import reminders
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


def _add_reminder(db: Path, *, body: str, in_seconds: int = 3600) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", body, int(_t.time()) + in_seconds),
        )


def _mk_task(tid: str, content: str) -> Task:
    return Task(
        id=tid, content=content, project_id="p1", is_completed=False,
        labels=[], due_date=None, due_datetime=None,
    )


def test_scan_finds_duplicate_reminders(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering vandaag")
    _add_reminder(db, body="Bel verzekering morgen")
    pairs = collect_duplicate_pairs(db_path=db)
    assert len(pairs) == 1


def test_scan_finds_duplicate_across_reminder_and_todoist(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering vandaag")
    fake = _FakeTodoist(tasks=[_mk_task("t1", "Bel verzekering")])
    pairs = collect_duplicate_pairs(
        db_path=db, todoist_client=fake, todoist_project_id="p1",
    )
    assert len(pairs) == 1
    # Todoist wint als keeper
    assert pairs[0]["keeper"]["source"] == "todoist"
    assert pairs[0]["duplicate"]["source"] == "reminder"


def test_scan_ignores_dissimilar_items(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering")
    _add_reminder(db, body="Mail boekhouder Q3")
    pairs = collect_duplicate_pairs(db_path=db)
    assert pairs == []


def test_scan_returns_empty_when_no_items(db: Path) -> None:
    assert collect_duplicate_pairs(db_path=db) == []


def test_scan_skips_sent_reminders(db: Path) -> None:
    _add_reminder(db, body="Bel verzekering vandaag")
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at, sent_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            ("h1", "Bel verzekering morgen", int(_t.time()) + 3600),
        )
    pairs = collect_duplicate_pairs(db_path=db)
    assert pairs == []


def test_scan_short_body_skipped(db: Path) -> None:
    _add_reminder(db, body="X")
    _add_reminder(db, body="Y")
    pairs = collect_duplicate_pairs(db_path=db)
    assert pairs == []


def test_scan_keeper_prefers_earliest_when_both_reminders(db: Path) -> None:
    now = int(_t.time())
    with sqlite3.connect(db) as c:
        cur1 = c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Bel verzekering nu", now + 60),  # sooner
        )
        cur2 = c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Bel verzekering vandaag", now + 86400),  # later
        )
    pairs = collect_duplicate_pairs(db_path=db)
    assert len(pairs) == 1
    # De eerstvolgende (sooner) is de keeper
    assert pairs[0]["keeper"]["id"] == cur1.lastrowid
    assert pairs[0]["duplicate"]["id"] == cur2.lastrowid
