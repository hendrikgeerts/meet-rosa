"""Tests voor decisions schema + tools."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.decisions.schema import (
    init_decisions_schema,
    insert_decision,
    recent_decisions,
    search_decisions,
    supersede_decision,
)
from extensions.decisions.tools import (
    find_decisions_handler,
    log_decision_handler,
    recent_decisions_handler,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "dec.db"
    init_decisions_schema(p)
    return p


def test_insert_and_recent(db: Path) -> None:
    with sqlite3.connect(db) as c:
        did = insert_decision(c, title="Vendor X gekozen",
                              body="Reden: laagste TCO en bestaande integratie.",
                              attendees=["Hendrik", "Anouk"])
    assert did > 0
    with sqlite3.connect(db) as c:
        rows = recent_decisions(c, days=1)
    assert len(rows) == 1
    assert rows[0]["title"] == "Vendor X gekozen"
    assert rows[0]["attendees"] == ["Hendrik", "Anouk"]


def test_search_decisions_filters_by_query(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_decision(c, title="Vendor X gekozen", body="laagste TCO")
        insert_decision(c, title="Office verhuizing uitgesteld",
                        body="lease loopt door tot Q3")
        rows = search_decisions(c, query="vendor")
    assert len(rows) == 1
    assert "Vendor X" in rows[0]["title"]


def test_search_respects_days_window(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_decision(c, title="Old", body="x",
                        decided_at=int(_time.time()) - 60 * 86400)
        insert_decision(c, title="New", body="x")
        rows = search_decisions(c, query="", days=7)
    titles = [r["title"] for r in rows]
    assert "New" in titles
    assert "Old" not in titles


def test_supersede(db: Path) -> None:
    with sqlite3.connect(db) as c:
        did = insert_decision(c, title="Original", body="oorspronkelijk")
        ok = supersede_decision(c, did, replaced_by="zie decision #99")
    assert ok
    with sqlite3.connect(db) as c:
        rows = search_decisions(c, query="")
    assert all(r["status"] == "active" for r in rows)
    assert len(rows) == 0


def test_log_decision_handler(db: Path) -> None:
    out = log_decision_handler(db, {
        "title": "ISE booth grootte",
        "body": "10x10m gekozen ipv 8x8 — meer footfall verwacht",
        "attendees": ["Hendrik", "Wieke"],
        "source_ref": "plaud:meeting:42",
    })
    assert out["ok"] is True
    with sqlite3.connect(db) as c:
        rows = recent_decisions(c, days=1)
    assert rows[0]["source_ref"] == "plaud:meeting:42"


def test_find_decisions_handler(db: Path) -> None:
    log_decision_handler(db, {"title": "Pricing tier 2", "body": "EU launch in Q3"})
    out = find_decisions_handler(db, {"query": "pricing"})
    assert len(out) == 1


def test_recent_decisions_handler_returns_iso_dates(db: Path) -> None:
    log_decision_handler(db, {"title": "x", "body": "y"})
    out = recent_decisions_handler(db, {"days": 1})
    # ISO-format met tz-offset
    assert "T" in out[0]["decided_at"]
    assert "+" in out[0]["decided_at"] or "Z" in out[0]["decided_at"]
