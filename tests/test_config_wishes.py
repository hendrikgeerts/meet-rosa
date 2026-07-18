"""Tests voor config_wishes extensie."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from extensions.config_wishes.schema import (
    count_open,
    get_wish,
    init_config_wishes_schema,
    insert_wish,
    list_wishes,
    update_wish_status,
)
from extensions.config_wishes.tools import (
    add_config_wish_handler,
    config_wish_set_status_handler,
    config_wishes_list_handler,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "wishes.db"
    init_config_wishes_schema(p)
    return p


# --- schema --------------------------------------------------------------

def test_insert_and_get(db: Path) -> None:
    with sqlite3.connect(db) as c:
        wid = insert_wish(c, title="Niet pingen tijdens focus",
                           body="Tussen 9-11 niets, ook geen briefing")
        wish = get_wish(c, wid)
    assert wish is not None
    assert wish["title"] == "Niet pingen tijdens focus"
    assert wish["status"] == "open"


def test_list_orders_open_first(db: Path) -> None:
    with sqlite3.connect(db) as c:
        a = insert_wish(c, title="oud", body="x")
        b = insert_wish(c, title="middel", body="x")
        c_ = insert_wish(c, title="nieuwst", body="x")
        update_wish_status(c, a, "done")  # done geschoven naar achteren
    with sqlite3.connect(db) as c:
        rows = list_wishes(c)
    titles = [r["title"] for r in rows]
    assert titles[0] in ("nieuwst", "middel")
    assert titles[-1] == "oud"


def test_update_status_invalid_raises(db: Path) -> None:
    with sqlite3.connect(db) as c:
        wid = insert_wish(c, title="x")
        with pytest.raises(ValueError):
            update_wish_status(c, wid, "weird")


def test_update_status_done_sets_resolved_at(db: Path) -> None:
    with sqlite3.connect(db) as c:
        wid = insert_wish(c, title="x")
        update_wish_status(c, wid, "done")
        wish = get_wish(c, wid)
    assert wish["status"] == "done"
    assert wish["resolved_at"] is not None


def test_count_open(db: Path) -> None:
    with sqlite3.connect(db) as c:
        a = insert_wish(c, title="a")
        b = insert_wish(c, title="b")
        update_wish_status(c, b, "done")
        assert count_open(c) == 1


# --- tools ----------------------------------------------------------------

def test_add_config_wish_tool(db: Path) -> None:
    out = add_config_wish_handler(
        db,
        {"title": "Voortaan in NL antwoorden voor klantmail",
         "body": "behalve internationale klanten"},
        source_handle="hendrik@example.com",
    )
    assert out["ok"] is True
    listed = config_wishes_list_handler(db, {})
    assert any(w["title"].startswith("Voortaan in NL") for w in listed)
    assert listed[0]["source_handle"] == "hendrik@example.com"


def test_add_wish_requires_title(db: Path) -> None:
    out = add_config_wish_handler(db, {"title": ""})
    assert "error" in out


def test_list_filter_by_status(db: Path) -> None:
    with sqlite3.connect(db) as c:
        a = insert_wish(c, title="open-1")
        b = insert_wish(c, title="open-2")
        d = insert_wish(c, title="done-1")
        update_wish_status(c, d, "done")
    open_only = config_wishes_list_handler(db, {"status": "open"})
    assert len(open_only) == 2
    done_only = config_wishes_list_handler(db, {"status": "done"})
    assert len(done_only) == 1


def test_set_status_handler(db: Path) -> None:
    with sqlite3.connect(db) as c:
        wid = insert_wish(c, title="x")
    out = config_wish_set_status_handler(db, {"wish_id": wid, "status": "wip"})
    assert out["ok"] is True
    assert out["wish"]["status"] == "wip"


def test_set_status_invalid(db: Path) -> None:
    out = config_wish_set_status_handler(db, {"wish_id": 1, "status": "ufo"})
    assert "error" in out


def test_set_status_not_found(db: Path) -> None:
    out = config_wish_set_status_handler(db, {"wish_id": 999, "status": "done"})
    assert "error" in out
