"""Schema voor faillissementen + KvK-watchlist.

Twee tabellen:
  insolvencies       — append-only log van alle gefetched + geparsed
                       publicaties. PK = `link` (RSS-feed URL is uniek
                       per publicatie).
  kvk_watchlist      — the user's lijst van KvK-nummers waar hij direct
                       een alert op wil. PK = `kvk` (string ipv int —
                       leading zeros komen voor).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path


def normalize_kvk(raw: str | int | None) -> str | None:
    """Canonical KvK-representatie voor zowel watchlist als feed-data.
    NL KvK is altijd 8 cijfers (oude registraties hadden minder maar
    moeten met leading zeros aangevuld). Strip non-digits (dots,
    spaces) en zerofill naar 8. Returns None bij geen cijfers of
    >8 cijfers (= geen geldig NL KvK)."""
    if raw is None:
        return None
    s = re.sub(r"\D", "", str(raw))
    if not s or len(s) > 8:
        return None
    return s.zfill(8)


SCHEMA = """
CREATE TABLE IF NOT EXISTS insolvencies (
    link               TEXT PRIMARY KEY,
    naam               TEXT NOT NULL,
    kvk                TEXT,                 -- string ivm leading-zero gevallen
    plaats             TEXT,
    provincie          TEXT,
    rechtbank          TEXT,
    curator            TEXT,
    insolventie_nr     TEXT,                 -- F.05/26/207
    status             TEXT,                 -- 'Faillissement' | 'Surseance' | 'WSNP' | onbekend
    hoofd_activiteit   TEXT,
    raw_description    TEXT,
    pub_date           TEXT,                 -- RFC2822 uit RSS, audit-trail
    pub_at_unix        INTEGER NOT NULL DEFAULT 0,  -- H4: voor correcte ORDER BY
    fetched_at         INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    matched            INTEGER NOT NULL DEFAULT 0,
    matched_layers     TEXT,                 -- JSON
    matched_terms      TEXT,                 -- JSON
    alerted_at         INTEGER,
    ignored_at         INTEGER,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_insolv_kvk         ON insolvencies(kvk);
CREATE INDEX IF NOT EXISTS idx_insolv_fetched     ON insolvencies(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_insolv_pub_at      ON insolvencies(pub_at_unix DESC);
CREATE INDEX IF NOT EXISTS idx_insolv_matched     ON insolvencies(matched, pub_at_unix DESC);
CREATE INDEX IF NOT EXISTS idx_insolv_naam        ON insolvencies(naam);

CREATE TABLE IF NOT EXISTS kvk_watchlist (
    kvk         TEXT PRIMARY KEY,
    naam_hint   TEXT,                        -- "Concurrent X" voor leesbaarheid
    relation    TEXT,                        -- 'klant' | 'leverancier' | 'concurrent' | 'other'
    added_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    added_via   TEXT,                        -- 'imessage' | 'import' | 'manual'
    notes       TEXT
);

-- H2: ignore-policy losgekoppeld van individuele publicatie. Bij
-- `insolvencies_ignore` met een item dat een KvK heeft → KvK hier
-- inschrijven, alle huidige + toekomstige publicaties van dezelfde KvK
-- worden door de matcher als ignored gemarkeerd zonder alert.
CREATE TABLE IF NOT EXISTS ignored_kvks (
    kvk         TEXT PRIMARY KEY,
    ignored_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    reason      TEXT,
    via_link    TEXT                         -- welke alert triggerde de ignore
);
"""


def init_insolvencies_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def insolvency_exists(conn: sqlite3.Connection, link: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM insolvencies WHERE link = ? LIMIT 1", (link,),
    ).fetchone()
    return row is not None


def is_kvk_on_watchlist(conn: sqlite3.Connection, kvk: str | None) -> bool:
    """Checks via genormaliseerde KvK zodat watchlist '00123456' wel
    matched op feed-waarde '123456' (M2-fix)."""
    norm = normalize_kvk(kvk)
    if norm is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM kvk_watchlist WHERE kvk = ? LIMIT 1", (norm,),
    ).fetchone()
    return row is not None


def is_kvk_ignored(conn: sqlite3.Connection, kvk: str | None) -> bool:
    """H2: KvK staat op ignored_kvks → matcher slaat hem over."""
    norm = normalize_kvk(kvk)
    if norm is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM ignored_kvks WHERE kvk = ? LIMIT 1", (norm,),
    ).fetchone()
    return row is not None


def add_to_ignored_kvks(
    conn: sqlite3.Connection, *, kvk: str,
    reason: str | None = None, via_link: str | None = None,
) -> bool:
    """H2: nieuwe KvK op ignore-lijst. Returns True als nieuw, False
    als al aanwezig."""
    norm = normalize_kvk(kvk)
    if norm is None:
        raise ValueError("kvk normalisatie faalde")
    cur = conn.execute(
        "INSERT OR IGNORE INTO ignored_kvks (kvk, reason, via_link) "
        "VALUES (?, ?, ?)",
        (norm, reason, via_link),
    )
    return (cur.rowcount or 0) > 0


def list_watchlist(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT kvk, naam_hint, relation, added_at, added_via, notes "
        "FROM kvk_watchlist ORDER BY added_at DESC"
    ).fetchall())


def add_to_watchlist(
    conn: sqlite3.Connection, *, kvk: str,
    naam_hint: str | None = None,
    relation: str | None = None,
    added_via: str = "imessage",
    notes: str | None = None,
) -> bool:
    """Voeg KvK toe aan watchlist. Returns True als nieuw, False als
    al bestaand. Normaliseert KvK voor opslag (M2)."""
    norm = normalize_kvk(kvk)
    if norm is None:
        raise ValueError("kvk is required en moet geldige cijfers zijn")
    cur = conn.execute(
        "INSERT OR IGNORE INTO kvk_watchlist "
        "(kvk, naam_hint, relation, added_via, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (norm, naam_hint, relation, added_via, notes),
    )
    return (cur.rowcount or 0) > 0


def remove_from_watchlist(conn: sqlite3.Connection, kvk: str) -> bool:
    """Returns True als de KvK in de tabel stond en is verwijderd."""
    norm = normalize_kvk(kvk)
    if norm is None:
        return False
    cur = conn.execute(
        "DELETE FROM kvk_watchlist WHERE kvk = ?", (norm,),
    )
    return (cur.rowcount or 0) > 0


def prune_old_unmatched(
    conn: sqlite3.Connection, *, days: int = 90,
) -> int:
    """Cleanup van unmatched-rijen ouder dan N dagen. Matched-rijen
    blijven voor history (search-tool)."""
    cur = conn.execute(
        "DELETE FROM insolvencies WHERE matched = 0 "
        "AND fetched_at < strftime('%s','now') - ? * 86400",
        (int(days),),
    )
    return int(cur.rowcount or 0)
