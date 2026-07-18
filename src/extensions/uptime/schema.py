"""SQLite schema voor uptime-monitoring.

Twee tabellen:
  uptime_checks   — state per target (current up/down, consecutive
                    failures, when last alerted, etc). One row per target.
  uptime_events   — append-only log: elke status-flip, alert verzonden,
                    recovery, silence-toggle. Voor history/dashboard.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS uptime_checks (
    name TEXT PRIMARY KEY,                -- 'DST Templates CMS'
    url TEXT NOT NULL,
    last_check_at INTEGER,                -- unix seconds
    last_status_code INTEGER,             -- HTTP code; NULL bij netwerk-fout
    last_latency_ms INTEGER,
    last_error TEXT,                      -- exception class + message[:200]
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    is_down INTEGER NOT NULL DEFAULT 0,   -- 0/1 boolean
    down_since INTEGER,                   -- unix seconds: eerste fail
    last_alert_at INTEGER,                -- unix seconds: laatste alert verzonden
    silence_until INTEGER,                -- unix seconds: silence tot wanneer
    escalated_at INTEGER                  -- unix seconds: wanneer ntfy-escalatie
                                          -- fire'd voor het huidige incident.
                                          -- NULL na recovery; voorkomt
                                          -- ntfy-storm bij elke re-alert.
);

CREATE TABLE IF NOT EXISTS uptime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_name TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- 'down' | 'up' | 'alert' | 'realert' | 'recovery' | 'silence'
    at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    status_code INTEGER,                  -- snapshot voor history
    latency_ms INTEGER,
    error TEXT,                           -- error msg / detail
    detail TEXT                           -- vrije tekst (downtime duur etc)
);
CREATE INDEX IF NOT EXISTS idx_uptime_events_target_at
    ON uptime_events(target_name, at DESC);
"""


@dataclass
class CheckResult:
    """Resultaat van één HTTP-check; later geconverteerd naar DB-row."""
    name: str
    url: str
    ok: bool
    status_code: int | None       # None = netwerk-/connect-fout
    latency_ms: int
    error: str | None             # exception text bij niet-ok
    checked_at: int               # unix seconds
    retry_after: int | None = None  # R1: Retry-After header (sec, cap 3600)


def init_uptime_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Idempotente migratie voor bestaande DB's: voeg `escalated_at`
        # toe als 'ie nog niet bestaat. ALTER TABLE ADD COLUMN is in
        # SQLite niet IF NOT EXISTS-able, dus we vragen het schema op.
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(uptime_checks)")
        }
        if "escalated_at" not in cols:
            conn.execute(
                "ALTER TABLE uptime_checks ADD COLUMN escalated_at INTEGER"
            )


def mark_escalated(conn: sqlite3.Connection, *, name: str) -> None:
    """M1 — registreer dat Ntfy-escalation gefired heeft voor dit
    incident. Wordt door _send_down_alert aangeroepen na succesvolle
    escalatie. record_check zal dit veld op None zetten zodra het
    target weer up gaat."""
    conn.execute(
        "UPDATE uptime_checks SET escalated_at = strftime('%s','now') "
        "WHERE name = ?", (name,),
    )


def upsert_target(conn: sqlite3.Connection, *, name: str, url: str) -> None:
    """Maak de state-rij aan als 'ie nog niet bestaat. URL wordt
    bijgewerkt zodat config-changes meelopen."""
    conn.execute(
        "INSERT INTO uptime_checks (name, url) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET url=excluded.url",
        (name, url),
    )


def remove_target(conn: sqlite3.Connection, *, name: str) -> None:
    """Verwijder een target dat niet meer in config staat. Events
    blijven staan voor history."""
    conn.execute("DELETE FROM uptime_checks WHERE name=?", (name,))


def get_target_state(
    conn: sqlite3.Connection, *, name: str,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM uptime_checks WHERE name=?", (name,),
    ).fetchone()
    return dict(row) if row else None


def list_targets_state(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All target states — for dashboard + worker scheduling."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM uptime_checks ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def record_check(
    conn: sqlite3.Connection, *, result: CheckResult,
    consecutive_failures: int, is_down: bool,
    down_since: int | None,
) -> None:
    """Update de state-rij met het resultaat van deze check.

    Bij recovery (is_down=False) resetten we ook `escalated_at` zodat
    een toekomstige outage opnieuw kan escaleren — anders zou de
    'al-gepushed-deze-incident' guard de escalatie permanent
    uitschakelen na de eerste outage van dit target. (M1 fix.)"""
    if is_down:
        conn.execute(
            "UPDATE uptime_checks SET "
            "  last_check_at=?, last_status_code=?, last_latency_ms=?, "
            "  last_error=?, consecutive_failures=?, is_down=?, "
            "  down_since=? "
            "WHERE name=?",
            (result.checked_at, result.status_code, result.latency_ms,
             result.error, consecutive_failures, 1,
             down_since, result.name),
        )
    else:
        conn.execute(
            "UPDATE uptime_checks SET "
            "  last_check_at=?, last_status_code=?, last_latency_ms=?, "
            "  last_error=?, consecutive_failures=?, is_down=?, "
            "  down_since=?, escalated_at=NULL "
            "WHERE name=?",
            (result.checked_at, result.status_code, result.latency_ms,
             result.error, consecutive_failures, 0,
             down_since, result.name),
        )


def record_alert_sent(
    conn: sqlite3.Connection, *, name: str, at: int,
) -> None:
    conn.execute(
        "UPDATE uptime_checks SET last_alert_at=? WHERE name=?",
        (at, name),
    )


def set_silence(
    conn: sqlite3.Connection, *, name: str, until: int | None,
) -> None:
    conn.execute(
        "UPDATE uptime_checks SET silence_until=? WHERE name=?",
        (until, name),
    )


def insert_event(
    conn: sqlite3.Connection, *,
    target_name: str, kind: str,
    status_code: int | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
    detail: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO uptime_events "
        "(target_name, kind, status_code, latency_ms, error, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (target_name, kind, status_code, latency_ms, error, detail),
    )
    return cur.lastrowid or 0


def prune_old_events(
    conn: sqlite3.Connection, *,
    days_up: int = 90, days_alert: int = 365,
) -> tuple[int, int]:
    """R5: retention voor uptime_events. 'up' rijen worden veel sneller
    weggeruimd dan alert/recovery/silence (history-waarde voor SLA-
    rapporten + post-mortems). Returns (removed_up, removed_alert)."""
    if days_up <= 0 and days_alert <= 0:
        return (0, 0)
    cutoff_up_row = conn.execute(
        "SELECT strftime('%s','now') - ? * 86400", (days_up,),
    ).fetchone()
    cutoff_alert_row = conn.execute(
        "SELECT strftime('%s','now') - ? * 86400", (days_alert,),
    ).fetchone()
    cutoff_up = int(cutoff_up_row[0])
    cutoff_alert = int(cutoff_alert_row[0])

    removed_up = 0
    if days_up > 0:
        cur = conn.execute(
            "DELETE FROM uptime_events WHERE kind='up' AND at < ?",
            (cutoff_up,),
        )
        removed_up = cur.rowcount or 0

    removed_alert = 0
    if days_alert > 0:
        # 'down' wordt elke check geinsert tijdens downtime — even ruisig
        # als 'up', dus same retention. alert/realert/recovery/silence
        # blijven langer voor incident-traceerbaarheid.
        cur = conn.execute(
            "DELETE FROM uptime_events WHERE kind='down' AND at < ?",
            (cutoff_up,),  # same cutoff als 'up' — beide zijn ruisig
        )
        removed_up += cur.rowcount or 0
        cur = conn.execute(
            "DELETE FROM uptime_events WHERE "
            "kind IN ('alert','realert','recovery','silence','alert_failed') "
            "AND at < ?",
            (cutoff_alert,),
        )
        removed_alert = cur.rowcount or 0

    return (removed_up, removed_alert)


def silence_with_audit(
    conn: sqlite3.Connection, *, name: str, until: int | None,
    reason: str | None = None, actor: str = "operator",
) -> None:
    """Set silence + write audit-event in één DB-tx. Vervangt rauwe SQL
    voor silence-manipulatie zodat A.12.4.3 (admin-logs) wordt
    afgedekt."""
    set_silence(conn, name=name, until=until)
    if until is None:
        detail = f"silence cleared by {actor}"
    else:
        from datetime import datetime
        until_dt = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
        detail = f"silenced by {actor} until {until_dt}"
        if reason:
            detail += f" — {reason}"
    insert_event(conn, target_name=name, kind="silence", detail=detail)


def recent_events(
    conn: sqlite3.Connection, *, target_name: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    if target_name:
        rows = conn.execute(
            "SELECT * FROM uptime_events WHERE target_name=? "
            "ORDER BY at DESC, id DESC LIMIT ?",
            (target_name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM uptime_events ORDER BY at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
