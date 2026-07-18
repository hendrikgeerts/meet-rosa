"""Tests voor de review-queue: open_loops landen in todoist_push_queue
i.p.v. direct in Todoist; Hendrik approve't of reject't per item."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from extensions import reminders
from extensions.open_loops.schema import (
    OpenLoop,
    init_open_loops_schema,
    insert_loop,
)
from extensions.todoist_sync.schema import (
    get_link_by_local,
    init_todoist_sync_schema,
    queue_enqueue_loop,
    queue_get,
    queue_list_pending,
    queue_mark_approved,
    queue_mark_rejected,
    queue_pending_count,
)
from extensions.todoist_sync.sync import push_pending
from extensions.todoist_sync.tools import TODOIST_HANDLERS
from integrations.todoist import Project, Task, TodoistProjectFullError


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "rq.db"
    reminders.init_reminders_schema(p)
    init_open_loops_schema(p)
    init_todoist_sync_schema(p)
    return p


@dataclass
class _FakeTodoist:
    next_id: int = 1
    created: list[Task] = field(default_factory=list)
    raise_full: bool = False

    def create_task(
        self, *, content: str, project_id: str | None = None,
        labels: list[str] | None = None,
        due_datetime: str | None = None,
        description: str | None = None, **_: Any,
    ) -> Task:
        if self.raise_full:
            raise TodoistProjectFullError("full")
        tid = f"t{self.next_id}"
        self.next_id += 1
        t = Task(
            id=tid, content=content, project_id=project_id,
            is_completed=False, labels=list(labels or []),
            due_date=None, due_datetime=due_datetime,
        )
        self.created.append(t)
        return t

    def close_task(self, _id: str) -> bool:
        return True

    def list_tasks(self, *, project_id: str | None = None) -> list[Task]:
        return list(self.created)


_PROJECT = Project(id="p1", name="Rosa")


# ---- schema/queue helpers ----------------------------------------------

def test_enqueue_then_list_pending(db: Path) -> None:
    with sqlite3.connect(db) as c:
        queue_enqueue_loop(
            c, loop_id=42, kind="incoming_question",
            label="rosa-mail", title="Reageer op offerte",
        )
        pending = queue_list_pending(c)
    assert len(pending) == 1
    assert pending[0]["loop_id"] == 42
    assert pending[0]["state"] == "pending"


def test_enqueue_dedups_on_loop_id(db: Path) -> None:
    with sqlite3.connect(db) as c:
        ok1 = queue_enqueue_loop(c, loop_id=1, kind="x",
                                  label="rosa-mail", title="a")
        ok2 = queue_enqueue_loop(c, loop_id=1, kind="x",
                                  label="rosa-mail", title="a")
        count = queue_pending_count(c)
    assert ok1 is True
    assert ok2 is False
    assert count == 1


def test_mark_approved_updates_state(db: Path) -> None:
    with sqlite3.connect(db) as c:
        queue_enqueue_loop(c, loop_id=42, kind="x",
                            label="rosa-mail", title="a")
        rows = queue_list_pending(c)
        qid = rows[0]["id"]
        queue_mark_approved(c, queue_id=qid, todoist_id="tABC")
        row = queue_get(c, qid)
    assert row["state"] == "approved"
    assert row["todoist_id"] == "tABC"


def test_mark_rejected_updates_state(db: Path) -> None:
    with sqlite3.connect(db) as c:
        queue_enqueue_loop(c, loop_id=42, kind="x",
                            label="rosa-mail", title="a")
        rows = queue_list_pending(c)
        qid = rows[0]["id"]
        queue_mark_rejected(c, qid)
        row = queue_get(c, qid)
    assert row["state"] == "rejected"


# ---- push_pending with review_queue_loops -----------------------------

def test_push_pending_routes_loops_to_queue_when_enabled(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="anouk@klant.nl",
            title="Reageer op offerte",
        ))
    fake = _FakeTodoist()
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=True)
    assert pushed == 0  # niets naar Todoist
    assert fake.created == []
    with sqlite3.connect(db) as c:
        assert queue_pending_count(c) == 1


def test_push_pending_pushes_reminders_regardless(db: Path) -> None:
    """Reminders MOETEN auto-syncen — alleen open_loops gaan via queue."""
    import time as _t
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h1", "Bel verzekering", int(_t.time()) + 3600),
        )
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:xyz",
            kind="incoming_question", who="x@y.nl", title="Vraag iets",
        ))
    fake = _FakeTodoist()
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=True)
    assert pushed == 1  # alleen de reminder
    assert len(fake.created) == 1
    assert "rosa-reminder" in fake.created[0].labels


def test_push_pending_legacy_mode_pushes_loops_directly(db: Path) -> None:
    """Backwards-compat: review_queue_loops=False → oude gedrag."""
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="x@y.nl", title="Vraag",
        ))
    fake = _FakeTodoist()
    pushed = push_pending(db, fake, _PROJECT, review_queue_loops=False)
    assert pushed == 1
    assert len(fake.created) == 1
    with sqlite3.connect(db) as c:
        assert queue_pending_count(c) == 0


# ---- review-queue tools (list/approve/reject) -------------------------

def test_tool_list_returns_pending_items(db: Path) -> None:
    with sqlite3.connect(db) as c:
        queue_enqueue_loop(c, loop_id=1, kind="incoming_question",
                            label="rosa-mail", title="a")
        queue_enqueue_loop(c, loop_id=2, kind="incoming_task",
                            label="rosa-slack", title="b")
    out = TODOIST_HANDLERS["todoist_review_queue_list"](db, None, None, {})
    assert out["count"] == 2
    assert {item["loop_id"] for item in out["items"]} == {1, 2}


def test_tool_approve_pushes_to_todoist_and_links_loop(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="x@y.nl", title="Reageer op offerte",
        ))
        loop_id = c.execute("SELECT id FROM open_loops").fetchone()[0]
        queue_enqueue_loop(c, loop_id=loop_id, kind="incoming_question",
                            label="rosa-mail", title="Reageer op offerte")
        qid = c.execute(
            "SELECT id FROM todoist_push_queue WHERE loop_id=?", (loop_id,),
        ).fetchone()[0]

    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_review_queue_approve"](
        db, fake, "p1", {"queue_ids": [qid]},
    )
    assert len(out["pushed"]) == 1
    assert len(fake.created) == 1
    with sqlite3.connect(db) as c:
        row = queue_get(c, qid)
        link = get_link_by_local(c, kind="open_loop", local_id=loop_id)
    assert row["state"] == "approved"
    assert link is not None
    assert link["todoist_id"] == fake.created[0].id


def test_tool_approve_caps_batch_size(db: Path) -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_review_queue_approve"](
        db, fake, "p1", {"queue_ids": list(range(50))},
    )
    assert "error" in out


def test_tool_approve_project_full_stops_loop(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="x@y.nl", title="a",
        ))
        loop_id = c.execute("SELECT id FROM open_loops").fetchone()[0]
        queue_enqueue_loop(c, loop_id=loop_id, kind="x",
                            label="rosa-mail", title="a")
        qid = c.execute(
            "SELECT id FROM todoist_push_queue WHERE loop_id=?", (loop_id,),
        ).fetchone()[0]

    fake = _FakeTodoist(raise_full=True)
    out = TODOIST_HANDLERS["todoist_review_queue_approve"](
        db, fake, "p1", {"queue_ids": [qid]},
    )
    assert out["project_full"] is True
    assert out["pushed"] == []
    with sqlite3.connect(db) as c:
        row = queue_get(c, qid)
    assert row["state"] == "pending"  # niet gemarkeerd


def test_tool_reject_marks_state(db: Path) -> None:
    with sqlite3.connect(db) as c:
        queue_enqueue_loop(c, loop_id=42, kind="x", label="rosa-mail",
                            title="a")
        qid = c.execute(
            "SELECT id FROM todoist_push_queue WHERE loop_id=42",
        ).fetchone()[0]

    out = TODOIST_HANDLERS["todoist_review_queue_reject"](
        db, None, None, {"queue_ids": [qid]},
    )
    assert out["rejected"] == [qid]
    with sqlite3.connect(db) as c:
        row = queue_get(c, qid)
    assert row["state"] == "rejected"


def test_tool_approve_link_failure_keeps_queue_pending(db: Path) -> None:
    """H1 review-fix: als insert_link faalt na een succesvolle
    Todoist-create, mag de queue NIET als approved gemarkeerd worden —
    anders zou pull_completions de loop nooit dichten bij remote-ack."""
    from extensions.todoist_sync.schema import insert_link

    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="x@y.nl", title="a",
        ))
        loop_id = c.execute("SELECT id FROM open_loops").fetchone()[0]
        queue_enqueue_loop(c, loop_id=loop_id, kind="x",
                            label="rosa-mail", title="a")
        qid = c.execute(
            "SELECT id FROM todoist_push_queue WHERE loop_id=?", (loop_id,),
        ).fetchone()[0]
        # Forceer link-conflict: bestaande link met dummy todoist_id —
        # FakeTodoist gaat een nieuwe task aanmaken met andere id, dus
        # we creëren een (kind, local_id) clash door alvast een link
        # voor loop_id naar 'pre-existing' te zetten.
        insert_link(c, kind="open_loop", local_id=loop_id,
                     todoist_id="pre-existing")

    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_review_queue_approve"](
        db, fake, "p1", {"queue_ids": [qid]},
    )
    assert out["pushed"] == []
    assert len(out["failed"]) == 1
    assert out["failed"][0]["error"] == "link_insert_failed"
    with sqlite3.connect(db) as c:
        row = queue_get(c, qid)
    assert row["state"] == "pending"


def test_tool_unknown_queue_id_in_response(db: Path) -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_review_queue_approve"](
        db, fake, "p1", {"queue_ids": [99999]},
    )
    assert out["pushed"] == []
    assert out["unknown_queue_ids"] == [99999]
