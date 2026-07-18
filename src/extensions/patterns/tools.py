"""Orchestrator-tools voor patterns.

- patterns_recent — laatste detecties (incl. al getoond)
- patterns_snooze — onderdruk een pattern N dagen (default 7)

Dayclose roept zelf `pending_patterns()` aan om 0-2 actieve signals te
surfacen en markeert ze daarna als surfaced. the user heeft géén tool
nodig om het 'aan' te zetten — pattern-detectie is altijd-actief.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.patterns.schema import (
    list_patterns,
    snooze_pattern,
)

TZ = ZoneInfo("Europe/Amsterdam")


PATTERN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "patterns_recent",
        "description": (
            "List recently detected behavior patterns (mail-volume spikes, "
            "decision slowdowns, stale request build-up, meeting overload, "
            "response-time slowdowns). Use bij 'wat zijn de trends', "
            "'pattern-check', 'gedrag deze maand'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks_back": {"type": "integer", "minimum": 1, "maximum": 26,
                                "default": 8},
            },
        },
    },
    {
        "name": "patterns_snooze",
        "description": (
            "Snooze a pattern voor N dagen — gebruik wanneer the user het "
            "signaal heeft gezien en niet weer in dayclose wil zien."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern_id": {"type": "integer"},
                "days": {"type": "integer", "minimum": 1, "maximum": 90,
                          "default": 7},
            },
            "required": ["pattern_id"],
        },
    },
]


def patterns_recent_handler(
    db_path: Path, args: dict[str, Any],
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = list_patterns(conn, weeks_back=int(args.get("weeks_back", 8)))
    return [_format(r) for r in rows]


def patterns_snooze_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    pid = int(args["pattern_id"])
    days = int(args.get("days", 7))
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        ok = snooze_pattern(conn, pid, days=days)
    return {"ok": ok, "pattern_id": pid, "snoozed_days": days}


def _format(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["detected"] = datetime.fromtimestamp(r["detected_at"], TZ).date().isoformat()
    out["week_start_date"] = datetime.fromtimestamp(r["week_start"], TZ).date().isoformat()
    return out


PATTERN_HANDLERS = {
    "patterns_recent": patterns_recent_handler,
    "patterns_snooze": patterns_snooze_handler,
}
