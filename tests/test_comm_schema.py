"""Tests voor extensions.comm_intel.schema — insert/dedupe/state-roundtrip."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from extensions.comm_intel.schema import (
    CommItem, init_comm_schema, insert_item, item_exists, load_state,
    prune_old_comm_items, upsert_state,
)


def _conn(p: Path) -> sqlite3.Connection:
    init_comm_schema(p)
    c = sqlite3.connect(p, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _item(**over) -> CommItem:
    base = dict(
        source="gmail", account="gmail", external_id="m1",
        direction="in", occurred_at=1_700_000_000,
        body_full="Hi Hendrik, bel je morgen?", from_addr="piet@klant.nl",
        to_addrs=["hendrik@x.nl"], subject="Even bellen?",
        thread_ref="t-1",
    )
    base.update(over)
    return CommItem(**base)


# --- insert_item -----------------------------------------------------------

def test_insert_first_time_returns_id(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    rid = insert_item(conn, _item(), summary="Wil bellen", intent="task", sentiment="neutral")
    assert isinstance(rid, int) and rid > 0


def test_insert_duplicate_returns_none(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_item(conn, _item())
    assert insert_item(conn, _item()) is None  # same (source, account, external_id)


def test_insert_dedupe_per_account(tmp_path: Path) -> None:
    """Zelfde external_id maar ander account = aparte rij."""
    conn = _conn(tmp_path / "db.sqlite")
    insert_item(conn, _item(account="gmail"))
    rid2 = insert_item(conn, _item(account="hendrikdpm", source="imap"))
    assert rid2 is not None


def test_item_exists(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_item(conn, _item())
    assert item_exists(conn, source="gmail", account="gmail", external_id="m1")
    assert not item_exists(conn, source="gmail", account="gmail", external_id="m999")


# --- ingest_state ----------------------------------------------------------

def test_state_missing_returns_none(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    assert load_state(conn, source="gmail", account="gmail") is None


def test_upsert_then_load_returns_values(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    upsert_state(conn, source="gmail", account="gmail",
                 last_external_id="m9", last_occurred_at=1234)
    s = load_state(conn, source="gmail", account="gmail")
    assert s == {"last_external_id": "m9", "last_occurred_at": 1234,
                 "last_polled_at": pytest.approx(s["last_polled_at"], abs=2)}


def test_upsert_preserves_old_values_when_new_is_none(tmp_path: Path) -> None:
    """Tweede upsert met alleen polled-bump moet last_external_id niet wissen."""
    conn = _conn(tmp_path / "db.sqlite")
    upsert_state(conn, source="gmail", account="gmail",
                 last_external_id="m9", last_occurred_at=1234)
    upsert_state(conn, source="gmail", account="gmail")  # no new high-water-mark
    s = load_state(conn, source="gmail", account="gmail")
    assert s["last_external_id"] == "m9"
    assert s["last_occurred_at"] == 1234


def test_state_per_folder(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    upsert_state(conn, source="imap", account="dpm", folder="INBOX",
                 last_external_id="100")
    upsert_state(conn, source="imap", account="dpm", folder="Sent",
                 last_external_id="55")
    inbox = load_state(conn, source="imap", account="dpm", folder="INBOX")
    sent = load_state(conn, source="imap", account="dpm", folder="Sent")
    assert inbox["last_external_id"] == "100"
    assert sent["last_external_id"] == "55"


# --- DB-TTL: prune_old_comm_items ----------------------------------------

def test_prune_old_comm_items_drops_past_retention(tmp_path: Path) -> None:
    import time as _t
    conn = _conn(tmp_path / "db.sqlite")
    now = int(_t.time())
    insert_item(conn, _item(external_id="old", occurred_at=now - 400 * 86400),
                summary="old", intent="fyi", sentiment="neutral")
    insert_item(conn, _item(external_id="mid", occurred_at=now - 200 * 86400),
                summary="mid", intent="fyi", sentiment="neutral")
    insert_item(conn, _item(external_id="recent", occurred_at=now - 5 * 86400),
                summary="recent", intent="fyi", sentiment="neutral")
    removed = prune_old_comm_items(conn, days=365)
    assert removed == 1
    remaining = conn.execute(
        "SELECT external_id FROM comm_items ORDER BY occurred_at"
    ).fetchall()
    assert {r[0] for r in remaining} == {"mid", "recent"}


def test_prune_old_comm_items_zero_days_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_item(conn, _item(external_id="any"),
                summary="any", intent="fyi", sentiment="neutral")
    removed = prune_old_comm_items(conn, days=0)
    cnt = conn.execute("SELECT COUNT(*) FROM comm_items").fetchone()[0]
    assert removed == 0
    assert cnt == 1


def test_prune_old_comm_items_handles_missing_dependent_tables(tmp_path: Path) -> None:
    """vec0 (comm_embeddings) + pending_proposals are absent on a bare
    init_comm_schema DB. Prune must still succeed without raising."""
    import time as _t
    conn = _conn(tmp_path / "db.sqlite")
    insert_item(
        conn, _item(external_id="old", occurred_at=int(_t.time()) - 1000 * 86400),
        summary="old", intent="fyi", sentiment="neutral",
    )
    removed = prune_old_comm_items(conn, days=365)
    assert removed == 1
