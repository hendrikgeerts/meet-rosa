"""Tests voor extensions.uptime.tools — on-demand uptime-rapport tool.

Seeden uptime_checks (target-registratie) en uptime_events
(recovery-events met canonical downtime in detail), valideren dat:
- days-window correct geparsed wordt
- start_date/end_date variant werkt
- onverenigbare param-combinaties → error
- mooie label-formulering ("afgelopen 7 weken" ipv "afgelopen 49 dagen")
- target-filter werkt
- retention-warning bij grote windows
- lege targets → nette error
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from core.timezone import bind as bind_tz
from extensions.uptime.schema import init_uptime_schema, upsert_target
from extensions.uptime.tools import (
    UPTIME_HANDLERS,
    UPTIME_TOOL_SCHEMAS,
    uptime_report_handler,
)

TZ = ZoneInfo("Europe/Amsterdam")


def _seed_recovery(
    conn: sqlite3.Connection, target_name: str, recovery_at: datetime,
    downtime_seconds: int, status_code: int | None = 503,
) -> None:
    rec_ts = int(recovery_at.timestamp())
    down_ts = rec_ts - downtime_seconds
    conn.execute(
        "INSERT INTO uptime_events (target_name, kind, at, status_code) "
        "VALUES (?, 'down', ?, ?)",
        (target_name, down_ts, status_code),
    )
    conn.execute(
        "INSERT INTO uptime_events (target_name, kind, at, status_code, detail) "
        "VALUES (?, 'recovery', ?, ?, ?)",
        (target_name, rec_ts, status_code, f"downtime {downtime_seconds}s"),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "uptime.db"
    init_uptime_schema(p)
    # Hendrik's twee SaaS-platforms registreren
    with sqlite3.connect(p) as conn:
        upsert_target(conn, name="DST-Connect CMS",
                       url="https://cms.dst-connect.io/")
        upsert_target(conn, name="DS Templates Content",
                       url="https://content.ds-templates.com/")
    bind_tz(p, default_timezone="Europe/Amsterdam")
    return p


@pytest.fixture
def frozen_now():
    """30 mei 2026, 14:00 lokaal — een vrijdag in het midden van een week."""
    return datetime(2026, 5, 30, 14, 0, tzinfo=TZ)


# --- registratie ---------------------------------------------------------

def test_tool_is_registered() -> None:
    names = [s["name"] for s in UPTIME_TOOL_SCHEMAS]
    assert "uptime_report" in names
    assert "uptime_report" in UPTIME_HANDLERS


# --- days variant --------------------------------------------------------

def test_days_window_basic(db: Path, frozen_now: datetime) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        # Incident 3 dagen geleden binnen het 7-daagse window
        with sqlite3.connect(db) as conn:
            _seed_recovery(conn, "DST-Connect CMS",
                            frozen_now - timedelta(days=3), 600)
        out = uptime_report_handler(db, {"days": 7})
    assert out["ok"] is True
    assert "afgelopen 7 dagen" in out["report"]
    assert "DST-Connect CMS" in out["report"]
    assert out["window"]["days"] == 7
    # Eén target heeft 600s downtime, ander 0s → totals
    cms = next(t for t in out["targets"] if t["name"] == "DST-Connect CMS")
    assert cms["downtime_seconds"] == 600
    assert cms["incident_count"] == 1


def test_days_friendly_labels_for_week_multiples(
    db: Path, frozen_now: datetime,
) -> None:
    """49 dagen → 'afgelopen 7 weken' (vriendelijker dan '49 dagen')."""
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        out = uptime_report_handler(db, {"days": 49})
    assert out["ok"] is True
    assert "afgelopen 7 weken" in out["report"]


def test_days_30_says_30_dagen(db: Path, frozen_now: datetime) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        out = uptime_report_handler(db, {"days": 30})
    assert "afgelopen 30 dagen" in out["report"]


# --- start_date / end_date variant --------------------------------------

def test_start_end_date_window(db: Path, frozen_now: datetime) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        with sqlite3.connect(db) as conn:
            _seed_recovery(conn, "DST-Connect CMS",
                            datetime(2026, 5, 15, 10, 0, tzinfo=TZ), 1200)
        out = uptime_report_handler(db, {
            "start_date": "2026-05-01", "end_date": "2026-05-31",
        })
    assert out["ok"] is True
    cms = next(t for t in out["targets"] if t["name"] == "DST-Connect CMS")
    assert cms["downtime_seconds"] == 1200


def test_invalid_date_format(db: Path) -> None:
    out = uptime_report_handler(db, {
        "start_date": "01/05/2026", "end_date": "31/05/2026",
    })
    assert out["ok"] is False
    assert "YYYY-MM-DD" in out["error"]


def test_end_before_start_rejected(db: Path) -> None:
    out = uptime_report_handler(db, {
        "start_date": "2026-05-31", "end_date": "2026-05-01",
    })
    assert out["ok"] is False
    assert "ná start_date" in out["error"]


# --- mutually exclusive params ------------------------------------------

def test_days_and_start_date_conflict(db: Path) -> None:
    out = uptime_report_handler(db, {
        "days": 7, "start_date": "2026-05-01", "end_date": "2026-05-31",
    })
    assert out["ok"] is False
    assert "niet beide" in out["error"]


def test_neither_days_nor_dates(db: Path) -> None:
    out = uptime_report_handler(db, {})
    assert out["ok"] is False
    assert "days" in out["error"] or "start_date" in out["error"]


# --- target filter -------------------------------------------------------

def test_target_filter(db: Path, frozen_now: datetime) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        with sqlite3.connect(db) as conn:
            _seed_recovery(conn, "DST-Connect CMS",
                            frozen_now - timedelta(days=3), 600)
            _seed_recovery(conn, "DS Templates Content",
                            frozen_now - timedelta(days=4), 800)
        out = uptime_report_handler(db, {
            "days": 7, "target": "DST-Connect CMS",
        })
    assert out["ok"] is True
    assert len(out["targets"]) == 1
    assert out["targets"][0]["name"] == "DST-Connect CMS"


def test_unknown_target_filter(db: Path, frozen_now: datetime) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        out = uptime_report_handler(db, {
            "days": 7, "target": "Imaginair platform",
        })
    assert out["ok"] is False
    assert "niet bekend" in out["error"]


# --- bounds checking -----------------------------------------------------

def test_days_too_low(db: Path) -> None:
    out = uptime_report_handler(db, {"days": 0})
    assert out["ok"] is False


def test_days_too_high(db: Path) -> None:
    out = uptime_report_handler(db, {"days": 9999})
    assert out["ok"] is False
    assert "maximaal" in out["error"]


def test_retention_warning_for_large_window(
    db: Path, frozen_now: datetime,
) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        out = uptime_report_handler(db, {"days": 400})
    assert out["ok"] is True
    assert "⚠️" in out["report"]
    assert "retention" in out["report"]


# --- no targets ----------------------------------------------------------

def test_no_targets_returns_friendly_error(tmp_path: Path) -> None:
    """Schoon database — geen targets → vriendelijke error ipv crash."""
    p = tmp_path / "empty.db"
    init_uptime_schema(p)
    bind_tz(p, default_timezone="Europe/Amsterdam")
    out = uptime_report_handler(p, {"days": 7})
    assert out["ok"] is False
    assert "geen uptime-targets" in out["error"]


# --- report bevat de juiste sectie-headers ------------------------------

def test_threshold_pct_garbage_falls_back_to_default(
    db: Path, frozen_now: datetime,
) -> None:
    """M1 review-finding: threshold_pct moet niet crashen op garbage
    input van Claude (string, dict). Defense-in-depth — JSON-schema
    valideert maar directe callers (tests, scripts) ontsnappen daaraan."""
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        out_string = uptime_report_handler(db, {
            "days": 7, "threshold_pct": "hoog",
        })
        out_dict = uptime_report_handler(db, {
            "days": 7, "threshold_pct": {"weird": True},
        })
        out_none = uptime_report_handler(db, {
            "days": 7, "threshold_pct": None,
        })
    # Geen van deze mag crashen
    assert out_string["ok"] is True
    assert out_dict["ok"] is True
    assert out_none["ok"] is True


def test_report_contains_required_sections(
    db: Path, frozen_now: datetime,
) -> None:
    with patch("extensions.uptime.tools.datetime") as dt_mock:
        dt_mock.now.return_value = frozen_now
        dt_mock.strptime = datetime.strptime
        with sqlite3.connect(db) as conn:
            _seed_recovery(conn, "DST-Connect CMS",
                            frozen_now - timedelta(days=2), 600)
        out = uptime_report_handler(db, {"days": 7, "include_incidents": True})
    report = out["report"]
    assert "Uptime" in report
    assert "DST-Connect CMS" in report
    assert "DS Templates Content" in report
    # Per-incident section omdat include_incidents=true en er een incident is
    assert "Incidents:" in report
