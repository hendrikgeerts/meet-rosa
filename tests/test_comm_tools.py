"""Tests voor extensions.comm_intel.tools — query-helpers tegen een
in-memory comm_items tabel."""
from __future__ import annotations

import json
import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.comm_intel.schema import CommItem, init_comm_schema, insert_item
from extensions.comm_intel.tools import (
    comm_about_person, comm_recent, comm_search, comm_thread,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "db.sqlite"
    init_comm_schema(p)
    now = int(_time.time())

    items = [
        # Vandaag, gmail, in
        (CommItem(source="gmail", account="gmail", external_id="g1",
                  direction="in", occurred_at=now - 3600,
                  body_full="Vraag over offerte van Heineken", from_addr="piet@klant.nl",
                  to_addrs=["hendrik@x.nl"], subject="Offerte vraag",
                  thread_ref="t1"),
         "Piet vraagt offerte-uitbreiding", "question", "neutral"),
        # Gisteren, slack, out
        (CommItem(source="slack", account="hendrikslack", external_id="s1",
                  direction="out", occurred_at=now - 86400,
                  body_full="Eens kijken morgen", from_addr="hendrik",
                  to_addrs=["C001"], subject="", thread_ref=None,
                  folder="general"),
         "Bevestiging morgen kijken", "fyi", "neutral"),
        # 5 dagen terug, imap, in
        (CommItem(source="imap", account="dpm", external_id="i1",
                  direction="in", occurred_at=now - 5 * 86400,
                  body_full="Status update Heineken", from_addr="judith@foresco.eu",
                  to_addrs=["you@example.com"], subject="Status",
                  thread_ref="t2", folder="INBOX"),
         "Judith over Heineken-status", "fyi", "positive"),
        # 40 dagen terug — buiten default zoekvenster
        (CommItem(source="gmail", account="gmail", external_id="g_old",
                  direction="in", occurred_at=now - 40 * 86400,
                  body_full="Oud nieuws", from_addr="news@example.com",
                  subject="Nieuwsbrief"),
         "Oud", "newsletter", "neutral"),
        # Same thread t1
        (CommItem(source="gmail", account="gmail", external_id="g2",
                  direction="out", occurred_at=now - 3500,
                  body_full="Antwoord aan Piet", from_addr="hendrik@x.nl",
                  to_addrs=["piet@klant.nl"], subject="Re: Offerte vraag",
                  thread_ref="t1"),
         "Antwoord met details", "task", "neutral"),
    ]

    with sqlite3.connect(p) as c:
        for it, sm, intent, sent in items:
            insert_item(c, it, summary=sm, intent=intent, sentiment=sent)
    return p


# --- comm_recent -----------------------------------------------------------

def test_recent_default_returns_today(db: Path) -> None:
    out = comm_recent(db, {})
    # default days=1, so only items from last 24h
    ids = {r["id"] for r in out}
    assert all(r["at"] for r in out)
    assert any(r["source"] == "gmail" and r["from"] == "piet@klant.nl" for r in out)
    # Older items excluded
    assert not any(r["from"] == "judith@foresco.eu" for r in out)


def test_recent_filtered_by_source(db: Path) -> None:
    out = comm_recent(db, {"source": "slack", "days": 7})
    assert all(r["source"] == "slack" for r in out)


def test_recent_filtered_by_direction(db: Path) -> None:
    out = comm_recent(db, {"direction": "out", "days": 7})
    assert all(r["direction"] == "out" for r in out)


def test_recent_respects_limit(db: Path) -> None:
    out = comm_recent(db, {"days": 60, "limit": 2})
    assert len(out) == 2


# --- comm_search -----------------------------------------------------------

def test_search_matches_summary(db: Path) -> None:
    out = comm_search(db, {"query": "Heineken", "days": 30})
    # Two summaries mention Heineken (Piet's vraag + Judith's status)
    summaries = [r["summary"] for r in out]
    assert any("Piet vraagt" in s for s in summaries)
    assert any("Judith over Heineken" in s for s in summaries)


def test_search_matches_subject(db: Path) -> None:
    out = comm_search(db, {"query": "Status", "days": 30})
    assert any(r["subject"] == "Status" for r in out)


def test_search_skips_old_outside_window(db: Path) -> None:
    """Default 30-day window excludes 40-day-old item."""
    out = comm_search(db, {"query": "nieuws"})
    assert not any(r["from"] == "news@example.com" for r in out)


def test_search_empty_query_returns_empty(db: Path) -> None:
    assert comm_search(db, {"query": ""}) == []


# --- comm_about_person ----------------------------------------------------

def test_about_matches_from_address(db: Path) -> None:
    out = comm_about_person(db, {"person": "piet@klant.nl"})
    assert all("piet" in (r["from"].lower() + json.dumps(r["to"]).lower())
               for r in out)


def test_about_matches_in_to_addrs(db: Path) -> None:
    out = comm_about_person(db, {"person": "piet@klant.nl"})
    # Ook outgoing bericht aan piet moet matchen
    assert any(r["direction"] == "out" for r in out)


def test_about_substring_match(db: Path) -> None:
    out = comm_about_person(db, {"person": "judith"})
    assert len(out) == 1
    assert "judith" in out[0]["from"].lower()


# --- comm_thread ----------------------------------------------------------

def test_thread_returns_messages_in_order(db: Path) -> None:
    out = comm_thread(db, {"thread_ref": "t1"})
    assert len(out) == 2
    assert out[0]["occurred_at"] < out[1]["occurred_at"] if all("occurred_at" in r for r in out) else True
    # Body excerpt should be present
    assert all("body_excerpt" in r for r in out)


def test_thread_unknown_ref_returns_empty(db: Path) -> None:
    assert comm_thread(db, {"thread_ref": "nope"}) == []


# --- HIGH-4: wildcard / short-query rejection in user-supplied queries ---

def test_search_rejects_query_with_wildcards(db: Path) -> None:
    """Wildcard-containing queries are rejected outright (not stripped &
    continued) — a prompt-injected '%Heineken%' is suspicious; return
    empty rather than silently normalising it."""
    assert comm_search(db, {"query": "%Heineken%", "days": 30}) == []


def test_search_rejects_short_query(db: Path) -> None:
    """1- and 2-char queries match too much; minLength=3 enforced."""
    assert comm_search(db, {"query": "a", "days": 30}) == []
    assert comm_search(db, {"query": "ab", "days": 30}) == []


def test_search_accepts_normal_query(db: Path) -> None:
    out = comm_search(db, {"query": "Heineken", "days": 30})
    assert len(out) >= 1


def test_about_rejects_query_with_wildcards(db: Path) -> None:
    assert comm_about_person(db, {"person": "%judith%"}) == []


def test_about_rejects_short_person(db: Path) -> None:
    assert comm_about_person(db, {"person": "j"}) == []


# --- L5: schema-assertion tests (catch silent loosening) -----------------

def test_comm_search_schema_enforces_minlength_and_pattern() -> None:
    from extensions.comm_intel.tools import COMM_TOOL_SCHEMAS
    schema = next(t for t in COMM_TOOL_SCHEMAS if t["name"] == "comm_search")
    q = schema["input_schema"]["properties"]["query"]
    assert q["minLength"] == 3
    assert q["pattern"] == "^[^%_*']+$"


def test_comm_about_person_schema_enforces_minlength_and_pattern() -> None:
    from extensions.comm_intel.tools import COMM_TOOL_SCHEMAS
    schema = next(t for t in COMM_TOOL_SCHEMAS if t["name"] == "comm_about_person")
    p = schema["input_schema"]["properties"]["person"]
    assert p["minLength"] == 3
    assert p["pattern"] == "^[^%_*']+$"
