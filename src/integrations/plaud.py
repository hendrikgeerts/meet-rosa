"""Plaud Pro transcripts: watch an inbox folder and ingest new .txt files into SQLite.

Plaud doesn't publish a public API (verified Apr 2026). The least-fragile
integration is: user arranges for Plaud transcripts to land as .txt files in
~/PlaudInbox/ (via the app's Share-to-Files, or a Notion sync with a separate
notion-to-file script, or manual drag). We poll that folder.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


SCHEMA = """
CREATE TABLE IF NOT EXISTS plaud_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    title TEXT,
    body TEXT NOT NULL,
    recorded_at INTEGER,
    ingested_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_plaud_recorded ON plaud_transcripts(recorded_at DESC);
"""


def init_plaud_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def scan_inbox(inbox: Path, db_path: Path) -> int:
    """Ingest any new .txt files in `inbox`. Returns count of newly ingested files."""
    inbox = inbox.expanduser()
    if not inbox.exists():
        inbox.mkdir(parents=True, exist_ok=True)
        return 0

    added = 0
    with sqlite3.connect(db_path) as conn:
        for path in sorted(inbox.glob("*.txt")):
            try:
                body = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                log.warning("could not read %s", path)
                continue
            if not body:
                continue

            h = hashlib.sha256(body.encode()).hexdigest()
            title = _derive_title(path, body)
            recorded_at = _derive_recorded_at(path)

            try:
                conn.execute(
                    "INSERT INTO plaud_transcripts (source_path, content_hash, title, body, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(path), h, title, body, recorded_at),
                )
                conn.commit()
                added += 1
                log.info("plaud: ingested %s (%d chars)", path.name, len(body))
            except sqlite3.IntegrityError:
                pass  # already ingested
    return added


def _derive_title(path: Path, body: str) -> str:
    """Prefer the first line if it looks like a title; else filename."""
    first = body.splitlines()[0].strip() if body else ""
    if first and len(first) < 120:
        return first
    return path.stem


def _derive_recorded_at(path: Path) -> int:
    """Use file mtime as recording timestamp — Plaud shares out in recording order."""
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return int(datetime.now(TZ).timestamp())
