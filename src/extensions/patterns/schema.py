"""patterns tabel — gedetecteerde gedrags-trends.

Eén rij per signaal per analyse-week. De wekelijkse detector schrijft
nieuwe rijen, dayclose surfaces N nog-niet-getoonde patterns per dag
en markeert `surfaced_at`. the user kan via tool een pattern snoozen.
"""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path
from typing import Any

VALID_KINDS = (
    "meeting_overload",
    "comm_volume_spike",
    "decisions_slowing",
    "stale_outgoing_rising",
    "focus_blocks_shrinking",
)
VALID_SEVERITIES = ("info", "watch", "alert")


SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    week_start INTEGER NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK(severity IN ('info','watch','alert')),
    title TEXT NOT NULL,
    body TEXT,
    metric_value REAL,
    baseline_value REAL,
    surfaced_at INTEGER,
    snoozed_until INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_unique
    ON patterns(week_start, kind);
CREATE INDEX IF NOT EXISTS idx_patterns_pending
    ON patterns(surfaced_at, snoozed_until);
"""


def init_patterns_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def insert_or_replace_pattern(
    conn: sqlite3.Connection, *,
    week_start: int, kind: str, severity: str,
    title: str, body: str = "",
    metric_value: float | None = None,
    baseline_value: float | None = None,
) -> int:
    """Idempotent — same (week_start, kind) overschrijft eerdere detectie
    in dezelfde week. Reset surfaced_at zodat een nieuwe inzicht alsnog
    in dayclose verschijnt."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown pattern kind: {kind!r}")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"unknown severity: {severity!r}")
    cur = conn.execute(
        "INSERT INTO patterns (week_start, kind, severity, title, body, "
        "metric_value, baseline_value) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(week_start, kind) DO UPDATE SET "
        "severity=excluded.severity, title=excluded.title, "
        "body=excluded.body, metric_value=excluded.metric_value, "
        "baseline_value=excluded.baseline_value, "
        "surfaced_at=NULL, snoozed_until=NULL",
        (week_start, kind, severity, title, body, metric_value, baseline_value),
    )
    return cur.lastrowid or 0


def list_patterns(
    conn: sqlite3.Connection, *,
    weeks_back: int = 8, limit: int = 50,
) -> list[dict[str, Any]]:
    cutoff = int(_time.time()) - weeks_back * 7 * 86400
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM patterns WHERE detected_at >= ? "
        "ORDER BY detected_at DESC, severity DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def pending_patterns(
    conn: sqlite3.Connection, *, limit: int = 3,
) -> list[dict[str, Any]]:
    """Niet eerder getoond én niet gesnoozed — sorteer op severity desc."""
    now = int(_time.time())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM patterns "
        "WHERE surfaced_at IS NULL "
        "AND (snoozed_until IS NULL OR snoozed_until < ?) "
        "ORDER BY CASE severity WHEN 'alert' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END, "
        "detected_at DESC LIMIT ?",
        (now, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_surfaced(conn: sqlite3.Connection, pattern_ids: list[int]) -> int:
    if not pattern_ids:
        return 0
    placeholders = ",".join("?" * len(pattern_ids))
    cur = conn.execute(
        f"UPDATE patterns SET surfaced_at = ? WHERE id IN ({placeholders})",
        (int(_time.time()), *pattern_ids),
    )
    return cur.rowcount


def snooze_pattern(
    conn: sqlite3.Connection, pattern_id: int, *, days: int = 7,
) -> bool:
    cur = conn.execute(
        "UPDATE patterns SET snoozed_until = ? WHERE id = ?",
        (int(_time.time()) + days * 86400, pattern_id),
    )
    return cur.rowcount > 0


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "detected_at": r["detected_at"],
        "week_start": r["week_start"],
        "kind": r["kind"],
        "severity": r["severity"],
        "title": r["title"],
        "body": r["body"],
        "metric_value": r["metric_value"],
        "baseline_value": r["baseline_value"],
        "surfaced_at": r["surfaced_at"],
        "snoozed_until": r["snoozed_until"],
    }
