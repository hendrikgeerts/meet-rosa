"""market_items tabel: één rij per gefetcht nieuws-item.

URL is unique key voor dedup over feeds (verschillende sources rapporteren
vaak hetzelfde nieuws → houd één rij). Status-machine:
  new → scored → digested → archived.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DOMAINS = ("digital_signage", "ai_models", "press_mentions")

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    author TEXT,
    published_at INTEGER,
    fetched_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    snippet TEXT,
    summary TEXT,
    relevance_score INTEGER,
    is_opportunity INTEGER NOT NULL DEFAULT 0,
    opportunity_reason TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new','scored','digested','archived'))
);
CREATE INDEX IF NOT EXISTS idx_market_domain_score
    ON market_items(domain, relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_market_published
    ON market_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_status
    ON market_items(status);
"""


def init_market_intel_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migrate: oudere DBs hadden CHECK(domain IN ('digital_signage','ai_models')).
        # Detecteer via sqlite_master en rebuild als nodig.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_items'"
        ).fetchone()
        if row and "CHECK(domain IN" in (row[0] or ""):
            _rebuild_table_without_domain_check(conn)


def _rebuild_table_without_domain_check(conn: sqlite3.Connection) -> None:
    """One-shot migratie: oude CHECK(domain IN ('digital_signage','ai_models'))
    weghalen zodat 'press_mentions' kan worden ingevoegd."""
    conn.executescript("""
        BEGIN;
        CREATE TABLE market_items_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            author TEXT,
            published_at INTEGER,
            fetched_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            snippet TEXT,
            summary TEXT,
            relevance_score INTEGER,
            is_opportunity INTEGER NOT NULL DEFAULT 0,
            opportunity_reason TEXT,
            status TEXT NOT NULL DEFAULT 'new'
                CHECK(status IN ('new','scored','digested','archived'))
        );
        INSERT INTO market_items_new SELECT * FROM market_items;
        DROP TABLE market_items;
        ALTER TABLE market_items_new RENAME TO market_items;
        CREATE INDEX IF NOT EXISTS idx_market_domain_score
            ON market_items(domain, relevance_score DESC);
        CREATE INDEX IF NOT EXISTS idx_market_published
            ON market_items(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_status
            ON market_items(status);
        COMMIT;
    """)


@dataclass
class MarketItem:
    domain: str
    source: str
    title: str
    url: str
    author: str | None = None
    published_at: int | None = None
    snippet: str | None = None


def insert_item(conn: sqlite3.Connection, item: MarketItem) -> int | None:
    """Insert nieuw item; return rowid of None bij URL-dedup."""
    try:
        cur = conn.execute(
            """
            INSERT INTO market_items (domain, source, title, url, author,
                                       published_at, snippet)
            VALUES (?,?,?,?,?,?,?)
            """,
            (item.domain, item.source, item.title, item.url, item.author,
             item.published_at, item.snippet),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def find_unscored(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM market_items WHERE status='new' "
        "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_score(
    conn: sqlite3.Connection, item_id: int, *,
    summary: str, relevance: int, is_opportunity: bool,
    opportunity_reason: str | None,
) -> None:
    conn.execute(
        "UPDATE market_items SET summary=?, relevance_score=?, "
        "is_opportunity=?, opportunity_reason=?, status='scored' "
        "WHERE id=?",
        (summary, relevance, 1 if is_opportunity else 0,
         opportunity_reason, item_id),
    )


def top_for_digest(
    conn: sqlite3.Connection, *, days: int = 7, limit: int = 15,
) -> list[dict[str, Any]]:
    """Top items uit de afgelopen N dagen, gesorteerd op relevance,
    opportunities boven gelijke score."""
    import time as _time
    cutoff = int(_time.time()) - days * 86400
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM market_items WHERE status='scored' "
        "AND COALESCE(published_at, fetched_at) >= ? "
        "ORDER BY is_opportunity DESC, relevance_score DESC, "
        "COALESCE(published_at, fetched_at) DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_digested(conn: sqlite3.Connection, item_ids: list[int]) -> None:
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    conn.execute(
        f"UPDATE market_items SET status='digested' WHERE id IN ({placeholders})",
        item_ids,
    )


def search(
    conn: sqlite3.Connection, *, query: str, domain: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Simpele LIKE-zoek over title + summary."""
    sql = (
        "SELECT * FROM market_items WHERE status != 'archived' "
        "AND (title LIKE ? OR summary LIKE ?)"
    )
    params: list[Any] = [f"%{query}%", f"%{query}%"]
    if domain:
        sql += " AND domain = ?"
        params.append(domain)
    sql += " ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?"
    params.append(limit)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def recent(
    conn: sqlite3.Connection, *, domain: str | None = None,
    days: int = 7, limit: int = 30,
) -> list[dict[str, Any]]:
    import time as _time
    cutoff = int(_time.time()) - days * 86400
    sql = (
        "SELECT * FROM market_items WHERE status != 'archived' "
        "AND COALESCE(published_at, fetched_at) >= ?"
    )
    params: list[Any] = [cutoff]
    if domain:
        sql += " AND domain = ?"
        params.append(domain)
    sql += (
        " ORDER BY is_opportunity DESC, relevance_score DESC, "
        "COALESCE(published_at, fetched_at) DESC LIMIT ?"
    )
    params.append(limit)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
