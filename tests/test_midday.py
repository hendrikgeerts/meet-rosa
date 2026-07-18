"""Tests voor core.midday — context-collectie split correct in events
voorbij/komend, reminders gevuurd/openstaand, en open loops."""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.midday import collect_midday_context
from extensions import reminders
from extensions.comm_intel.schema import init_comm_schema
from extensions.open_loops.schema import (
    OpenLoop, init_open_loops_schema, insert_loop,
)

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "midday.db"
    reminders.init_reminders_schema(p)
    init_open_loops_schema(p)
    init_comm_schema(p)
    return p


def _make_event(start: datetime, title: str) -> dict[str, Any]:
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
    }


def test_split_events_into_passed_and_remaining(db: Path) -> None:
    now = datetime.now(TZ)
    morning_event = _make_event(now - timedelta(hours=3), "Standup")
    afternoon_event = _make_event(now + timedelta(hours=3), "Klantcall")

    cal = MagicMock()
    # collect_midday_context calls list_events twice: passed first, then remaining
    cal.list_events.side_effect = [
        [morning_event],   # time_min=start_today, time_max=now
        [afternoon_event], # time_min=now, time_max=start_tomorrow
    ]
    gmail = MagicMock()

    ctx = collect_midday_context(gmail=gmail, calendar=cal, db_path=db)
    assert ctx["events_passed_today"] == [morning_event]
    assert ctx["events_remaining_today"] == [afternoon_event]


def test_reminders_fired_vs_remaining(db: Path) -> None:
    now = datetime.now(TZ)
    start_today = datetime.combine(now.date(), time(0, 0), tzinfo=TZ)
    end_today = start_today + timedelta(days=1) - timedelta(minutes=1)

    with sqlite3.connect(db) as c:
        # Ochtend: tussen 00:00 en min(now, 9u) — sent in het verleden
        morning_at = int(min(start_today + timedelta(hours=9), now - timedelta(minutes=5)).timestamp())
        # Middag: tussen now en eind van vandaag — pending
        afternoon_at = int(min(now + timedelta(hours=2), end_today).timestamp())
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at, sent_at) "
            "VALUES (?, ?, ?, ?)",
            ("h1", "ochtend-call", morning_at, morning_at),
        )
        c.execute(
            "INSERT INTO reminders (handle, body, remind_at) "
            "VALUES (?, ?, ?)",
            ("h1", "middag-task", afternoon_at),
        )

    cal = MagicMock()
    cal.list_events.return_value = []
    gmail = MagicMock()

    ctx = collect_midday_context(gmail=gmail, calendar=cal, db_path=db)
    fired_bodies = [r["body"] for r in ctx["reminders_fired_today"]]
    remaining_bodies = [r["body"] for r in ctx["reminders_remaining_today"]]
    assert fired_bodies == ["ochtend-call"]
    assert remaining_bodies == ["middag-task"]


def test_open_loops_split_inbound_and_waiting(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="m1", kind="incoming_question",
            who="piet@klant.nl", title="Reageert Hendrik op offerte?",
        ))
        insert_loop(c, OpenLoop(
            source="comm", source_ref="m2", kind="outgoing_request",
            who="anouk@partner.nl", title="Vroeg om goedkeuring",
        ))

    cal = MagicMock()
    cal.list_events.return_value = []
    gmail = MagicMock()

    ctx = collect_midday_context(gmail=gmail, calendar=cal, db_path=db)
    inbound_titles = [l["title"] for l in ctx["open_loops_inbound"]]
    waiting_titles = [l["title"] for l in ctx["open_loops_waiting"]]
    assert "Reageert Hendrik op offerte?" in inbound_titles
    assert "Vroeg om goedkeuring" in waiting_titles


def test_calendar_failure_falls_back_to_empty(db: Path) -> None:
    cal = MagicMock()
    cal.list_events.side_effect = RuntimeError("calendar down")
    gmail = MagicMock()

    ctx = collect_midday_context(gmail=gmail, calendar=cal, db_path=db)
    assert ctx["events_passed_today"] == []
    assert ctx["events_remaining_today"] == []


def test_comm_volume_today_counts_per_source(db: Path) -> None:
    now_unix = int(_time.time())
    yesterday_unix = now_unix - 86400 * 2
    with sqlite3.connect(db) as c:
        # 3 mails vandaag, 2 slack vandaag, 1 mail gisteren (telt niet)
        for i in range(3):
            c.execute(
                "INSERT INTO comm_items (source, account, external_id, "
                "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
                ("gmail", "gmail", f"m{i}", "in", now_unix - i * 60, "x"),
            )
        for i in range(2):
            c.execute(
                "INSERT INTO comm_items (source, account, external_id, "
                "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
                ("slack", "ws1", f"s{i}", "in", now_unix - i * 60, "x"),
            )
        c.execute(
            "INSERT INTO comm_items (source, account, external_id, "
            "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
            ("gmail", "gmail", "old1", "in", yesterday_unix, "x"),
        )

    cal = MagicMock(); cal.list_events.return_value = []
    gmail = MagicMock()
    ctx = collect_midday_context(gmail=gmail, calendar=cal, db_path=db)
    assert ctx["comm_volume_today"] == {"gmail": 3, "slack": 2}


def test_comm_open_counts_split_by_source_ref(db: Path) -> None:
    with sqlite3.connect(db) as c:
        # 2 mail-loops (gmail + imap), 1 slack-loop, allemaal incoming_question
        for src, account, ext in [
            ("gmail", "gmail", "g1"),
            ("imap", "hendrikdpm", "i1"),
            ("slack", "ws1", "s1"),
        ]:
            insert_loop(c, OpenLoop(
                source="comm",
                source_ref=f"{src}:{account}:{ext}",
                kind="incoming_question",
                who=f"sender@{src}.example",
                title=f"vraag uit {src}",
            ))

    cal = MagicMock(); cal.list_events.return_value = []
    gmail = MagicMock()
    ctx = collect_midday_context(gmail=gmail, calendar=cal, db_path=db)
    counts = ctx["comm_open_counts"]
    assert counts == {"gmail": 1, "imap": 1, "slack": 1}
    sources = sorted(l["source"] for l in ctx["open_loops_inbound"])
    assert sources == ["gmail", "imap", "slack"]
