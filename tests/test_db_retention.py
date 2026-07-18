"""Tests voor core.db.prune_conversation_history — Audit DB-2 (28/6)."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from core.db import init_db, prune_conversation_history


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "memory.db"
    init_db(p)
    return p


def test_prune_removes_old_conversation_turns(db: Path) -> None:
    now = int(_time.time())
    old = now - 200 * 86400   # 200 dagen oud
    fresh = now - 5 * 86400   # 5 dagen
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO conversation_turns (handle, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("h1", "user", "ancient", old),
        )
        c.execute(
            "INSERT INTO conversation_turns (handle, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("h1", "user", "fresh", fresh),
        )
        turns_removed, _ = prune_conversation_history(
            c, turns_days=180, processed_days=180,
        )
        kept = [r[0] for r in c.execute(
            "SELECT content FROM conversation_turns",
        ).fetchall()]
    assert turns_removed == 1
    assert kept == ["fresh"]


def test_prune_removes_old_processed_messages(db: Path) -> None:
    now = int(_time.time())
    old = now - 365 * 86400
    fresh = now - 5 * 86400
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO processed_messages "
            "(guid, rowid, handle, text, received_at) VALUES (?, ?, ?, ?, ?)",
            ("g-old", 1, "h", "old", old),
        )
        c.execute(
            "INSERT INTO processed_messages "
            "(guid, rowid, handle, text, received_at) VALUES (?, ?, ?, ?, ?)",
            ("g-fresh", 2, "h", "fresh", fresh),
        )
        _, processed_removed = prune_conversation_history(
            c, turns_days=180, processed_days=180,
        )
        kept_guids = [r[0] for r in c.execute(
            "SELECT guid FROM processed_messages",
        ).fetchall()]
    assert processed_removed == 1
    assert kept_guids == ["g-fresh"]


def test_prune_zero_days_no_op(db: Path) -> None:
    """retention=0 → uitschakelen, niet alles verwijderen."""
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO conversation_turns (handle, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("h", "user", "x", now - 999 * 86400),
        )
        turns_removed, processed_removed = prune_conversation_history(
            c, turns_days=0, processed_days=0,
        )
    assert turns_removed == 0
    assert processed_removed == 0


def test_prune_empty_db_returns_zero(db: Path) -> None:
    with sqlite3.connect(db) as c:
        assert prune_conversation_history(
            c, turns_days=180, processed_days=180,
        ) == (0, 0)
