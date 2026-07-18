"""Tests voor extensions.config_wishes.audit — detecteer
wish-statements die niet via add_config_wish zijn vastgelegd."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from extensions.config_wishes.audit import find_unrecorded_wish_candidates
from extensions.config_wishes.schema import (
    init_config_wishes_schema, insert_wish,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "wishes.db"
    init_config_wishes_schema(p)
    with sqlite3.connect(p) as c:
        c.executescript("""
            CREATE TABLE conversation_turns (
                id INTEGER PRIMARY KEY,
                role TEXT,
                content TEXT,
                created_at INTEGER
            );
        """)
    return p


def _insert_turn(db: Path, role: str, content: str, ts: int) -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO conversation_turns(role, content, created_at) "
            "VALUES (?, ?, ?)",
            (role, content, ts),
        )


def _today_bounds() -> tuple[datetime, datetime]:
    """Return (start, until) waar until ver genoeg vooruit ligt zodat
    alle msg_ts-berekeningen (start + 8-10h) binnen het venster
    vallen, ook als de test vroeg op de dag draait."""
    tz = ZoneInfo("Europe/Amsterdam")
    now = datetime.now(tz).replace(microsecond=0)
    start = now.replace(hour=0, minute=0, second=0)
    return start, start + timedelta(days=1)


# ---- detection ---------------------------------------------------------

def test_unrecorded_kun_je_voortaan_flagged(db: Path) -> None:
    start, until = _today_bounds()
    msg_ts = int((start + timedelta(hours=10)).timestamp())
    _insert_turn(db, "user", "kun je voortaan briefings in het Nederlands schrijven", msg_ts)
    cands = find_unrecorded_wish_candidates(db, since=start, until=until)
    assert len(cands) == 1
    assert "voortaan" in cands[0]["content_excerpt"].lower()


def test_recorded_wish_within_window_not_flagged(db: Path) -> None:
    start, until = _today_bounds()
    msg_ts = int((start + timedelta(hours=10)).timestamp())
    _insert_turn(db, "user", "kun je voortaan briefings in het Nederlands", msg_ts)
    # Insert wish 5 min later — covered.
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO config_wishes(title, created_at) VALUES (?, ?)",
            ("Briefings in NL", msg_ts + 300),
        )
    cands = find_unrecorded_wish_candidates(db, since=start, until=until)
    assert cands == []


def test_recorded_wish_outside_window_still_flagged(db: Path) -> None:
    start, until = _today_bounds()
    msg_ts = int((start + timedelta(hours=10)).timestamp())
    _insert_turn(db, "user", "kun je voortaan briefings in het Nederlands", msg_ts)
    # Insert wish 5h later — outside coverage window → message still candidate.
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO config_wishes(title, created_at) VALUES (?, ?)",
            ("Iets anders", msg_ts + 18000),
        )
    cands = find_unrecorded_wish_candidates(db, since=start, until=until)
    assert len(cands) == 1


def test_assistant_messages_ignored(db: Path) -> None:
    start, until = _today_bounds()
    msg_ts = int((start + timedelta(hours=10)).timestamp())
    _insert_turn(db, "assistant", "kun je voortaan ...", msg_ts)
    cands = find_unrecorded_wish_candidates(db, since=start, until=until)
    assert cands == []


def test_non_wish_message_ignored(db: Path) -> None:
    start, until = _today_bounds()
    msg_ts = int((start + timedelta(hours=10)).timestamp())
    _insert_turn(db, "user", "wat staat er vandaag in m'n agenda?", msg_ts)
    cands = find_unrecorded_wish_candidates(db, since=start, until=until)
    assert cands == []


def test_multiple_patterns_detected(db: Path) -> None:
    start, until = _today_bounds()
    base = int((start + timedelta(hours=8)).timestamp())
    _insert_turn(db, "user", "graag voortaan alle mails in Markdown", base)
    _insert_turn(db, "user", "from now on, prefer Slack over mail", base + 300)
    _insert_turn(db, "user", "onthoud dat onze SLA 99.5% is", base + 600)
    cands = find_unrecorded_wish_candidates(db, since=start, until=until, limit=10)
    assert len(cands) == 3


def test_limit_respected(db: Path) -> None:
    start, until = _today_bounds()
    base = int((start + timedelta(hours=8)).timestamp())
    for i in range(10):
        _insert_turn(db, "user", f"kun je voortaan iets #{i}", base + i * 60)
    cands = find_unrecorded_wish_candidates(db, since=start, until=until, limit=3)
    assert len(cands) == 3


def test_excerpt_clipped(db: Path) -> None:
    start, until = _today_bounds()
    msg_ts = int((start + timedelta(hours=10)).timestamp())
    long_content = "kun je voortaan " + "x" * 500
    _insert_turn(db, "user", long_content, msg_ts)
    cands = find_unrecorded_wish_candidates(db, since=start, until=until)
    assert len(cands[0]["content_excerpt"]) <= 200


def test_empty_db_returns_empty(db: Path) -> None:
    start, until = _today_bounds()
    assert find_unrecorded_wish_candidates(db, since=start, until=until) == []
