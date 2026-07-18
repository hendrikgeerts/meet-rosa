"""Tests voor extensions.open_loops — schema, dedupe, status-machine,
detect-helpers, en het comm-intel-coupling pad."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.comm_intel.schema import CommItem
from extensions.open_loops.detect import (
    close_for_outgoing_comm_item, sync_for_comm_item, track_for_comm_item,
)
from extensions.open_loops.schema import (
    OpenLoop, close_loop, close_loops_by_context, init_open_loops_schema,
    insert_loop, list_open, reopen_snoozed_due, snooze_loop,
)


def _conn(p: Path) -> sqlite3.Connection:
    init_open_loops_schema(p)
    c = sqlite3.connect(p, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _loop(**over) -> OpenLoop:
    base = dict(source="comm", source_ref="gmail:gmail:m1",
                kind="incoming_question", who="piet@klant.nl",
                title="Vraag over offerte", body_excerpt="Kun je iets sturen?",
                context="thread-abc")
    base.update(over)
    return OpenLoop(**base)


# --- schema ---------------------------------------------------------------

def test_insert_first_returns_id(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    rid = insert_loop(conn, _loop())
    assert isinstance(rid, int) and rid > 0


def test_insert_duplicate_source_ref_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_loop(conn, _loop())
    assert insert_loop(conn, _loop()) is None


def test_insert_with_no_source_ref_is_always_new(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_loop(conn, _loop(source_ref=None))
    rid2 = insert_loop(conn, _loop(source_ref=None))
    assert rid2 is not None  # both stored


def test_list_open_default(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_loop(conn, _loop(source_ref="g1", who="piet@klant.nl"))
    insert_loop(conn, _loop(source_ref="g2", who="piet@klant.nl",
                            kind="incoming_task"))
    rows = list_open(conn)
    assert len(rows) == 2


def test_list_open_filter_by_kind(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_loop(conn, _loop(source_ref="g1", kind="incoming_question"))
    insert_loop(conn, _loop(source_ref="g2", kind="incoming_task"))
    rows = list_open(conn, kind="incoming_task")
    assert len(rows) == 1
    assert rows[0]["kind"] == "incoming_task"


def test_list_open_filter_by_who_substring(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    insert_loop(conn, _loop(source_ref="g1", who="piet@klant.nl"))
    insert_loop(conn, _loop(source_ref="g2", who="anouk@andere.nl"))
    rows = list_open(conn, who="piet")
    assert len(rows) == 1


def test_close_loop_changes_status(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    rid = insert_loop(conn, _loop())
    assert close_loop(conn, rid, via="manual")
    rows = list_open(conn)
    assert rows == []  # niet meer open


def test_close_loops_by_context_only_incoming(tmp_path: Path) -> None:
    """Outgoing-reply moet alleen incoming-question/task closen, niet andere kinds."""
    conn = _conn(tmp_path / "db.sqlite")
    insert_loop(conn, _loop(source_ref="g1", kind="incoming_question",
                            context="thread-1"))
    insert_loop(conn, _loop(source_ref="g2", kind="meeting_action_self",
                            context="thread-1"))
    n = close_loops_by_context(conn, context="thread-1")
    assert n == 1
    open_rows = list_open(conn)
    assert len(open_rows) == 1
    assert open_rows[0]["kind"] == "meeting_action_self"


def test_snooze_then_reopen(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    rid = insert_loop(conn, _loop())
    snooze_loop(conn, rid, until_unix=int(_time.time()) - 10)  # already due
    assert list_open(conn) == []
    n = reopen_snoozed_due(conn)
    assert n == 1
    assert len(list_open(conn)) == 1


# --- detect ---------------------------------------------------------------

def _comm(**over) -> CommItem:
    base = dict(
        source="gmail", account="gmail", external_id="m1",
        direction="in", occurred_at=int(_time.time()),
        body_full="Kun je vóór vrijdag de offerte sturen?",
        from_addr="piet@klant.nl", to_addrs=["hendrik@x.nl"],
        subject="Offerte aanvraag", thread_ref="thread-1",
    )
    base.update(over)
    return CommItem(**base)


def test_track_creates_loop_for_incoming_question(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    rid = track_for_comm_item(conn, _comm(), intent="question")
    assert rid is not None
    rows = list_open(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "incoming_question"
    assert rows[0]["who"] == "piet@klant.nl"
    assert "Offerte" in rows[0]["title"]


def test_track_creates_loop_for_incoming_task(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    track_for_comm_item(conn, _comm(), intent="task")
    rows = list_open(conn)
    assert rows[0]["kind"] == "incoming_task"


def test_track_skips_outgoing(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    rid = track_for_comm_item(conn, _comm(direction="out"), intent="question")
    assert rid is None
    assert list_open(conn) == []


def test_track_skips_non_actionable_intents(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    for intent in ("fyi", "newsletter", "social", "other", None):
        assert track_for_comm_item(conn, _comm(), intent=intent) is None
    assert list_open(conn) == []


def test_track_dedupes_per_external_id(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    track_for_comm_item(conn, _comm(), intent="question")
    track_for_comm_item(conn, _comm(), intent="question")  # dezelfde external_id
    assert len(list_open(conn)) == 1


def test_outgoing_in_same_thread_closes_loop(tmp_path: Path) -> None:
    """Hendrik antwoordt → matching open loop in thread wordt closed."""
    conn = _conn(tmp_path / "db.sqlite")
    track_for_comm_item(conn, _comm(thread_ref="thread-A"), intent="question")
    assert len(list_open(conn)) == 1
    n = close_for_outgoing_comm_item(
        conn, _comm(direction="out", external_id="m2",
                    from_addr="hendrik@x.nl", thread_ref="thread-A"),
    )
    assert n == 1
    assert list_open(conn) == []


def test_outgoing_in_different_thread_does_nothing(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    track_for_comm_item(conn, _comm(thread_ref="thread-A"), intent="question")
    n = close_for_outgoing_comm_item(
        conn, _comm(direction="out", external_id="m2", thread_ref="thread-Z"),
    )
    assert n == 0
    assert len(list_open(conn)) == 1


def test_outgoing_without_thread_ref_skipped(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    track_for_comm_item(conn, _comm(thread_ref="thread-A"), intent="question")
    n = close_for_outgoing_comm_item(
        conn, _comm(direction="out", external_id="m2", thread_ref=None),
    )
    assert n == 0


# --- delegate-tracker (outgoing_request) -----------------------------------

from extensions.open_loops.detect import sync_for_comm_item


def test_outgoing_question_creates_outgoing_request_loop(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    out_item = _comm(
        direction="out", external_id="m_out_1",
        from_addr="hendrik@x.nl", to_addrs=["piet@klant.nl"],
        subject="Kun je de offerte vóór vrijdag sturen?",
        thread_ref="thread-d1",
    )
    new_id, closed = sync_for_comm_item(conn, out_item, intent="question")
    assert new_id is not None
    assert closed == 0
    rows = list_open(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "outgoing_request"
    assert rows[0]["who"] == "piet@klant.nl"
    assert "offerte" in rows[0]["title"].lower()


def test_incoming_reply_closes_outgoing_request(tmp_path: Path) -> None:
    """Hendrik vroeg Piet iets → outgoing_request open. Piet antwoordt
    in dezelfde thread → outgoing_request gesloten."""
    conn = _conn(tmp_path / "db.sqlite")
    sync_for_comm_item(conn, _comm(
        direction="out", external_id="m1", from_addr="hendrik@x.nl",
        to_addrs=["piet@klant.nl"], thread_ref="thread-X",
    ), intent="question")
    assert len(list_open(conn)) == 1

    _, closed = sync_for_comm_item(conn, _comm(
        direction="in", external_id="m2", from_addr="piet@klant.nl",
        thread_ref="thread-X", subject="RE: ...",
    ), intent="fyi")  # Piet's reply zelf hoeft geen task/question te zijn
    assert closed == 1
    assert list_open(conn) == []


def test_incoming_reply_does_not_touch_other_threads(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    sync_for_comm_item(conn, _comm(
        direction="out", external_id="m1", to_addrs=["piet@klant.nl"],
        thread_ref="thread-A",
    ), intent="question")
    sync_for_comm_item(conn, _comm(
        direction="in", external_id="m2", from_addr="piet@klant.nl",
        thread_ref="thread-Z",
    ), intent="fyi")
    rows = list_open(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "outgoing_request"


def test_outgoing_fyi_does_not_create_loop(tmp_path: Path) -> None:
    """Een uitgaande FYI-mail (geen vraag) is GEEN delegate-request."""
    conn = _conn(tmp_path / "db.sqlite")
    new_id, _ = sync_for_comm_item(conn, _comm(
        direction="out", external_id="m1", to_addrs=["piet@klant.nl"],
    ), intent="fyi")
    assert new_id is None
    assert list_open(conn) == []


def test_sync_dedupe_per_external_id(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "db.sqlite")
    sync_for_comm_item(conn, _comm(direction="out", external_id="m1",
                                    to_addrs=["piet@klant.nl"]), intent="question")
    sync_for_comm_item(conn, _comm(direction="out", external_id="m1",
                                    to_addrs=["piet@klant.nl"]), intent="question")
    assert len(list_open(conn)) == 1


def test_outgoing_reply_does_not_close_outgoing_request(tmp_path: Path) -> None:
    """Hendrik schrijft 2x naar Piet in zelfde thread (follow-up vraag).
    De tweede uitgaande mail mag de eerste outgoing_request NIET sluiten."""
    conn = _conn(tmp_path / "db.sqlite")
    sync_for_comm_item(conn, _comm(
        direction="out", external_id="m1", to_addrs=["piet@klant.nl"],
        thread_ref="thread-T",
    ), intent="question")
    # Tweede uitgaande mail in zelfde thread (intent fyi=geen nieuwe loop):
    new_id, closed = sync_for_comm_item(conn, _comm(
        direction="out", external_id="m2", to_addrs=["piet@klant.nl"],
        thread_ref="thread-T",
    ), intent="fyi")
    assert closed == 0   # outgoing_request blijft open
    rows = list_open(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "outgoing_request"


def test_incoming_question_does_not_close_outgoing_in_same_thread(tmp_path: Path) -> None:
    """Edge: Hendrik vraagt iets → Piet stelt een wedervraag (intent=question)
    in dezelfde thread. Onze `sync` sluit dan de outgoing_request én opent
    een nieuwe incoming_question — beiden mogen tegelijk bestaan."""
    conn = _conn(tmp_path / "db.sqlite")
    sync_for_comm_item(conn, _comm(
        direction="out", external_id="m1", to_addrs=["piet@klant.nl"],
        thread_ref="thread-T",
    ), intent="question")

    new_id, closed = sync_for_comm_item(conn, _comm(
        direction="in", external_id="m2", from_addr="piet@klant.nl",
        thread_ref="thread-T", subject="Wedervraag",
    ), intent="question")
    # Outgoing is gesloten (Piet heeft geantwoord) maar de nieuwe vraag
    # van Piet is een nieuwe incoming_question loop.
    assert closed == 1
    assert new_id is not None
    rows = list_open(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "incoming_question"


# --- v2 detector: closing/newsletter blocks + Llama yes/no gate ---------

def _fake_ollama(answer: str) -> object:
    """Maak een fake ollama-client die altijd `answer` retourneert."""
    from unittest.mock import MagicMock
    fake = MagicMock()
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = answer
    resp.content = [block]
    fake.chat.return_value = resp
    return fake


def test_closing_pattern_blocks_loop_even_with_question_intent(tmp_path: Path) -> None:
    """'Bedankt!' / 'akkoord' → geen loop ondanks intent=question."""
    conn = _conn(tmp_path / "db.sqlite")
    item = _comm(body_full="Bedankt voor je snelle reactie, helemaal duidelijk!")
    rid = track_for_comm_item(conn, item, intent="question")
    assert rid is None
    assert list_open(conn) == []


def test_newsletter_pattern_blocks_loop(tmp_path: Path) -> None:
    """Newsletter / unsubscribe / register-now → geen loop."""
    conn = _conn(tmp_path / "db.sqlite")
    item = _comm(body_full="Save the date! Register now for our free webinar.")
    rid = track_for_comm_item(conn, item, intent="question")
    assert rid is None


def test_llama_gate_blocks_false_positive(tmp_path: Path) -> None:
    """Intent=question maar Llama zegt NO → geen loop. Vangt bv. een
    nieuwsbrief-achtige mail die per ongeluk als question werd geclassified."""
    conn = _conn(tmp_path / "db.sqlite")
    item = _comm(body_full="Wat een mooie tijd om te beginnen — kijk eens!")
    rid = track_for_comm_item(conn, item, intent="question",
                                ollama=_fake_ollama("NO"))
    assert rid is None


def test_llama_gate_catches_implicit_question(tmp_path: Path) -> None:
    """Intent=other (Llama-summarizer miste het) maar action-keyword EN
    Llama bevestigt → loop opent alsnog (false-negative-fix)."""
    conn = _conn(tmp_path / "db.sqlite")
    item = _comm(body_full="Stuur jij me het rapport voor vrijdag?")
    rid = track_for_comm_item(conn, item, intent="other",
                                ollama=_fake_ollama("YES"))
    assert rid is not None
    rows = list_open(conn)
    assert rows[0]["kind"] == "incoming_question"


def test_sync_creates_outgoing_request_loop(tmp_path: Path) -> None:
    """sync_for_comm_item moet OUTGOING request-loops aanmaken — was bug
    in v1 (track_for_comm_item retourneerde altijd None voor direction=out).
    Hendrik's klacht: 'ik communiceer een actiepunt en Rosa pikt niet op'."""
    conn = _conn(tmp_path / "db.sqlite")
    item = _comm(direction="out", from_addr="hendrik@x.nl",
                  to_addrs=["roel@klant.nl"],
                  body_full="Stuur jij me even de pricing voor lite-pakket?")
    new_id, _closed = sync_for_comm_item(
        conn, item, intent="question", ollama=_fake_ollama("YES"),
    )
    assert new_id is not None
    rows = list_open(conn)
    assert rows[0]["kind"] == "outgoing_request"
    assert rows[0]["who"] == "roel@klant.nl"


def test_no_ollama_keeps_v1_behavior(tmp_path: Path) -> None:
    """Backwards-compat: zonder ollama-param gedraagt detector zich als v1
    (intent-only, geen action-keyword override)."""
    conn = _conn(tmp_path / "db.sqlite")
    # Intent=other met action-keyword → v1 zou GEEN loop maken
    item = _comm(body_full="Stuur jij me het rapport voor vrijdag?")
    rid = track_for_comm_item(conn, item, intent="other")  # geen ollama
    assert rid is None
