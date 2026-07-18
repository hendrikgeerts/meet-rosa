"""Tests voor extensions.uptime.weekly_report.

Bouwt een in-memory uptime_events tabel, voert recovery-events in
met bekende downtimes, en valideert (a) berekening van stats per
target, (b) trend-vergelijking met vorige week, (c) clipping bij
cross-window incidents, (d) rendering naar iMessage-tekst, en
(e) de previous_week_window helper.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from extensions.uptime.schema import init_uptime_schema
from extensions.uptime.weekly_report import (
    Incident,
    TargetStats,
    _fmt_dt,
    _fmt_duration,
    _fmt_trend,
    compute_weekly_stats,
    compute_window_stats,
    format_imessage_report,
    previous_week_window,
)

TZ = ZoneInfo("Europe/Amsterdam")


def _seed_recovery(
    conn: sqlite3.Connection, target_name: str, recovery_at: datetime,
    downtime_seconds: int, status_code: int | None = 503,
    error: str | None = None,
) -> None:
    """Insert een 'recovery' event + een matching 'down' event ervoor."""
    rec_ts = int(recovery_at.timestamp())
    down_ts = rec_ts - downtime_seconds
    conn.execute(
        "INSERT INTO uptime_events (target_name, kind, at, status_code, error) "
        "VALUES (?, 'down', ?, ?, ?)",
        (target_name, down_ts, status_code, error),
    )
    conn.execute(
        "INSERT INTO uptime_events (target_name, kind, at, status_code, latency_ms, error, detail) "
        "VALUES (?, 'recovery', ?, ?, ?, ?, ?)",
        (target_name, rec_ts, status_code, None, error,
         f"downtime {downtime_seconds}s"),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "uptime.db"
    init_uptime_schema(p)
    return p


# --- compute_window_stats ------------------------------------------------

def test_no_events_means_100pct(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    with sqlite3.connect(db) as conn:
        pct, downtime, incidents = compute_window_stats(
            conn, "DST-Connect CMS", week_start, week_end,
        )
    assert pct == 100.0
    assert downtime == 0
    assert incidents == []


def test_single_incident_basic(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    # 6m 18s incident op dinsdag 02:14
    recovery_at = datetime(2026, 5, 19, 2, 20, 18, tzinfo=TZ)
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS", recovery_at, 378, status_code=503)
        pct, downtime, incidents = compute_window_stats(
            conn, "CMS", week_start, week_end,
        )
    assert downtime == 378
    assert len(incidents) == 1
    assert incidents[0].duration_seconds == 378
    assert incidents[0].reason == "HTTP 503"
    expected_pct = (604800 - 378) / 604800 * 100.0
    assert abs(pct - expected_pct) < 0.001


def test_multiple_incidents_sum(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS", datetime(2026, 5, 19, 2, 20, tzinfo=TZ), 378)
        _seed_recovery(conn, "CMS", datetime(2026, 5, 20, 14, 8, tzinfo=TZ), 1023)
        _seed_recovery(conn, "CMS", datetime(2026, 5, 22, 9, 30, tzinfo=TZ), 262)
        pct, downtime, incidents = compute_window_stats(
            conn, "CMS", week_start, week_end,
        )
    assert len(incidents) == 3
    assert downtime == 378 + 1023 + 262


def test_cross_window_incident_is_clipped(db: Path) -> None:
    """Incident dat 1 uur vóór week begint maar recovert 30s binnen de
    week: alleen het deel ín het window mag meetellen."""
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    # recovery 30s na week_start, raw downtime 3630s (=1u + 30s)
    recovery_at = week_start + timedelta(seconds=30)
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS", recovery_at, 3630)
        pct, downtime, incidents = compute_window_stats(
            conn, "CMS", week_start, week_end,
        )
    assert len(incidents) == 1
    assert incidents[0].duration_seconds == 30      # geclipt
    assert incidents[0].raw_duration_seconds == 3630  # origineel
    assert downtime == 30


def test_incident_outside_window_ignored(db: Path) -> None:
    """Incidents die buiten het window recoverten tellen niet mee."""
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    # recovery 1 dag vóór week_start
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS", week_start - timedelta(days=1), 600)
        # recovery 1 dag na week_end
        _seed_recovery(conn, "CMS", week_end + timedelta(days=1), 600)
        pct, downtime, incidents = compute_window_stats(
            conn, "CMS", week_start, week_end,
        )
    assert incidents == []
    assert downtime == 0


def test_reason_extracted_from_down_event(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    recovery_at = datetime(2026, 5, 19, 2, 20, tzinfo=TZ)
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS", recovery_at, 300,
                        status_code=None, error="connection timed out")
        pct, downtime, incidents = compute_window_stats(
            conn, "CMS", week_start, week_end,
        )
    assert incidents[0].reason == "timeout"


# --- compute_weekly_stats + trend ----------------------------------------

def test_trend_vs_previous_week(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    # Deze week: 5m downtime
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS",
                        datetime(2026, 5, 19, 12, 0, tzinfo=TZ), 300)
        # Vorige week: 10m downtime
        _seed_recovery(conn, "CMS",
                        datetime(2026, 5, 12, 12, 0, tzinfo=TZ), 600)

    stats = compute_weekly_stats(
        db, ["CMS"], week_start, week_end, include_trend=True,
    )
    assert len(stats) == 1
    s = stats[0]
    assert s.downtime_seconds == 300
    assert s.prev_week_uptime_pct is not None
    # Trend should be positive (deze week beter)
    assert s.trend_diff is not None
    assert s.trend_diff > 0


def test_no_trend_when_disabled(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    stats = compute_weekly_stats(
        db, ["CMS"], week_start, week_end, include_trend=False,
    )
    assert stats[0].prev_week_uptime_pct is None
    assert stats[0].trend_diff is None


def test_trend_window_matches_current_window_length(db: Path) -> None:
    """H1 review-finding: voor on-demand windows (30 dagen, kwartaal, jaar)
    moet de prev-window dezelfde lengte hebben als de current window,
    anders zijn de uptime-percentages mathematisch onvergelijkbaar.

    Reproductie-scenario: 30-daagse current window met 1u downtime.
    Prev 30-daagse window had 8u downtime.
    Met de oude bug (prev = 7d hardcoded) zou prev op een 7-daagse
    window worden berekend dat de 8u downtime kan missen → verkeerde
    trend. Met de fix moet prev 30 dagen lang zijn en de 8u meenemen.
    """
    current_end = datetime(2026, 5, 30, 0, 0, tzinfo=TZ)
    current_start = current_end - timedelta(days=30)
    prev_start = current_start - timedelta(days=30)
    # Current window: 1h downtime midden in
    # Prev window: 8h downtime — moet meegenomen worden in vergelijking
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS", current_end - timedelta(days=10),
                        3600)  # 1u in current
        _seed_recovery(conn, "CMS", prev_start + timedelta(days=15),
                        28800)  # 8u in middle of prev window
    stats = compute_weekly_stats(
        db, ["CMS"], current_start, current_end, include_trend=True,
    )
    s = stats[0]
    assert s.downtime_seconds == 3600
    # Current 30d uptime ≈ (2592000 - 3600) / 2592000 * 100 = 99.86%
    expected_current = (30 * 86400 - 3600) / (30 * 86400) * 100
    assert abs(s.uptime_pct - expected_current) < 0.01
    # Prev moet de 8u downtime gevangen hebben — bewijst dat het
    # prev-window 30 dagen lang was, niet 7
    assert s.prev_week_uptime_pct is not None
    expected_prev = (30 * 86400 - 28800) / (30 * 86400) * 100
    assert abs(s.prev_week_uptime_pct - expected_prev) < 0.01
    # Trend: current is beter dan prev → positief
    assert s.trend_diff is not None
    assert s.trend_diff > 0


def test_compute_stats_with_trend_alias() -> None:
    """M2 review-finding: alias `compute_stats_with_trend` voor
    leesbaarheid in nieuwe callers, geen breaking rename."""
    from extensions.uptime.weekly_report import compute_stats_with_trend
    assert compute_stats_with_trend is compute_weekly_stats


def test_per_target_stats_isolated(db: Path) -> None:
    """Incidents van platform A tellen niet bij platform B mee."""
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    with sqlite3.connect(db) as conn:
        _seed_recovery(conn, "CMS",
                        datetime(2026, 5, 19, 12, 0, tzinfo=TZ), 500)
    stats = compute_weekly_stats(
        db, ["CMS", "Content"], week_start, week_end,
    )
    cms = next(s for s in stats if s.name == "CMS")
    content = next(s for s in stats if s.name == "Content")
    assert cms.downtime_seconds == 500
    assert content.downtime_seconds == 0
    assert content.uptime_pct == 100.0


# --- formatting ----------------------------------------------------------

def test_fmt_duration() -> None:
    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(45) == "45s"
    assert _fmt_duration(378) == "6m 18s"
    assert _fmt_duration(3600) == "1u"
    assert _fmt_duration(3900) == "1u 5m"
    assert _fmt_duration(3661) == "1u 1m"  # geen seconden bij uren


def test_fmt_dt_includes_date() -> None:
    """Hendrik feedback 30/5: 'di 09:11' alleen is ambig over een maand
    met 4-5 dinsdagen. Datum erbij voor disambiguation."""
    dt = datetime(2026, 5, 19, 2, 14, tzinfo=TZ)  # dinsdag 19 mei
    assert _fmt_dt(dt) == "di 19 mei 02:14"


def test_fmt_dt_with_year_for_multi_year_windows() -> None:
    """Bij year=True: 'di 19 mei 2026 02:14' — nodig voor windows
    die de jaarwissel overspannen."""
    dt = datetime(2025, 12, 31, 23, 45, tzinfo=TZ)
    assert _fmt_dt(dt, include_year=True) == "wo 31 dec 2025 23:45"


def test_fmt_dt_all_dutch_months() -> None:
    """Alle 12 maanden moeten een NL-afkorting hebben."""
    expected = ["jan", "feb", "mrt", "apr", "mei", "jun",
                "jul", "aug", "sep", "okt", "nov", "dec"]
    for month_idx, abbrev in enumerate(expected, start=1):
        dt = datetime(2026, month_idx, 15, 12, 0, tzinfo=TZ)
        assert abbrev in _fmt_dt(dt)


def test_format_adds_year_when_window_crosses_year_boundary() -> None:
    """Window 15 dec 2025 → 15 jan 2026 → elke datum moet jaartal
    bevatten om te kunnen onderscheiden tussen 2025 en 2026."""
    week_start = datetime(2025, 12, 15, 0, 0, tzinfo=TZ)
    week_end = datetime(2026, 1, 15, 0, 0, tzinfo=TZ)
    inc_2025 = Incident(
        target_name="CMS",
        started_at=datetime(2025, 12, 20, 10, 0, tzinfo=TZ),
        duration_seconds=300, raw_duration_seconds=300, reason="HTTP 503",
    )
    inc_2026 = Incident(
        target_name="CMS",
        started_at=datetime(2026, 1, 5, 14, 0, tzinfo=TZ),
        duration_seconds=600, raw_duration_seconds=600, reason="timeout",
    )
    stats = [
        TargetStats(name="CMS", uptime_pct=99.9,
                     downtime_seconds=900, incident_count=2,
                     longest_incident=inc_2026,
                     incidents=[inc_2025, inc_2026]),
    ]
    text = format_imessage_report(
        stats, week_start=week_start, week_end=week_end,
    )
    assert "2025" in text  # voor inc_2025
    assert "2026" in text  # voor inc_2026


def test_format_omits_year_for_single_year_windows() -> None:
    """Binnen één jaar is jaartal redundant — laat 'm weg voor
    leesbaarheid (kortere regels)."""
    week_start = datetime(2026, 5, 1, 0, 0, tzinfo=TZ)
    week_end = datetime(2026, 5, 31, 0, 0, tzinfo=TZ)
    inc = Incident(
        target_name="CMS",
        started_at=datetime(2026, 5, 15, 10, 0, tzinfo=TZ),
        duration_seconds=300, raw_duration_seconds=300, reason="HTTP 503",
    )
    stats = [
        TargetStats(name="CMS", uptime_pct=99.9,
                     downtime_seconds=300, incident_count=1,
                     longest_incident=inc, incidents=[inc]),
    ]
    text = format_imessage_report(
        stats, week_start=week_start, week_end=week_end,
    )
    # Geen jaartal in datum-stempel
    assert "vr 15 mei 10:00" in text
    assert "2026 10:00" not in text  # geen "mei 2026 10:00"


def test_fmt_trend() -> None:
    assert _fmt_trend(None) == "→"
    assert _fmt_trend(0.0) == "→"
    assert _fmt_trend(0.001) == "→"   # binnen drempel
    assert _fmt_trend(0.02) == "↑0.02%"
    assert _fmt_trend(-0.18) == "↓0.18%"


def test_format_clean_week(db: Path) -> None:
    """100% uptime — vriendelijke compacte output."""
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    stats = [
        TargetStats(name="DST-Connect CMS", uptime_pct=100.0,
                     downtime_seconds=0, incident_count=0,
                     longest_incident=None, incidents=[],
                     prev_week_uptime_pct=100.0),
        TargetStats(name="DS Templates Content", uptime_pct=100.0,
                     downtime_seconds=0, incident_count=0,
                     longest_incident=None, incidents=[],
                     prev_week_uptime_pct=100.0),
    ]
    text = format_imessage_report(
        stats, week_start=week_start, week_end=week_end,
    )
    assert "✅" in text
    assert "geen downtime" in text
    assert "DST-Connect CMS" in text
    assert "100.00%" in text
    assert "Incidents:" not in text  # geen incidents → geen section


def test_format_with_incidents_and_threshold_flag(db: Path) -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    incident_a = Incident(
        target_name="Content",
        started_at=datetime(2026, 5, 19, 2, 14, tzinfo=TZ),
        duration_seconds=378, raw_duration_seconds=378, reason="HTTP 503",
    )
    incident_b = Incident(
        target_name="Content",
        started_at=datetime(2026, 5, 21, 14, 8, tzinfo=TZ),
        duration_seconds=8400, raw_duration_seconds=8400, reason="timeout",
    )
    stats = [
        TargetStats(name="Content", uptime_pct=98.55,
                     downtime_seconds=8778, incident_count=2,
                     longest_incident=incident_b,
                     incidents=[incident_a, incident_b],
                     prev_week_uptime_pct=99.80),
    ]
    text = format_imessage_report(
        stats, week_start=week_start, week_end=week_end,
        threshold_pct=99.0,
    )
    # Per-platform compact
    assert "Content" in text
    assert "98.55%" in text
    # Trend (98.55 - 99.80 = -1.25)
    assert "↓1.25%" in text
    # Longest incident vermeld
    assert "2u 20m" in text or "8400" in text or "longest" in text
    # Incidents-sectie aanwezig
    assert "Incidents:" in text
    # Datum + tijd in het format 'di 19 mei 02:14'
    assert "di 19 mei 02:14" in text  # eerste incident
    assert "do 21 mei 14:08" in text  # tweede incident
    # SLA-flag onder threshold
    assert "⚠️" in text
    assert "onder 99.0% SLA-target" in text


def test_format_caps_incident_list_at_max(db: Path) -> None:
    """M3 review-finding: incident-lijst gecapt bij DEFAULT_MAX_INCIDENT_LIST.
    Boven cap: longest-first selectie + footer met 'X kortere incidents
    niet getoond'."""
    from extensions.uptime.weekly_report import DEFAULT_MAX_INCIDENT_LIST

    week_start = datetime(2026, 5, 1, 0, 0, tzinfo=TZ)
    week_end = datetime(2026, 5, 31, 0, 0, tzinfo=TZ)
    # 50 incidents, oplopend in duur (1*60, 2*60, ..., 50*60 sec)
    n = DEFAULT_MAX_INCIDENT_LIST + 10
    incidents = [
        Incident(
            target_name="CMS",
            started_at=week_start + timedelta(days=i % 28, hours=12),
            duration_seconds=(i + 1) * 60,
            raw_duration_seconds=(i + 1) * 60,
            reason="HTTP 503",
        )
        for i in range(n)
    ]
    longest = max(incidents, key=lambda i: i.duration_seconds)
    total_down = sum(i.duration_seconds for i in incidents)
    stats = [
        TargetStats(name="CMS", uptime_pct=99.5,
                     downtime_seconds=total_down,
                     incident_count=n, longest_incident=longest,
                     incidents=incidents, prev_week_uptime_pct=99.7),
    ]
    text = format_imessage_report(
        stats, week_start=week_start, week_end=week_end,
    )
    # Cap respected: max + 1 (header "Incidents:") + footer
    incident_lines = [line for line in text.splitlines()
                      if line.startswith("  ") and ("CMS" in line)]
    assert len(incident_lines) == DEFAULT_MAX_INCIDENT_LIST
    # Footer aanwezig
    assert "10 kortere incidents niet getoond" in text


def test_format_no_incident_section_when_disabled() -> None:
    week_start = datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    week_end = week_start + timedelta(days=7)
    incident = Incident(
        target_name="CMS",
        started_at=datetime(2026, 5, 19, 2, 14, tzinfo=TZ),
        duration_seconds=378, raw_duration_seconds=378, reason="HTTP 503",
    )
    stats = [
        TargetStats(name="CMS", uptime_pct=99.94,
                     downtime_seconds=378, incident_count=1,
                     longest_incident=incident, incidents=[incident],
                     prev_week_uptime_pct=100.0),
    ]
    text = format_imessage_report(
        stats, week_start=week_start, week_end=week_end,
        include_per_incident_list=False,
    )
    assert "Incidents:" not in text
    # Compact block blijft wel
    assert "99.94%" in text


# --- previous_week_window ------------------------------------------------

def test_previous_week_window_on_monday() -> None:
    """Maandag 09:00 → window van vorige maandag t/m deze maandag 00:00."""
    now = datetime(2026, 5, 25, 9, 0, tzinfo=TZ)  # maandag
    start, end = previous_week_window(now)
    assert start.weekday() == 0  # maandag
    assert start.hour == 0 and start.minute == 0
    assert end.weekday() == 0
    assert (end - start).days == 7


def test_previous_week_window_midweek() -> None:
    """Op andere dagen: meest recente complete ma-zo week."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=TZ)  # donderdag
    start, end = previous_week_window(now)
    # Deze week is ma 25/5. Vorige is ma 18/5. End = deze maandag.
    assert start == datetime(2026, 5, 18, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 5, 25, 0, 0, tzinfo=TZ)
