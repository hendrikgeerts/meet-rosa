"""Tests voor core.weekend_prep — context-collection + ranking."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.weekend_prep import (
    _build_stale_items,
    _build_top_priorities,
    collect_weekend_prep_context,
)
from extensions.comm_intel.schema import init_comm_schema
from extensions.open_loops.schema import (
    OpenLoop,
    init_open_loops_schema,
    insert_loop,
)
from extensions.reminders import init_reminders_schema


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "wp.db"
    init_reminders_schema(p)
    init_open_loops_schema(p)
    init_comm_schema(p)
    return p


def _fake_calendar(events: list[dict[str, Any]] | None = None) -> Any:
    cal = MagicMock()
    cal.list_events.return_value = events or []
    return cal


# ---- _build_top_priorities --------------------------------------------

def test_top_priorities_prefers_todoist_overdue() -> None:
    wo = {
        "todoist": {
            "overdue": [{"content": "achterstallig", "due_date": "2026-01-01"}],
            "today": [{"content": "vandaag"}],
        },
        "loops_inbound": [{"title": "vraag piet", "age_days": 5, "who": "p"}],
    }
    out = _build_top_priorities(wo)
    assert out[0]["source"] == "todoist_overdue"
    assert out[0]["title"] == "achterstallig"


def test_top_priorities_falls_back_to_inbound_oldest_first() -> None:
    wo = {
        "todoist": {"overdue": [], "today": []},
        "loops_inbound": [
            {"title": "new", "age_days": 1, "who": "a"},
            {"title": "old", "age_days": 20, "who": "b"},
        ],
    }
    out = _build_top_priorities(wo)
    assert out[0]["title"] == "old"


def test_top_priorities_caps_at_three() -> None:
    wo = {
        "todoist": {
            "overdue": [{"content": f"t{i}", "due_date": "2026-01-01"} for i in range(10)],
            "today": [],
        },
        "loops_inbound": [],
    }
    assert len(_build_top_priorities(wo)) == 3


def test_top_priorities_round_robin_avoids_source_monoculture() -> None:
    """M1 review-fix: drie todoist-overdue items mogen niet alle
    slots wegnemen van een 60d oude VIP-vraag."""
    wo = {
        "todoist": {
            "overdue": [
                {"id": "t1", "content": "old1", "due_date": "2026-01-01"},
                {"id": "t2", "content": "old2", "due_date": "2026-01-02"},
                {"id": "t3", "content": "old3", "due_date": "2026-01-03"},
            ],
            "today": [],
        },
        "loops_inbound": [
            {"id": 99, "title": "VIP vergeten", "age_days": 60, "who": "ceo"},
        ],
    }
    out = _build_top_priorities(wo)
    sources = {p["source"] for p in out}
    assert "todoist_overdue" in sources
    assert "inbound_loop" in sources  # niet alle 3 slots door overdue gevuld


def test_stale_excludes_ids_from_top_priorities() -> None:
    """M3 review-fix: dezelfde loop hoort niet in beide secties."""
    wo = {
        "loops_inbound": [
            {"id": 42, "title": "ouwe", "age_days": 30, "source": "comm"},
        ],
        "loops_waiting": [],
        "loops_meeting": [],
    }
    out = _build_stale_items(wo, threshold_days=7, exclude_ids={42})
    assert out == []


def test_stale_includes_meeting_action_self() -> None:
    """M4 review-fix: Plaud meeting-actions kunnen ook stilletjes ouder
    worden — moeten in stale-walk meelopen."""
    wo = {
        "loops_inbound": [],
        "loops_waiting": [],
        "loops_meeting": [
            {"id": 7, "title": "Stuur Q3 cijfers", "age_days": 14,
             "source": "plaud"},
        ],
    }
    out = _build_stale_items(wo, threshold_days=7)
    assert len(out) == 1
    assert out[0]["id"] == 7


# ---- _build_stale_items -----------------------------------------------

def test_stale_filters_by_threshold() -> None:
    wo = {
        "loops_inbound": [
            {"id": 1, "title": "old", "age_days": 14, "source": "comm"},
            {"id": 2, "title": "fresh", "age_days": 2, "source": "comm"},
        ],
        "loops_waiting": [
            {"id": 3, "title": "waited", "age_days": 30, "source": "comm"},
        ],
    }
    out = _build_stale_items(wo, threshold_days=7)
    assert {item["id"] for item in out} == {1, 3}


def test_stale_sorted_oldest_first() -> None:
    wo = {
        "loops_inbound": [
            {"id": 1, "title": "a", "age_days": 8, "source": "comm"},
            {"id": 2, "title": "b", "age_days": 30, "source": "comm"},
        ],
        "loops_waiting": [],
    }
    out = _build_stale_items(wo, threshold_days=7)
    assert [i["id"] for i in out] == [2, 1]


def test_stale_caps_at_five() -> None:
    wo = {
        "loops_inbound": [
            {"id": i, "title": f"t{i}", "age_days": 10, "source": "comm"}
            for i in range(20)
        ],
        "loops_waiting": [],
    }
    assert len(_build_stale_items(wo, threshold_days=7)) == 5


# ---- collect_weekend_prep_context --------------------------------------

def test_collect_returns_required_keys(db: Path) -> None:
    out = collect_weekend_prep_context(
        calendar=_fake_calendar(), db_path=db,
    )
    for key in (
        "now", "week_start", "first_monday_event", "monday_event_count",
        "monday_events", "top_priorities", "stale_items",
        "week_reminders", "open_wishes", "totals_snapshot",
    ):
        assert key in out


def test_collect_pulls_week_reminders(db: Path) -> None:
    # Reminder voor "binnenkort" — week_reminders kijkt vanaf eerstvolgende
    # maandag. Plus eentje 30 dagen ver weg → out of window.
    in_window = int(_time.time()) + 86400 * 3   # binnen 7 dagen vanaf nu
    out_of_window = int(_time.time()) + 86400 * 60
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h", "in window", in_window),
        )
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) VALUES (?, ?, ?)",
            ("h", "far", out_of_window),
        )
    out = collect_weekend_prep_context(
        calendar=_fake_calendar(), db_path=db,
    )
    # Het exacte aantal hangt af van de huidige weekdag — we checken
    # alleen dat 'far' er sowieso NIET in zit.
    bodies = {r["body"] for r in out["week_reminders"]}
    assert "far" not in bodies


def test_collect_monday_events_passes_through_first_event(db: Path) -> None:
    events = [
        {"id": "e1", "title": "Standup", "start": "2026-06-29T09:00:00"},
        {"id": "e2", "title": "Klant", "start": "2026-06-29T11:00:00"},
    ]
    out = collect_weekend_prep_context(
        calendar=_fake_calendar(events), db_path=db,
    )
    assert out["monday_event_count"] == 2
    assert out["first_monday_event"]["id"] == "e1"


def test_collect_loops_into_top_priorities(db: Path) -> None:
    with sqlite3.connect(db) as c:
        # Forceer hoge age door created_at via OpenLoop default override.
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:hg:abc",
            kind="incoming_question", who="anouk@x.nl",
            title="Vraag review",
        ))
        # Hand-update created_at zodat age_days >0
        c.execute(
            "UPDATE open_loops SET created_at = ? WHERE who='anouk@x.nl'",
            (int(_time.time()) - 86400 * 10,),
        )
    out = collect_weekend_prep_context(
        calendar=_fake_calendar(), db_path=db,
    )
    # Top priority is uit todoist (none) → falls back to inbound loop
    assert out["top_priorities"]
    assert any("anouk" in (p.get("why_it_matters") or "") for p in out["top_priorities"])
