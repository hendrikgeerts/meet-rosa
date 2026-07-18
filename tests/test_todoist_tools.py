"""Tests voor extensions.todoist_sync.tools — Rosa's lees/schrijf-tools
op Todoist. Geen netwerk; TodoistClient gefaked."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from extensions.todoist_sync.tools import (
    TODOIST_HANDLERS,
    TODOIST_TOOL_SCHEMAS,
)
from integrations.todoist import Task


@dataclass
class _FakeTodoist:
    tasks: list[Task] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def list_tasks(self, *, project_id: str | None = None) -> list[Task]:
        return list(self.tasks)

    def close_task(self, task_id: str) -> bool:
        self.closed.append(task_id)
        return True

    def update_task(self, task_id: str, **kwargs: Any) -> bool:
        self.updates.append((task_id, kwargs))
        return True

    def create_task(
        self, *, content: str, project_id: str | None = None,
        labels: list[str] | None = None, due_datetime: str | None = None,
        description: str | None = None, **_: Any,
    ) -> Task:
        tid = f"new-{len(self.tasks) + 1}"
        t = Task(
            id=tid, content=content, project_id=project_id,
            is_completed=False, labels=list(labels or []),
            due_date=None, due_datetime=due_datetime,
        )
        self.tasks.append(t)
        return t


def _make_task(
    tid: str, content: str, *,
    due_date: str | None = None, due_datetime: str | None = None,
    labels: list[str] | None = None,
) -> Task:
    return Task(
        id=tid, content=content, project_id="p1", is_completed=False,
        labels=labels or [], due_date=due_date, due_datetime=due_datetime,
    )


def _today() -> str:
    from core.timezone import current_tz
    return datetime.now(current_tz()).date().isoformat()


def _shift(today_iso: str, days: int) -> str:
    return (datetime.fromisoformat(today_iso) + timedelta(days=days)).date().isoformat()


# ---- schema sanity -----------------------------------------------------

def test_tool_schemas_have_required_names() -> None:
    names = {s["name"] for s in TODOIST_TOOL_SCHEMAS}
    assert names == {
        "todoist_list_open_tasks",
        "todoist_complete_task",
        "todoist_update_task",
        "todoist_create_task",
        "todoist_search",
        "todoist_cleanup_suggest",
        "todoist_cleanup_apply",
        "todoist_review_queue_list",
        "todoist_review_queue_approve",
        "todoist_review_queue_reject",
    }


# ---- todoist_create_task ----------------------------------------------

def test_create_task_happy_path() -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_create_task"](
        fake, "p1", {"content": "Bel verzekeraar", "labels": ["urgent"]},
    )
    assert "error" not in out
    assert out["content"] == "Bel verzekeraar"
    assert "urgent" in out["labels"]


def test_create_task_requires_content() -> None:
    out = TODOIST_HANDLERS["todoist_create_task"](_FakeTodoist(), "p1", {})
    assert "error" in out


def test_create_task_rejects_bad_iso() -> None:
    out = TODOIST_HANDLERS["todoist_create_task"](
        _FakeTodoist(), "p1",
        {"content": "x", "due_datetime": "morgen 10:00"},
    )
    assert "error" in out
    assert "ISO" in out["error"]


def test_create_task_handles_project_full() -> None:
    from integrations.todoist import TodoistProjectFullError

    class _FullFake(_FakeTodoist):
        def create_task(self, **kwargs):  # type: ignore[override]
            raise TodoistProjectFullError("full")

    out = TODOIST_HANDLERS["todoist_create_task"](
        _FullFake(), "p1", {"content": "x"},
    )
    assert "error" in out
    assert "full" in out["error"].lower() or "cleanup" in out["error"].lower()


def test_create_task_without_client_errors() -> None:
    out = TODOIST_HANDLERS["todoist_create_task"](None, None, {"content": "x"})
    assert "error" in out


def test_create_task_truncates_content() -> None:
    fake = _FakeTodoist()
    TODOIST_HANDLERS["todoist_create_task"](
        fake, "p1", {"content": "x" * 1000},
    )
    # FakeTodoist creates Task with the content; just check no crash
    assert len(fake.tasks) == 1


def test_handlers_cover_all_schemas() -> None:
    names = {s["name"] for s in TODOIST_TOOL_SCHEMAS}
    assert set(TODOIST_HANDLERS) == names


# ---- todoist_list_open_tasks ------------------------------------------

def test_list_today_filter_picks_only_today() -> None:
    today = _today()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "today", due_date=today),
        _make_task("b", "tomorrow", due_date=_shift(today, 1)),
        _make_task("c", "no due"),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](fake, "p1", {"filter": "today"})
    assert out["count"] == 1
    assert out["tasks"][0]["id"] == "a"


def test_list_overdue_filter() -> None:
    today = _today()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "today", due_date=today),
        _make_task("b", "yesterday", due_date=_shift(today, -1)),
        _make_task("c", "older", due_date=_shift(today, -30)),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](fake, "p1", {"filter": "overdue"})
    assert out["count"] == 2
    assert {t["id"] for t in out["tasks"]} == {"b", "c"}
    # sorted ascending by due_date
    assert out["tasks"][0]["id"] == "c"


def test_list_week_filter() -> None:
    today = _today()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "today", due_date=today),
        _make_task("b", "in3d", due_date=_shift(today, 3)),
        _make_task("c", "in10d", due_date=_shift(today, 10)),
        _make_task("d", "yesterday", due_date=_shift(today, -1)),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](fake, "p1", {"filter": "week"})
    assert {t["id"] for t in out["tasks"]} == {"a", "b"}


def test_list_nodue_filter() -> None:
    today = _today()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "today", due_date=today),
        _make_task("b", "no due"),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](fake, "p1", {"filter": "nodue"})
    assert out["count"] == 1
    assert out["tasks"][0]["id"] == "b"


def test_list_all_returns_everything() -> None:
    fake = _FakeTodoist(tasks=[
        _make_task("a", "x"), _make_task("b", "y"),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](fake, "p1", {"filter": "all"})
    assert out["count"] == 2


def test_list_due_datetime_used_when_date_absent() -> None:
    today = _today()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "today via dt", due_datetime=f"{today}T15:00:00Z"),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](fake, "p1", {"filter": "today"})
    assert out["count"] == 1


def test_list_query_filter() -> None:
    today = _today()
    fake = _FakeTodoist(tasks=[
        _make_task("a", "Bel verzekering", due_date=today),
        _make_task("b", "Mail boekhouder", due_date=today),
    ])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](
        fake, "p1", {"filter": "today", "query": "verzekering"},
    )
    assert out["count"] == 1
    assert out["tasks"][0]["id"] == "a"


def test_list_query_too_short_rejected() -> None:
    fake = _FakeTodoist(tasks=[_make_task("a", "x")])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](
        fake, "p1", {"filter": "all", "query": "ab"},
    )
    assert "error" in out


def test_list_query_wildcard_rejected() -> None:
    fake = _FakeTodoist(tasks=[_make_task("a", "x")])
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](
        fake, "p1", {"filter": "all", "query": "abc%"},
    )
    assert "error" in out


def test_list_unknown_filter_errors() -> None:
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](
        _FakeTodoist(), "p1", {"filter": "yesterday"},
    )
    assert "error" in out


def test_list_limit_applies() -> None:
    today = _today()
    tasks = [_make_task(f"t{i}", f"c{i}", due_date=today) for i in range(50)]
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](
        _FakeTodoist(tasks=tasks), "p1", {"filter": "today", "limit": 5},
    )
    assert out["count"] == 50  # total matched
    assert len(out["tasks"]) == 5


def test_list_without_client_errors() -> None:
    out = TODOIST_HANDLERS["todoist_list_open_tasks"](None, None, {"filter": "today"})
    assert "error" in out
    assert "todoist" in out["error"].lower()


# ---- todoist_complete_task --------------------------------------------

def test_complete_task_happy_path() -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_complete_task"](fake, "p1", {"task_id": "abc"})
    assert out == {"task_id": "abc", "completed": True}
    assert fake.closed == ["abc"]


def test_complete_task_missing_id_errors() -> None:
    out = TODOIST_HANDLERS["todoist_complete_task"](_FakeTodoist(), "p1", {})
    assert "error" in out


def test_complete_without_client_errors() -> None:
    out = TODOIST_HANDLERS["todoist_complete_task"](None, None, {"task_id": "abc"})
    assert "error" in out


# ---- todoist_update_task ----------------------------------------------

def test_update_task_with_content() -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_update_task"](
        fake, "p1", {"task_id": "abc", "content": "Bel verzekeraar"},
    )
    assert out["updated"] is True
    assert out["fields"] == ["content"]
    assert fake.updates == [("abc", {"content": "Bel verzekeraar"})]


def test_update_task_with_multiple_fields() -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_update_task"](
        fake, "p1", {
            "task_id": "abc", "content": "X", "due_datetime": "2026-07-01T10:00:00",
        },
    )
    assert set(out["fields"]) == {"content", "due_datetime"}


def test_update_task_without_fields_errors() -> None:
    out = TODOIST_HANDLERS["todoist_update_task"](
        _FakeTodoist(), "p1", {"task_id": "abc"},
    )
    assert "error" in out


def test_update_task_missing_id_errors() -> None:
    out = TODOIST_HANDLERS["todoist_update_task"](
        _FakeTodoist(), "p1", {"content": "x"},
    )
    assert "error" in out


def test_update_rejects_non_iso_due_datetime() -> None:
    """L2: relative dates ('morgen') horen niet via due_datetime te
    gaan — Todoist API parsed dat niet en geeft stille 400."""
    out = TODOIST_HANDLERS["todoist_update_task"](
        _FakeTodoist(), "p1",
        {"task_id": "abc", "due_datetime": "morgen 14:00"},
    )
    assert "error" in out
    assert "ISO 8601" in out["error"]


def test_update_accepts_iso_with_z() -> None:
    fake = _FakeTodoist()
    out = TODOIST_HANDLERS["todoist_update_task"](
        fake, "p1",
        {"task_id": "abc", "due_datetime": "2026-06-30T15:00:00Z"},
    )
    assert out["updated"] is True


def test_update_content_truncated_to_500() -> None:
    fake = _FakeTodoist()
    long_content = "x" * 1000
    TODOIST_HANDLERS["todoist_update_task"](
        fake, "p1", {"task_id": "abc", "content": long_content},
    )
    assert len(fake.updates[0][1]["content"]) == 500


# ---- todoist_search ----------------------------------------------------

def test_search_finds_match() -> None:
    fake = _FakeTodoist(tasks=[
        _make_task("a", "Bel verzekering"),
        _make_task("b", "Mail boekhouder"),
    ])
    out = TODOIST_HANDLERS["todoist_search"](fake, "p1", {"query": "verzekering"})
    assert out["count"] == 1
    assert out["tasks"][0]["id"] == "a"


def test_search_case_insensitive() -> None:
    fake = _FakeTodoist(tasks=[_make_task("a", "Bel Verzekering")])
    out = TODOIST_HANDLERS["todoist_search"](fake, "p1", {"query": "VERZEKERING"})
    assert out["count"] == 1


def test_search_rejects_short_query() -> None:
    out = TODOIST_HANDLERS["todoist_search"](_FakeTodoist(), "p1", {"query": "ab"})
    assert "error" in out


def test_search_rejects_wildcards() -> None:
    out = TODOIST_HANDLERS["todoist_search"](_FakeTodoist(), "p1", {"query": "ab%"})
    assert "error" in out


def test_search_no_match_returns_empty_list() -> None:
    fake = _FakeTodoist(tasks=[_make_task("a", "Bel verzekering")])
    out = TODOIST_HANDLERS["todoist_search"](fake, "p1", {"query": "boekhouder"})
    assert out["count"] == 0
    assert out["tasks"] == []
