"""Tests voor extensions.person_brief.lookup — pure aggregator."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from extensions.comm_intel.schema import CommItem, init_comm_schema, insert_item
from extensions.open_loops.schema import (
    OpenLoop, init_open_loops_schema, insert_loop,
)
from extensions.person_brief.lookup import (
    _aliases_for_search, build_person_brief, find_vip_match,
    load_vip_contacts, validate_query,
)
from extensions.plaud_intel.schema import init_plaud_meetings_schema


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "pb.db"
    init_comm_schema(p)
    init_open_loops_schema(p)
    init_plaud_meetings_schema(p)
    # Plaud transcripts is referenced by FK from plaud_meetings; satisfy schema.
    with sqlite3.connect(p) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS plaud_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                title TEXT, body TEXT NOT NULL, recorded_at INTEGER,
                ingested_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            );
        """)
    return p


@pytest.fixture
def vip_path(tmp_path: Path) -> Path:
    p = tmp_path / "vip.yaml"
    p.write_text(
        "people:\n"
        "  - name: Piet Janssens\n"
        "    aliases: [\"P. Janssens\", \"Piet\"]\n"
        "    emails: [\"piet@klant.nl\"]\n"
        "    tier: A\n"
        "    relationship: long_term_client\n"
    )
    return p


# --- find_vip_match ------------------------------------------------------

def test_find_vip_by_name(vip_path: Path) -> None:
    vips = load_vip_contacts(vip_path)
    m = find_vip_match("Piet", vips)
    assert m is not None
    assert m["name"] == "Piet Janssens"


def test_find_vip_by_email(vip_path: Path) -> None:
    vips = load_vip_contacts(vip_path)
    m = find_vip_match("piet@klant.nl", vips)
    assert m is not None
    assert m["tier"] == "A"


def test_find_vip_by_alias(vip_path: Path) -> None:
    vips = load_vip_contacts(vip_path)
    assert find_vip_match("P. Janssens", vips) is not None


def test_find_vip_unknown_returns_none(vip_path: Path) -> None:
    vips = load_vip_contacts(vip_path)
    assert find_vip_match("Onbekend Iemand", vips) is None


def test_aliases_for_search_includes_vip_data(vip_path: Path) -> None:
    vips = load_vip_contacts(vip_path)
    vip = find_vip_match("Piet", vips)
    terms = _aliases_for_search(vip, "Piet")
    assert "Piet Janssens" in terms
    assert "P. Janssens" in terms
    assert "piet@klant.nl" in terms


def test_aliases_for_search_falls_back_to_query_only(vip_path: Path) -> None:
    terms = _aliases_for_search(None, "Onbekend")
    assert terms == ["Onbekend"]


# --- build_person_brief end-to-end --------------------------------------

def test_brief_finds_recent_interactions(db: Path, vip_path: Path) -> None:
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        insert_item(c, CommItem(
            source="gmail", account="gmail", external_id="m1",
            folder=None, direction="in", from_addr="piet@klant.nl",
            to_addrs=["user@example.com"], subject="Re: offerte",
            occurred_at=now, body_full="Wat is de status?",
            thread_ref="t1",
        ), summary="Vraag over offerte status", intent="question",
           sentiment="neutral")

    cal = MagicMock()
    cal.search_events.return_value = []
    brief = build_person_brief(
        query="Piet", db_path=db, calendar=cal, vip_path=vip_path,
    )
    assert brief["vip"]["name"] == "Piet Janssens"
    assert len(brief["recent_interactions"]) == 1
    assert brief["recent_interactions"][0]["subject"] == "Re: offerte"


def test_brief_includes_open_loops_for_person(db: Path, vip_path: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_loop(c, OpenLoop(
            source="comm", source_ref="gmail:gmail:m1",
            kind="incoming_question", who="piet@klant.nl",
            title="Reageert op offerte?",
        ))
    cal = MagicMock()
    cal.search_events.return_value = []
    brief = build_person_brief(query="Piet", db_path=db,
                                calendar=cal, vip_path=vip_path)
    assert len(brief["open_loops"]) == 1
    assert brief["open_loops"][0]["title"] == "Reageert op offerte?"


def test_brief_handles_unknown_person_gracefully(db: Path, vip_path: Path) -> None:
    cal = MagicMock(); cal.search_events.return_value = []
    brief = build_person_brief(query="OnbekendIemand123",
                                db_path=db, calendar=cal, vip_path=vip_path)
    assert brief["vip"] is None
    assert brief["recent_interactions"] == []
    assert brief["search_terms_used"] == ["OnbekendIemand123"]


def test_aliases_includes_first_name_fallback() -> None:
    """Voor body-mentions: voeg first-name als aparte zoekterm toe."""
    terms = _aliases_for_search(None, "Martijn Scholten")
    assert "Martijn Scholten" in terms
    assert "Martijn" in terms


def test_aliases_skips_first_name_for_short_token() -> None:
    """1-2 letter first names (initialen) niet als losse term — te ruisig."""
    terms = _aliases_for_search(None, "M Scholten")
    assert "M Scholten" in terms
    assert "M" not in terms


def test_brief_finds_person_only_in_body(db: Path, vip_path: Path) -> None:
    """Person komt alleen in body voor (geen afzender) — moet gevonden."""
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        insert_item(c, CommItem(
            source="gmail", account="gmail", external_id="m99",
            folder=None, direction="in", from_addr="iemand@anders.nl",
            to_addrs=["user@example.com"], subject="ISE follow-up",
            occurred_at=now,
            body_full="Ik sprak gisteren met Martijn over de pricing alignment.",
            thread_ref="t99",
        ), summary="ISE follow-up", intent="fyi", sentiment="neutral")
    cal = MagicMock(); cal.search_events.return_value = []
    brief = build_person_brief(query="Martijn", db_path=db,
                                calendar=cal, vip_path=vip_path)
    assert len(brief["recent_interactions"]) == 1
    assert brief["recent_interactions"][0]["subject"] == "ISE follow-up"


def test_brief_queries_calendar_with_search_terms(db: Path, vip_path: Path) -> None:
    cal = MagicMock(); cal.search_events.return_value = []
    build_person_brief(query="Piet", db_path=db, calendar=cal, vip_path=vip_path)
    # Calendar search moet aangeroepen zijn — minstens één term gebruikt.
    assert cal.search_events.call_count >= 1
    used_queries = [c.kwargs.get("query") for c in cal.search_events.call_args_list]
    assert any("Piet" in (q or "") for q in used_queries)


# --- HIGH-4: query-validation tegen prompt-injection bulk-export -------

def test_validate_query_accepts_normal_name() -> None:
    ok, err = validate_query("Piet")
    assert ok and err is None


def test_validate_query_rejects_too_short() -> None:
    ok, err = validate_query("ab")
    assert not ok
    assert err is not None and "too short" in err


def test_validate_query_rejects_sql_wildcard_percent() -> None:
    ok, err = validate_query("%Martijn%")
    assert not ok
    assert err is not None and "wildcard" in err


def test_validate_query_rejects_sql_wildcard_underscore() -> None:
    ok, _ = validate_query("Mar_tijn")
    assert not ok


def test_validate_query_rejects_shell_wildcard_star() -> None:
    ok, _ = validate_query("Mart*")
    assert not ok


def test_validate_query_rejects_apostrophe() -> None:
    ok, _ = validate_query("O'Neill")
    # Apostrof is SQL-quote-risk; we weigeren em conservatief en de
    # caller kan eventueel via aliases naam zonder ' aanleveren.
    assert not ok


def test_validate_query_rejects_punctuation_only() -> None:
    ok, err = validate_query("...")
    assert not ok
    assert err is not None and "alphanumeric" in err


def test_brief_returns_error_on_invalid_query(db: Path, vip_path: Path) -> None:
    cal = MagicMock(); cal.search_events.return_value = []
    brief = build_person_brief(query="%", db_path=db,
                                calendar=cal, vip_path=vip_path)
    assert brief.get("rejected") is True
    assert "error" in brief
    # Calendar mag NIET geraakt zijn — early return vóór alle work.
    assert cal.search_events.call_count == 0


def test_person_brief_schema_enforces_minlength_and_pattern() -> None:
    """L5: prevent a future edit from silently loosening the schema."""
    from extensions.person_brief.tools import PERSON_BRIEF_TOOL_SCHEMAS
    q = PERSON_BRIEF_TOOL_SCHEMAS[0]["input_schema"]["properties"]["query"]
    assert q["minLength"] == 3
    assert q["pattern"] == "^[^%_*']+$"


def test_brief_caps_interactions_at_5(db: Path, vip_path: Path) -> None:
    """Hard cap: vraag 50, krijg max 5."""
    now = int(_time.time())
    with sqlite3.connect(db) as c:
        for i in range(12):
            insert_item(c, CommItem(
                source="gmail", account="gmail", external_id=f"m{i}",
                folder=None, direction="in", from_addr="piet@klant.nl",
                to_addrs=["user@example.com"], subject=f"Mail {i}",
                occurred_at=now - i, body_full="x",
                thread_ref=f"t{i}",
            ), summary=f"s{i}", intent="fyi", sentiment="neutral")
    cal = MagicMock(); cal.search_events.return_value = []
    brief = build_person_brief(
        query="Piet", db_path=db, calendar=cal, vip_path=vip_path,
        interaction_limit=50,  # caller asks for 50
    )
    assert len(brief["recent_interactions"]) == 5
