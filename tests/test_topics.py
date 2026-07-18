"""Tests voor topic-clustering: subject/summary-token frequency."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.comm_intel.schema import init_comm_schema
from extensions.comm_intel.tools import COMM_HANDLERS
from extensions.comm_intel.topics import (
    collect_active_topics, collect_topic_items,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "topics.db"
    init_comm_schema(p)
    return p


_COUNTER = [0]


def _insert(
    db: Path, *,
    subject: str = "", summary: str = "",
    intent: str | None = None, age_seconds: int = 3600,
    direction: str = "in",
) -> int:
    ts = int(_time.time()) - age_seconds
    _COUNTER[0] += 1
    eid = f"ext-{_COUNTER[0]}"
    with sqlite3.connect(db) as c:
        cur = c.execute(
            "INSERT INTO comm_items "
            "(source, account, external_id, direction, occurred_at, "
            "body_full, subject, summary, intent) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("gmail", "hg", eid,
             direction, ts, "", subject, summary, intent),
        )
    return cur.lastrowid


# ---- collect_active_topics --------------------------------------------

def test_topic_picked_up_when_min_items_reached(db: Path) -> None:
    for i in range(3):
        _insert(db, subject=f"Q3-cijfers vraag {i}")
    topics = collect_active_topics(db, min_items=3)
    topic_names = {t["topic"] for t in topics}
    assert "cijfers" in topic_names


def test_topic_skipped_when_below_min_items(db: Path) -> None:
    _insert(db, subject="zeldzame term X")
    _insert(db, subject="andere mail")
    topics = collect_active_topics(db, min_items=3)
    assert all(t["topic"] != "zeldzame" for t in topics)


def test_topic_excludes_newsletters(db: Path) -> None:
    for i in range(5):
        _insert(db, subject="weekupdate ezineX", intent="newsletter")
    topics = collect_active_topics(db, min_items=3)
    assert topics == []


def test_topic_outside_days_window_skipped(db: Path) -> None:
    for i in range(3):
        _insert(db, subject="oude offerte vraag", age_seconds=60 * 86400)
    topics = collect_active_topics(db, days=14, min_items=3)
    assert topics == []


def test_stopwords_filtered_from_topics(db: Path) -> None:
    """'mail', 'fwd', 'graag' etc. mogen niet als topic verschijnen."""
    for i in range(5):
        _insert(db, subject=f"RE: Fwd: graag even mail {i}")
    topics = collect_active_topics(db, min_items=3)
    topic_names = {t["topic"] for t in topics}
    # Geen stopwords
    for noise in ("fwd", "graag", "even", "mail"):
        assert noise not in topic_names


def test_topics_sorted_by_count_desc(db: Path) -> None:
    # "boekhouder" 5x, "verzekering" 3x
    for i in range(5):
        _insert(db, subject=f"boekhouder vraag {i}")
    for i in range(3):
        _insert(db, subject=f"verzekering issue {i}")
    topics = collect_active_topics(db, min_items=3)
    assert len(topics) >= 2
    assert topics[0]["item_count"] >= topics[1]["item_count"]


def test_short_tokens_skipped(db: Path) -> None:
    """Tokens <4 chars worden niet als topic geclassificeerd."""
    for i in range(5):
        _insert(db, subject=f"abc kort {i}")
    topics = collect_active_topics(db, min_items=3)
    topic_names = {t["topic"] for t in topics}
    assert "abc" not in topic_names


# ---- collect_topic_items ----------------------------------------------

def test_topic_items_returns_matches(db: Path) -> None:
    _insert(db, subject="boekhouder Q3 cijfers")
    _insert(db, subject="andere mail zonder match")
    items = collect_topic_items(db, topic="boekhouder")
    assert len(items) == 1


def test_topic_items_case_insensitive(db: Path) -> None:
    _insert(db, subject="BOEKHOUDER vraag")
    items = collect_topic_items(db, topic="boekhouder")
    assert len(items) == 1


def test_topic_items_outside_window_skipped(db: Path) -> None:
    _insert(db, subject="boekhouder oude vraag", age_seconds=60 * 86400)
    items = collect_topic_items(db, topic="boekhouder", days=30)
    assert items == []


# ---- tools ------------------------------------------------------------

def test_tool_topics_active_returns_dict(db: Path) -> None:
    for i in range(3):
        _insert(db, subject="Q3-cijfers vraag")
    out = COMM_HANDLERS["comm_topics_active"](db, {"min_items": 3})
    assert "topics" in out
    assert out["count"] >= 1


def test_tool_topic_items_short_topic_errors(db: Path) -> None:
    out = COMM_HANDLERS["comm_topic_items"](db, {"topic": "xy"})
    assert "error" in out


def test_tool_topic_items_valid_returns_list(db: Path) -> None:
    _insert(db, subject="boekhouder")
    out = COMM_HANDLERS["comm_topic_items"](db, {"topic": "boekhouder"})
    assert "items" in out
    assert out["count"] == 1
