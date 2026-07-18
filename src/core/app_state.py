"""Persistent runtime-state (key-value) — voor niet-config waardes die
over daemon-restart heen moeten overleven maar geen eigen schema verdienen.

Eerste gebruiker: active_timezone (= welke tz Rosa nu gebruikt voor
briefings/dayclose/etc als the user op reis is). Toekomstige use-cases
zijn natuurlijk welkom — een 'do_not_disturb_until' of 'paused_until'
flag past hier ook.

Niet voor: secrets (Keychain), config (settings.yaml), eventlog
(extension-specifieke event-tabellen).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


def init_app_state_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def get(db_path: Path, *, key: str, default: str | None = None) -> str | None:
    """Lees waarde voor key. Returns default als key niet bestaat of
    de waarde NULL is."""
    try:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key=?", (key,),
            ).fetchone()
    except sqlite3.OperationalError:
        return default
    if row is None or row[0] is None:
        return default
    return str(row[0])


def set_value(db_path: Path, *, key: str, value: str | None) -> None:
    """Upsert. value=None om de key te verwijderen (logisch te resetten
    naar default)."""
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        if value is None:
            conn.execute("DELETE FROM app_state WHERE key=?", (key,))
            return
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value=excluded.value, updated_at=strftime('%s','now')",
            (key, value),
        )


def list_all(db_path: Path) -> dict[str, Any]:
    """Voor dashboard / debug: alle keys + waarden + updated_at."""
    try:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT key, value, updated_at FROM app_state ORDER BY key"
            ).fetchall()
        return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]}
                for r in rows}
    except sqlite3.OperationalError:
        return {}
