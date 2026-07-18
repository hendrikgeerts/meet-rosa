"""Tests voor extensions.todoist_sync.briefing — pulse-builders voor
ochtend-briefing en midday-update."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from extensions.todoist_sync.briefing import (
    build_todoist_midday_pulse, build_todoist_pulse,
)
from integrations.todoist import Task


@dataclass
class _FakeClient:
    tasks: list[Task] = field(default_factory=list)
    raise_on_list: bool = False

    def list_tasks(self, *, project_id: str | None = None) -> list[Task]:
        if self.raise_on_list:
            raise RuntimeError("api down")
        return list(self.tasks)


def _t(
    tid: str, content: str, *,
    due_date: str | None = None, due_datetime: str | None = None,
) -> Task:
    return Task(
        id=tid, content=content, project_id="p1", is_completed=False,
        labels=[], due_date=due_date, due_datetime=due_datetime,
    )


# ---- build_todoist_pulse (ochtend) ------------------------------------

def test_pulse_returns_empty_when_client_none() -> None:
    pulse = build_todoist_pulse(None, today=date(2026, 6, 27))
    assert pulse["available"] is False
    assert pulse["today"] == []
    assert pulse["overdue_count"] == 0


def test_pulse_separates_today_and_overdue() -> None:
    client = _FakeClient(tasks=[
        _t("a", "today", due_date="2026-06-27"),
        _t("b", "yesterday", due_date="2026-06-26"),
        _t("c", "tomorrow", due_date="2026-06-28"),
        _t("d", "no due"),
    ])
    pulse = build_todoist_pulse(client, today=date(2026, 6, 27))
    assert pulse["available"] is True
    assert pulse["today_count"] == 1
    assert pulse["today"][0]["id"] == "a"
    assert pulse["overdue_count"] == 1
    assert pulse["overdue"][0]["id"] == "b"


def test_pulse_overdue_sorted_oldest_first() -> None:
    client = _FakeClient(tasks=[
        _t("recent", "x", due_date="2026-06-26"),
        _t("ancient", "y", due_date="2026-01-01"),
    ])
    pulse = build_todoist_pulse(client, today=date(2026, 6, 27))
    assert [t["id"] for t in pulse["overdue"]] == ["ancient", "recent"]


def test_pulse_respects_today_limit() -> None:
    client = _FakeClient(tasks=[
        _t(f"t{i}", f"c{i}", due_date="2026-06-27") for i in range(10)
    ])
    pulse = build_todoist_pulse(client, today=date(2026, 6, 27), today_limit=3)
    assert pulse["today_count"] == 10
    assert len(pulse["today"]) == 3


def test_pulse_uses_due_datetime_when_date_missing() -> None:
    client = _FakeClient(tasks=[
        _t("a", "x", due_datetime="2026-06-27T15:00:00Z"),
    ])
    pulse = build_todoist_pulse(client, today=date(2026, 6, 27))
    assert pulse["today_count"] == 1


def test_pulse_swallows_api_failure_returns_unavailable() -> None:
    client = _FakeClient(raise_on_list=True)
    pulse = build_todoist_pulse(client, today=date(2026, 6, 27))
    assert pulse["available"] is False
    assert pulse["today"] == []


# ---- build_todoist_midday_pulse ---------------------------------------

def test_midday_pulse_lists_remaining_today_only() -> None:
    today = date(2026, 6, 27)
    client = _FakeClient(tasks=[
        _t("a", "today", due_date=today.isoformat()),
        _t("b", "tomorrow", due_date=(today + timedelta(days=1)).isoformat()),
        _t("c", "yesterday", due_date=(today - timedelta(days=1)).isoformat()),
    ])
    now = datetime(2026, 6, 27, 12, 30)
    pulse = build_todoist_midday_pulse(client, now=now)
    assert pulse["available"] is True
    assert pulse["remaining_count"] == 1
    assert pulse["remaining_today"][0]["id"] == "a"


def test_midday_pulse_handles_no_client() -> None:
    pulse = build_todoist_midday_pulse(None)
    assert pulse["available"] is False
    assert pulse["remaining_today"] == []


def test_midday_pulse_respects_limit() -> None:
    today = date(2026, 6, 27)
    client = _FakeClient(tasks=[
        _t(f"t{i}", f"c{i}", due_date=today.isoformat()) for i in range(8)
    ])
    pulse = build_todoist_midday_pulse(
        client, now=datetime(2026, 6, 27, 12, 0), remaining_limit=3,
    )
    assert pulse["remaining_count"] == 8
    assert len(pulse["remaining_today"]) == 3


def test_midday_pulse_swallows_api_failure() -> None:
    pulse = build_todoist_midday_pulse(_FakeClient(raise_on_list=True))
    assert pulse["available"] is False


# ---- review 27/6 M3: TZ-aware due_datetime ----------------------------

def test_pulse_due_datetime_utc_z_converts_to_local_date() -> None:
    """M3: 2026-06-26T23:30:00Z = 27 juni 01:30 NL (zomertijd, UTC+2).
    Zonder TZ-conversie zou [:10] '2026-06-26' geven en zou de taak
    als 'overdue' in de briefing van 27 juni verschijnen — bug."""
    from zoneinfo import ZoneInfo
    client = _FakeClient(tasks=[
        _t("a", "vannacht NL", due_datetime="2026-06-26T23:30:00Z"),
    ])
    pulse = build_todoist_pulse(
        client, today=date(2026, 6, 27), tz=ZoneInfo("Europe/Amsterdam"),
    )
    # In NL is dit op 27 juni 01:30 → 'today', niet overdue
    assert pulse["today_count"] == 1
    assert pulse["overdue_count"] == 0


def test_pulse_due_datetime_handles_naive_iso() -> None:
    """Naive ISO (geen Z, geen offset) → fallback op string-prefix
    zodat parse-failure de hele pulse niet kapotmaakt."""
    from zoneinfo import ZoneInfo
    client = _FakeClient(tasks=[
        _t("a", "geen TZ-info", due_datetime="2026-06-27T15:00:00"),
    ])
    pulse = build_todoist_pulse(
        client, today=date(2026, 6, 27), tz=ZoneInfo("Europe/Amsterdam"),
    )
    assert pulse["today_count"] == 1
