"""Tests voor pattern-detection schema, detector en tools."""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from extensions.comm_intel.schema import init_comm_schema
from extensions.decisions.schema import (
    init_decisions_schema,
)
from extensions.open_loops.schema import init_open_loops_schema
from extensions.patterns.detector import run_weekly_detection
from extensions.patterns.schema import (
    init_patterns_schema,
    insert_or_replace_pattern,
    list_patterns,
    mark_surfaced,
    pending_patterns,
    snooze_pattern,
)
from extensions.patterns.tools import (
    patterns_recent_handler,
    patterns_snooze_handler,
)
from extensions.plaud_intel.schema import init_plaud_meetings_schema
from integrations.plaud import init_plaud_schema

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "patterns.db"
    init_patterns_schema(p)
    init_comm_schema(p)
    init_decisions_schema(p)
    init_open_loops_schema(p)
    init_plaud_meetings_schema(p)
    init_plaud_schema(p)
    return p


def _ts(d: date) -> int:
    return int(datetime.combine(d, time(0, 0), tzinfo=TZ).timestamp())


# --- schema --------------------------------------------------------------

def test_insert_or_replace_idempotent(db: Path) -> None:
    week = _ts(date(2026, 4, 13))
    with sqlite3.connect(db) as c:
        insert_or_replace_pattern(
            c, week_start=week, kind="comm_volume_spike",
            severity="watch", title="X",
        )
        insert_or_replace_pattern(
            c, week_start=week, kind="comm_volume_spike",
            severity="alert", title="Y",
        )
        rows = c.execute("SELECT severity, title FROM patterns").fetchall()
    assert len(rows) == 1
    assert rows[0] == ("alert", "Y")


def test_invalid_kind_rejected(db: Path) -> None:
    with sqlite3.connect(db) as c, pytest.raises(ValueError):
        insert_or_replace_pattern(c, week_start=0, kind="weird",
                                   severity="info", title="x")


def test_pending_patterns_excludes_surfaced(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_or_replace_pattern(c, week_start=1, kind="comm_volume_spike",
                                    severity="watch", title="A")
        insert_or_replace_pattern(c, week_start=2, kind="comm_volume_spike",
                                    severity="alert", title="B")
        pending = pending_patterns(c, limit=10)
        assert len(pending) == 2
        # alert komt eerst
        assert pending[0]["title"] == "B"
        ids = [p["id"] for p in pending]
        mark_surfaced(c, ids)
        assert pending_patterns(c) == []


def test_snooze_pattern_hides_until(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_or_replace_pattern(c, week_start=1, kind="comm_volume_spike",
                                    severity="info", title="A")
        pid = c.execute("SELECT id FROM patterns").fetchone()[0]
        snooze_pattern(c, pid, days=7)
        assert pending_patterns(c) == []


def test_list_patterns_window(db: Path) -> None:
    with sqlite3.connect(db) as c:
        # Old (>8 weeks back via detected_at)
        c.execute(
            "INSERT INTO patterns (week_start, kind, severity, title, detected_at) "
            "VALUES (?,?,?,?,?)",
            (1, "comm_volume_spike", "info", "old",
             int(_time.time()) - 70 * 86400),
        )
        c.execute(
            "INSERT INTO patterns (week_start, kind, severity, title) "
            "VALUES (?,?,?,?)",
            (2, "decisions_slowing", "watch", "new"),
        )
        rows = list_patterns(c, weeks_back=8)
    titles = [r["title"] for r in rows]
    assert "new" in titles
    assert "old" not in titles


# --- detectors -----------------------------------------------------------

def test_comm_volume_spike_triggers(db: Path) -> None:
    today = date(2026, 4, 27)            # Monday
    last_week_start = today - timedelta(days=7)
    baseline_start = last_week_start - timedelta(weeks=4)

    with sqlite3.connect(db) as c:
        # Baseline: 12 items/wk avg over 4 weeks (48 items spread)
        for i in range(48):
            day = baseline_start + timedelta(days=i // 12 * 7 + (i % 7))
            c.execute(
                "INSERT INTO comm_items (source, account, external_id, "
                "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
                ("gmail", "g", f"b{i}", "in", _ts(day), "x"),
            )
        # Last week: 60 items (5x baseline)
        for i in range(60):
            d = last_week_start + timedelta(days=i % 7)
            c.execute(
                "INSERT INTO comm_items (source, account, external_id, "
                "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
                ("gmail", "g", f"w{i}", "in", _ts(d), "x"),
            )

    detected = run_weekly_detection(db, today=today)
    kinds = [p["kind"] for p in detected]
    assert "comm_volume_spike" in kinds


def test_comm_volume_spike_respects_floor(db: Path) -> None:
    """5x baseline maar te weinig totaal items → geen trigger."""
    today = date(2026, 4, 27)
    last_week_start = today - timedelta(days=7)
    baseline_start = last_week_start - timedelta(weeks=4)
    with sqlite3.connect(db) as c:
        # baseline 4 items, last week 20: ratio ok maar < floor van 50
        for i in range(4):
            c.execute(
                "INSERT INTO comm_items (source, account, external_id, "
                "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
                ("gmail", "g", f"b{i}", "in",
                 _ts(baseline_start + timedelta(days=i)), "x"),
            )
        for i in range(20):
            c.execute(
                "INSERT INTO comm_items (source, account, external_id, "
                "direction, occurred_at, body_full) VALUES (?,?,?,?,?,?)",
                ("gmail", "g", f"w{i}", "in",
                 _ts(last_week_start + timedelta(days=i % 7)), "x"),
            )
    detected = run_weekly_detection(db, today=today)
    kinds = [p["kind"] for p in detected]
    assert "comm_volume_spike" not in kinds


def test_decisions_slowing_triggers(db: Path) -> None:
    today = date(2026, 4, 27)
    last_week_start = today - timedelta(days=7)
    baseline_start = last_week_start - timedelta(weeks=4)
    with sqlite3.connect(db) as c:
        # baseline 16 decisions (avg 4/wk)
        for i in range(16):
            c.execute(
                "INSERT INTO decisions (title, body, decided_at) VALUES (?,?,?)",
                (f"d{i}", "x",
                 _ts(baseline_start + timedelta(days=i))),
            )
        # last week: 1 decision
        c.execute(
            "INSERT INTO decisions (title, body, decided_at) VALUES (?,?,?)",
            ("d-last", "x", _ts(last_week_start + timedelta(days=2))),
        )
    detected = run_weekly_detection(db, today=today)
    assert any(p["kind"] == "decisions_slowing" for p in detected)


def test_decisions_slowing_no_trigger_when_baseline_low(db: Path) -> None:
    today = date(2026, 4, 27)
    detected = run_weekly_detection(db, today=today)
    assert not any(p["kind"] == "decisions_slowing" for p in detected)


def test_stale_outgoing_rising_triggers(db: Path) -> None:
    today = date(2026, 4, 27)
    last_week_end = today
    cutoff = _ts(last_week_end) - 7 * 86400
    with sqlite3.connect(db) as c:
        # 5 stale outgoing requests (created_at < cutoff)
        for i in range(5):
            c.execute(
                "INSERT INTO open_loops (source, kind, who, title, status, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("comm", "outgoing_request", "klant", f"req-{i}", "open",
                 cutoff - 100 - i),
            )
    detected = run_weekly_detection(db, today=today)
    assert any(p["kind"] == "stale_outgoing_rising" for p in detected)


def test_stale_outgoing_rising_no_trigger_when_few(db: Path) -> None:
    today = date(2026, 4, 27)
    last_week_end = today
    cutoff = _ts(last_week_end) - 7 * 86400
    with sqlite3.connect(db) as c:
        for i in range(2):
            c.execute(
                "INSERT INTO open_loops (source, kind, who, title, status, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("comm", "outgoing_request", "k", f"r{i}", "open", cutoff - 100),
            )
    detected = run_weekly_detection(db, today=today)
    assert not any(p["kind"] == "stale_outgoing_rising" for p in detected)


def test_meeting_overload_triggers(db: Path) -> None:
    today = date(2026, 4, 27)
    last_week_start = today - timedelta(days=7)
    baseline_start = last_week_start - timedelta(weeks=4)
    with sqlite3.connect(db) as c:
        # baseline: 8 meetings over 4 weeks → avg 2/wk
        for i in range(8):
            c.execute(
                "INSERT INTO plaud_transcripts (source_path, content_hash, title, "
                "recorded_at, body) VALUES (?,?,?,?,?)",
                (f"/tmp/b{i}.txt", f"shab{i}", f"m{i}",
                 _ts(baseline_start + timedelta(days=i)), "x"),
            )
        # last week: 6 meetings (>= 5 floor + > 1.5x avg of 2)
        for i in range(6):
            c.execute(
                "INSERT INTO plaud_transcripts (source_path, content_hash, title, "
                "recorded_at, body) VALUES (?,?,?,?,?)",
                (f"/tmp/w{i}.txt", f"shaw{i}", f"m{i}",
                 _ts(last_week_start + timedelta(days=i)), "x"),
            )
    detected = run_weekly_detection(db, today=today)
    assert any(p["kind"] == "meeting_overload" for p in detected)


# --- tools ----------------------------------------------------------------

def test_patterns_recent_handler(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_or_replace_pattern(c, week_start=_ts(date(2026, 4, 13)),
                                    kind="comm_volume_spike",
                                    severity="watch", title="A")
    out = patterns_recent_handler(db, {"weeks_back": 4})
    assert len(out) == 1
    assert out[0]["title"] == "A"
    assert "detected" in out[0]


def test_patterns_snooze_handler(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_or_replace_pattern(c, week_start=1, kind="comm_volume_spike",
                                    severity="info", title="A")
        pid = c.execute("SELECT id FROM patterns").fetchone()[0]
    out = patterns_snooze_handler(db, {"pattern_id": pid, "days": 14})
    assert out["ok"] is True
    assert out["snoozed_days"] == 14


def test_run_weekly_detection_empty_db(db: Path) -> None:
    """Geen data → niets te detecteren, geen crash."""
    out = run_weekly_detection(db, today=date(2026, 4, 27))
    assert out == []
