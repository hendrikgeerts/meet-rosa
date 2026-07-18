"""Schema voor live-locatie + alert-historie.

current_location  — append-only log; "huidige" positie = laatste rij.
travel_alerts_sent — dedup-tabel zodat per event/alert-type 1× gestuurd.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS current_location (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    accuracy_m REAL,
    received_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    source TEXT NOT NULL DEFAULT 'ios_shortcut'
);
CREATE INDEX IF NOT EXISTS idx_location_received
    ON current_location(received_at DESC);

CREATE TABLE IF NOT EXISTS travel_alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    alert_kind TEXT NOT NULL,        -- 'plan' | 'leave_now' | 'late' | 'traffic_update'
    sent_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_duration_seconds INTEGER,   -- berekende travel-duration bij verzending (voor re-alert delta)
    UNIQUE(event_id, alert_kind)
);

-- Persistent geocode-cache zodat we niet bij elke daemon-restart opnieuw
-- HERE bevragen voor dezelfde adressen ("kantoor in Rotterdam").
CREATE TABLE IF NOT EXISTS geocode_cache (
    addr_key TEXT PRIMARY KEY,       -- normalised (strip+lower) address
    lat REAL,                        -- NULL = HERE gaf geen match (negative cache)
    lon REAL,
    geocoded_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


def init_travel_alerts_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migrate: add last_duration_seconds to existing tables that don't
        # have it yet (travel-alerts v2 introduces re-alert on traffic
        # change). Idempotent via try/except OperationalError.
        try:
            conn.execute(
                "ALTER TABLE travel_alerts_sent "
                "ADD COLUMN last_duration_seconds INTEGER"
            )
        except sqlite3.OperationalError:
            pass  # column already exists


# --- geocode cache (travel-alerts v2) ----------------------------------

def geocode_cache_get(
    conn: sqlite3.Connection, *, address: str,
    max_age_seconds: int | None = None,
) -> tuple[float, float] | None:
    """Return cached (lat, lon) for the normalised address, or None.
    Returns None for both 'never seen' and 'cached as not-found' — the
    caller can re-attempt HERE if the latter case matters.
    Optional max_age_seconds: rows older than this are treated as miss
    so the caller refreshes them (default = no expiry).
    """
    key = (address or "").strip().lower()
    if not key:
        return None
    row = conn.execute(
        "SELECT lat, lon, geocoded_at FROM geocode_cache WHERE addr_key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    lat, lon, geocoded_at = row
    if max_age_seconds is not None:
        now_ts = conn.execute("SELECT strftime('%s','now')").fetchone()[0]
        if int(now_ts) - int(geocoded_at) > max_age_seconds:
            return None
    if lat is None or lon is None:
        return None  # negative cache hit
    return (float(lat), float(lon))


def geocode_cache_set(
    conn: sqlite3.Connection, *, address: str,
    coords: tuple[float, float] | None,
) -> None:
    """Cache a geocode result. Pass coords=None to store a negative
    cache entry (HERE returned no match) so we don't re-query immediately."""
    key = (address or "").strip().lower()
    if not key:
        return
    lat = coords[0] if coords else None
    lon = coords[1] if coords else None
    conn.execute(
        "INSERT INTO geocode_cache (addr_key, lat, lon) VALUES (?, ?, ?) "
        "ON CONFLICT(addr_key) DO UPDATE SET "
        "  lat=excluded.lat, lon=excluded.lon, "
        "  geocoded_at=strftime('%s','now')",
        (key, lat, lon),
    )


def insert_location(
    conn: sqlite3.Connection, *, lat: float, lon: float,
    accuracy_m: float | None = None, source: str = "ios_shortcut",
    received_at: int | None = None,
    min_interval_seconds: int | None = None,
) -> int:
    """Insert a location row, optionally throttled to at most 1 row per
    `min_interval_seconds`. Returns the new rowid on insert, 0 on skip
    (too-soon-since-last). SECURITY_REVIEW_2 MEDIUM-2: prevents the
    append-only history from growing unboundedly when the iOS Shortcut
    fires sub-hourly.
    """
    if min_interval_seconds is not None and min_interval_seconds > 0:
        now_ts = received_at if received_at is not None else None
        # Use SQLite's strftime when caller didn't pass a timestamp so we
        # stay consistent with the column default.
        if now_ts is None:
            now_row = conn.execute("SELECT strftime('%s','now')").fetchone()
            now_ts = int(now_row[0])
        last = conn.execute(
            "SELECT received_at FROM current_location "
            "ORDER BY received_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if last is not None and (now_ts - int(last[0])) < min_interval_seconds:
            return 0
    if received_at is not None:
        cur = conn.execute(
            "INSERT INTO current_location (lat, lon, accuracy_m, source, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lat, lon, accuracy_m, source, received_at),
        )
    else:
        cur = conn.execute(
            "INSERT INTO current_location (lat, lon, accuracy_m, source) "
            "VALUES (?, ?, ?, ?)",
            (lat, lon, accuracy_m, source),
        )
    return cur.lastrowid or 0


def prune_old_locations(conn: sqlite3.Connection, *, days: int) -> int:
    """Delete current_location rows older than `days`. Returns row-count
    deleted. SECURITY_REVIEW_2 MEDIUM-2: continuous GPS-history is
    GDPR-categorie-PII; ISO 27001 A.18.1.3 expects minimal data storage.
    """
    if days <= 0:
        return 0
    cutoff_row = conn.execute(
        "SELECT strftime('%s','now') - ? * 86400",
        (days,),
    ).fetchone()
    cutoff = int(cutoff_row[0])
    cur = conn.execute(
        "DELETE FROM current_location WHERE received_at < ?",
        (cutoff,),
    )
    return cur.rowcount or 0


def latest_location(
    conn: sqlite3.Connection, *, max_age_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Laatste bekende positie. Returns None als er geen data is, of als
    `max_age_seconds` is gezet en de laatste positie ouder is."""
    conn.row_factory = sqlite3.Row
    # Tie-breaker op id DESC zodat opeenvolgende inserts binnen dezelfde
    # seconde nog deterministisch zijn (sqlite strftime('%s','now') is
    # second-precisie).
    sql = "SELECT * FROM current_location ORDER BY received_at DESC, id DESC LIMIT 1"
    row = conn.execute(sql).fetchone()
    if row is None:
        return None
    if max_age_seconds is not None:
        import time as _time
        if int(_time.time()) - int(row["received_at"]) > max_age_seconds:
            return None
    return dict(row)


def alert_already_sent(
    conn: sqlite3.Connection, *, event_id: str, alert_kind: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM travel_alerts_sent WHERE event_id=? AND alert_kind=? LIMIT 1",
        (event_id, alert_kind),
    ).fetchone()
    return row is not None


def mark_alert_sent(
    conn: sqlite3.Connection, *, event_id: str, alert_kind: str,
    duration_seconds: int | None = None,
) -> bool:
    """Returns True als de insert daadwerkelijk lukte (vs. dup-key).
    `duration_seconds` slaat de bij verzending berekende travel-duration
    op zodat een latere tick `traffic_update`-alerts kan triggeren op
    significante delta."""
    try:
        conn.execute(
            "INSERT INTO travel_alerts_sent "
            "(event_id, alert_kind, last_duration_seconds) VALUES (?, ?, ?)",
            (event_id, alert_kind, duration_seconds),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def last_alert_duration(
    conn: sqlite3.Connection, *, event_id: str, alert_kind: str,
) -> int | None:
    """Return de opgeslagen duration_seconds van de laatste alert van
    dit kind voor dit event, of None als nog niet verzonden of geen
    duration is opgeslagen."""
    row = conn.execute(
        "SELECT last_duration_seconds FROM travel_alerts_sent "
        "WHERE event_id=? AND alert_kind=? LIMIT 1",
        (event_id, alert_kind),
    ).fetchone()
    if row is None:
        return None
    val = row[0]
    return int(val) if val is not None else None
