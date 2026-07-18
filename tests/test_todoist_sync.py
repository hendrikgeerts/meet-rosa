"""Tests voor extensions.todoist_sync — schema, push, pull,
loop-source labelling, dedup. Geen netwerk-call: TodoistClient gemockt."""
from __future__ import annotations

import sqlite3
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from extensions import reminders
from extensions.open_loops.schema import (
    OpenLoop, init_open_loops_schema, insert_loop,
)
from extensions.todoist_sync.schema import (
    get_link_by_local, init_todoist_sync_schema, insert_link,
)
from extensions.todoist_sync.sync import (
    _loop_label, to_rfc3339, pull_completions, push_pending,
)
from integrations.todoist import Project, Task


# --- fixtures + fakes -----------------------------------------------------

@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "todoist.db"
    reminders.init_reminders_schema(p)
    init_open_loops_schema(p)
    init_todoist_sync_schema(p)
    return p


@dataclass
class _FakeTodoist:
    """Mimics TodoistClient: in-memory store van created tasks."""
    next_id: int = 1
    tasks: dict[str, Task] = field(default_factory=dict)
    descriptions: dict[str, str | None] = field(default_factory=dict)
    completed_ids: set[str] = field(default_factory=set)

    raise_project_full_after: int | None = None  # gooi op call N (0-indexed)
    _call_count: int = 0

    def create_task(self, *, content, project_id=None, labels=None,
                    due_string=None, due_datetime=None, description=None) -> Task:
        if (self.raise_project_full_after is not None
                and self._call_count >= self.raise_project_full_after):
            from integrations.todoist import TodoistProjectFullError
            raise TodoistProjectFullError("max items reached")
        self._call_count += 1
        tid = f"t{self.next_id}"
        self.next_id += 1
        t = Task(
            id=tid, content=content, project_id=project_id,
            is_completed=False, labels=list(labels or []),
            due_date=None, due_datetime=due_datetime,
        )
        self.tasks[tid] = t
        # Task dataclass heeft geen description-veld; we capturen het apart
        # zodat MED-3 tests kunnen verifiëren wat er naar Todoist gaat.
        self.descriptions[tid] = description
        return t

    def list_tasks(self, *, project_id=None) -> list[Task]:
        return [t for t in self.tasks.values() if t.id not in self.completed_ids]

    def close_task(self, task_id: str) -> bool:
        self.completed_ids.add(task_id)
        return True


_PROJECT = Project(id="p1", name="Rosa")


# --- _loop_label heuristic ------------------------------------------------

def test_loop_label_for_gmail_loop() -> None:
    row = {"source": "comm", "source_ref": "gmail:gmail:abc123", "kind": "incoming_question"}
    assert _loop_label(row) == "rosa-mail"


def test_loop_label_for_slack_loop() -> None:
    row = {"source": "comm", "source_ref": "slack:ws1:T123", "kind": "incoming_task"}
    assert _loop_label(row) == "rosa-slack"


def test_loop_label_for_imap_loop() -> None:
    row = {"source": "comm", "source_ref": "imap:hendrikdpm:42", "kind": "incoming_question"}
    assert _loop_label(row) == "rosa-mail"


def test_loop_label_for_plaud_loop() -> None:
    row = {"source": "plaud", "source_ref": "meeting:7:self:slug", "kind": "meeting_action_self"}
    assert _loop_label(row) == "rosa-meeting"


# --- date conversion ------------------------------------------------------

def test_to_rfc3339_uses_utc_z() -> None:
    # 2026-04-29 13:00:00 NL = 11:00:00 UTC
    from datetime import datetime
    from zoneinfo import ZoneInfo
    nl = datetime(2026, 4, 29, 13, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
    iso = to_rfc3339(int(nl.timestamp()))
    assert iso == "2026-04-29T11:00:00Z"


# --- push: reminders ------------------------------------------------------

def test_push_creates_task_for_new_reminder(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Bel verzekering", int(_time.time()) + 3600),
        )
    fake = _FakeTodoist()
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=False)
    assert pushed == 1
    assert len(fake.tasks) == 1
    task = list(fake.tasks.values())[0]
    # Content krijgt [DD mmm] prefix zodat recurring reminders in Todoist
    # visueel onderscheidbaar zijn — body-text moet er nog wel in zitten.
    assert "Bel verzekering" in task.content
    assert task.content.startswith("[")
    assert "rosa-reminder" in task.labels


def test_push_dedups_existing_link(db: Path) -> None:
    with sqlite3.connect(db) as c:
        cur = c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Test", int(_time.time()) + 3600),
        )
        rid = cur.lastrowid
        insert_link(c, kind="reminder", local_id=rid, todoist_id="t999")
    fake = _FakeTodoist()
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=False)
    assert pushed == 0


def test_push_skips_cancelled_reminder(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at, cancelled_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            ("h1", "Cancelled", int(_time.time()) + 3600),
        )
    fake = _FakeTodoist()
    assert push_pending(db, fake, _PROJECT, review_queue_loops=False) == 0


# --- push: open_loops -----------------------------------------------------

def test_push_creates_task_for_actionable_loop(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:gmail:abc",
            kind="incoming_question", who="piet@klant.nl",
            title="Reageert op offerte?",
            body_excerpt="Hi Hendrik, kun je laten weten wanneer de offerte komt?",
        ))
    fake = _FakeTodoist()
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=False)
    assert pushed == 1
    task = list(fake.tasks.values())[0]
    tid = task.id
    # MED-3: who-prefix mag NIET meer naar Todoist (zou persoonsnaam /
    # email lekken). Title blijft als content (Llama-extracted, doelmatig).
    assert "piet@klant.nl" not in task.content
    assert "Reageert op offerte" in task.content
    assert task.labels == ["rosa-mail"]
    # MED-3: body_excerpt mag NIET meer naar Todoist (mail-content leak).
    # Description bevat alleen een dashboard-ref + loop-id.
    desc = fake.descriptions[tid]
    assert desc is not None
    assert "Hi Hendrik" not in desc
    assert "kun je laten weten" not in desc
    assert "127.0.0.1:8080" in desc
    assert "loop #" in desc


def test_push_skips_outgoing_request_loops(db: Path) -> None:
    """Delegate-tracking items horen NIET op Hendrik's Todoist-lijst."""
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:gmail:xyz",
            kind="outgoing_request", who="anouk@partner.nl",
            title="Vroeg om goedkeuring",
        ))
        insert_loop(c, OpenLoop(
            source="plaud", source_ref="meeting:1:other:slug",
            kind="meeting_action_other", who="Piet",
            title="Stuurt vrijdag goedkeuring",
        ))
    fake = _FakeTodoist()
    assert push_pending(db, fake, _PROJECT, review_queue_loops=False) == 0


def test_push_includes_meeting_action_self(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="plaud", source_ref="meeting:1:self:slug",
            kind="meeting_action_self", who="Hendrik",
            title="Offerte aanpassen",
        ))
    fake = _FakeTodoist()
    assert push_pending(db, fake, _PROJECT, review_queue_loops=False) == 1
    task = list(fake.tasks.values())[0]
    assert task.labels == ["rosa-meeting"]


# --- pull: completions ---------------------------------------------------

def test_pull_marks_remote_completed_reminder_as_cancelled(db: Path) -> None:
    with sqlite3.connect(db) as c:
        cur = c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Bel", int(_time.time()) + 3600),
        )
        rid = cur.lastrowid
    fake = _FakeTodoist()
    push_pending(db, fake, _PROJECT, review_queue_loops=False)
    # Mark remote completed
    tid = list(fake.tasks.keys())[0]
    fake.completed_ids.add(tid)

    n = pull_completions(db, fake, _PROJECT)
    assert n == 1
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT cancelled_at FROM reminders WHERE id=?", (rid,)).fetchone()
        assert row[0] is not None


def test_pull_marks_remote_completed_loop_as_done(db: Path) -> None:
    with sqlite3.connect(db) as c:
        lid = insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:gmail:xx",
            kind="incoming_question", who="x@x.com", title="t",
        ))
    fake = _FakeTodoist()
    push_pending(db, fake, _PROJECT, review_queue_loops=False)
    tid = list(fake.tasks.keys())[0]
    fake.completed_ids.add(tid)

    n = pull_completions(db, fake, _PROJECT)
    assert n == 1
    with sqlite3.connect(db) as c:
        status, via = c.execute(
            "SELECT status, resolved_via FROM open_loops WHERE id=?", (lid,),
        ).fetchone()
        assert status == "done"
        assert via == "todoist"


def test_pull_no_completions_no_changes(db: Path) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Bel", int(_time.time()) + 3600),
        )
    fake = _FakeTodoist()
    push_pending(db, fake, _PROJECT, review_queue_loops=False)
    n = pull_completions(db, fake, _PROJECT)
    assert n == 0


def test_push_stops_when_project_full(db: Path) -> None:
    """Bij MAX_ITEMS_LIMIT_REACHED → stop retry-storm. Eerste reminder
    landt nog (raise_after=1), tweede triggert de TodoistProjectFullError,
    rest van de tick wordt geskipt."""
    with sqlite3.connect(db) as c:
        for i in range(5):
            c.execute(
                "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
                (f"h{i}", f"Bel {i}", int(_time.time()) + 3600 + i),
            )
    fake = _FakeTodoist(raise_project_full_after=1)
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=False)
    # Exactly één reminder gepushed; daarna stopt push_pending
    assert pushed == 1
    # Niet alle 5 zijn naar create_task gegaan
    assert fake._call_count <= 2
