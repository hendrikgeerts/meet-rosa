"""SQLite schema voor TenderNed-publicaties.

Eén tabel: `tenders`. Bewaart elke binnenkomende publicatie (matched én
unmatched) met enough metadata voor dedupe op `kenmerk` (de aanbesteding-
keten-ID) en voor latere `tenders_search` queries.

`kenmerk` is uniek per aanbestedings-keten — rectificaties + gunningen
delen het kenmerk van de oorspronkelijke aankondiging. We alerteren
alleen op het eerste publicatie per kenmerk, latere updates worden
opgeslagen maar pingen niet (tenzij sluitingsdatum wijzigt).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    publicatie_id      INTEGER PRIMARY KEY,
    kenmerk            INTEGER NOT NULL,
    aanbesteding_naam  TEXT NOT NULL,
    opdrachtgever_naam TEXT,
    opdracht_beschrijving TEXT,
    publicatie_datum   TEXT,              -- ISO
    sluitings_datum    TEXT,              -- ISO of NULL
    type_publicatie    TEXT,              -- 'Aankondiging' / 'Rectificatie' / 'Gunning' omschrijving
    aankondiging_code  TEXT,              -- 'OPE' / 'REC' / 'GUN' etc.
    procedure          TEXT,              -- 'Openbaar' / 'Niet-openbaar' etc.
    type_opdracht      TEXT,              -- 'D' (Diensten) / 'L' (Leveringen) / 'W' (Werken)
    cpv_codes          TEXT,              -- JSON: [{code, omschrijving, isHoofdOpdracht}]
    nuts_codes         TEXT,              -- JSON: [{code, omschrijving}]
    trefwoord1         TEXT,
    trefwoord2         TEXT,
    link               TEXT NOT NULL,     -- full URL naar TenderNed-overzicht
    matched            INTEGER NOT NULL DEFAULT 0,   -- 0/1
    matched_layers     TEXT,              -- JSON list: ['trefwoord','cpv_code','cpv_desc','keyword']
    matched_terms      TEXT,              -- JSON list: de daadwerkelijk getriggerde termen
    fetched_at         INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    alerted_at         INTEGER,           -- NULL = nog niet gealert
    ignored_at         INTEGER,           -- the user: 'niet relevant'
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_tenders_kenmerk      ON tenders(kenmerk);
CREATE INDEX IF NOT EXISTS idx_tenders_fetched      ON tenders(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_matched      ON tenders(matched, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_sluitings    ON tenders(sluitings_datum);
"""


def init_tenders_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def get_tender(conn: sqlite3.Connection, publicatie_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM tenders WHERE publicatie_id = ?", (publicatie_id,),
    ).fetchone()


def tender_exists(conn: sqlite3.Connection, publicatie_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tenders WHERE publicatie_id = ? LIMIT 1",
        (publicatie_id,),
    ).fetchone()
    return row is not None


def kenmerk_already_alerted(conn: sqlite3.Connection, kenmerk: int) -> bool:
    """Heeft er al een publicatie van deze aanbesteding-keten een alert
    gehad? Zo ja: latere rectificaties NIET opnieuw alerten (tenzij
    caller de sluitingsdatum-policy overruled)."""
    row = conn.execute(
        "SELECT 1 FROM tenders WHERE kenmerk = ? AND alerted_at IS NOT NULL LIMIT 1",
        (kenmerk,),
    ).fetchone()
    return row is not None


def prune_old_unmatched(
    conn: sqlite3.Connection, *, days: int = 90,
) -> int:
    """Cleanup: niet-gematchte publicaties ouder dan N dagen weghalen.
    Matched-rijen blijven (voor `tenders_search` historie). Returns
    aantal verwijderde rijen."""
    cur = conn.execute(
        "DELETE FROM tenders WHERE matched = 0 "
        "AND fetched_at < strftime('%s','now') - ? * 86400",
        (int(days),),
    )
    return int(cur.rowcount or 0)
