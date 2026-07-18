"""Tests voor response-time analytics + overdue-detectie."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.comm_intel.response_time import (
    collect_per_sender_stats,
    find_overdue_threads,
)
from extensions.comm_intel.schema import init_comm_schema
from extensions.comm_intel.tools import COMM_HANDLERS


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "rt.db"
    init_comm_schema(p)
    return p


def _insert(
    db: Path, *, thread_ref: str, from_addr: str,
    direction: str, occurred_at: int, source: str = "gmail",
    intent: str | None = None,
) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO comm_items "
            "(source, account, external_id, direction, from_addr, "
            "occurred_at, body_full, thread_ref, intent) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                source, "hg",
                f"{thread_ref}-{direction}-{occurred_at}",
                direction, from_addr, occurred_at, "", thread_ref, intent,
            ),
        )


# ---- per-sender stats -------------------------------------------------

def test_collect_skips_addrs_below_min_threads(db: Path) -> None:
    now = int(_time.time())
    # Eén thread voor afzender → niet meegenomen (default min_threads=2)
    _insert(db, thread_ref="t1", from_addr="x@y.nl",
            direction="in", occurred_at=now - 7200)
    _insert(db, thread_ref="t1", from_addr="me", direction="out",
            occurred_at=now - 5400)
    stats = collect_per_sender_stats(db)
    assert stats == []


def test_collect_aggregates_two_threads_for_same_sender(db: Path) -> None:
    now = int(_time.time())
    # Twee threads waar Hendrik op anouk@x.nl heeft gereageerd
    # Thread 1: 2u response (7200s)
    _insert(db, thread_ref="t1", from_addr="anouk@x.nl",
            direction="in", occurred_at=now - 86400)
    _insert(db, thread_ref="t1", from_addr="me", direction="out",
            occurred_at=now - 86400 + 7200)
    # Thread 2: 4u response (14400s)
    _insert(db, thread_ref="t2", from_addr="anouk@x.nl",
            direction="in", occurred_at=now - 172800)
    _insert(db, thread_ref="t2", from_addr="me", direction="out",
            occurred_at=now - 172800 + 14400)

    stats = collect_per_sender_stats(db)
    assert len(stats) == 1
    s = stats[0]
    assert s["from_addr"] == "anouk@x.nl"
    assert s["thread_count"] == 2
    # mean = (7200+14400)/2 = 10800s = 3.0h
    assert s["mean_hours"] == 3.0


def test_collect_excludes_min_response_seconds(db: Path) -> None:
    """Auto-bounce of meta-reply binnen 30s wordt niet meegeteld."""
    now = int(_time.time())
    _insert(db, thread_ref="t1", from_addr="x@y.nl",
            direction="in", occurred_at=now - 86400)
    _insert(db, thread_ref="t1", from_addr="me", direction="out",
            occurred_at=now - 86400 + 5)  # 5s
    _insert(db, thread_ref="t2", from_addr="x@y.nl",
            direction="in", occurred_at=now - 172800)
    _insert(db, thread_ref="t2", from_addr="me", direction="out",
            occurred_at=now - 172800 + 3600)  # echte 1h reply
    stats = collect_per_sender_stats(db)
    # 5s wordt geskipt; alleen 1 thread → onder min_threads
    assert stats == []


# ---- find_overdue_threads ---------------------------------------------

def test_overdue_uses_min_age_for_unknown_sender(db: Path) -> None:
    now = int(_time.time())
    # Onbekende afzender, laatste bericht is 'in' van 48u oud
    _insert(db, thread_ref="t1", from_addr="newbie@x.nl",
            direction="in", occurred_at=now - 48 * 3600)
    overdue = find_overdue_threads(db, min_age_hours=24.0)
    assert len(overdue) == 1
    assert overdue[0]["from_addr"] == "newbie@x.nl"


def test_overdue_uses_baseline_for_known_sender(db: Path) -> None:
    """Bekende afzender met snelle baseline (1u median) — een 3u-oud
    onbeantwoord bericht is dan al overdue, zelfs als globaal min_age=24h."""
    now = int(_time.time())
    # Twee gesloten threads → baseline 1h
    _insert(db, thread_ref="hist1", from_addr="quick@x.nl",
            direction="in", occurred_at=now - 86400 * 30)
    _insert(db, thread_ref="hist1", from_addr="me", direction="out",
            occurred_at=now - 86400 * 30 + 3600)
    _insert(db, thread_ref="hist2", from_addr="quick@x.nl",
            direction="in", occurred_at=now - 86400 * 20)
    _insert(db, thread_ref="hist2", from_addr="me", direction="out",
            occurred_at=now - 86400 * 20 + 3600)
    # Open thread: bericht van 3u oud onbeantwoord
    _insert(db, thread_ref="open1", from_addr="quick@x.nl",
            direction="in", occurred_at=now - 3 * 3600)

    # min_age laag zetten zodat baseline (1h*1.5=1.5h) de drempel wordt,
    # niet de globale ondergrens.
    overdue = find_overdue_threads(db, factor=1.5, min_age_hours=1.0)
    assert any(o["thread_ref"] == "open1" for o in overdue)


def test_overdue_skips_newsletters(db: Path) -> None:
    now = int(_time.time())
    _insert(db, thread_ref="nl", from_addr="newsletter@x.nl",
            direction="in", occurred_at=now - 48 * 3600,
            intent="newsletter")
    overdue = find_overdue_threads(db, min_age_hours=24.0)
    assert overdue == []


def test_overdue_skips_already_answered(db: Path) -> None:
    """Thread waar Hendrik AL gereageerd heeft (laatste bericht 'out')
    is niet overdue, ook al was het bericht voor zijn reply oud."""
    now = int(_time.time())
    _insert(db, thread_ref="t1", from_addr="x@y.nl",
            direction="in", occurred_at=now - 100 * 3600)
    _insert(db, thread_ref="t1", from_addr="me", direction="out",
            occurred_at=now - 50 * 3600)
    overdue = find_overdue_threads(db, min_age_hours=24.0)
    assert overdue == []


# ---- tools ------------------------------------------------------------

def test_tool_response_time_stats_returns_dict(db: Path) -> None:
    out = COMM_HANDLERS["response_time_stats"](db, {})
    assert "count" in out
    assert "stats" in out
    assert out["days"] == 90


def test_tool_response_time_overdue_returns_dict(db: Path) -> None:
    out = COMM_HANDLERS["response_time_overdue"](db, {"min_age_hours": 48})
    assert "items" in out
    assert out["min_age_hours"] == 48
