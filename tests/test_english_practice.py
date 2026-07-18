"""Tests voor english_practice schema + tool-handlers.

Hits a real SQLite file (per-test tmp_path) — geen mocks nodig voor de
storage-laag. Evaluate-handler test gebruikt heuristic fallback (gateway=None)
zodat we geen Claude nodig hebben.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from extensions.english_practice.schema import (
    LEITNER_INTERVALS_DAYS,
    end_session,
    get_card,
    get_state,
    init_english_practice_schema,
    insert_card,
    review_card,
    set_active_card,
    start_session,
)
from extensions.english_practice.tools import (
    english_practice_end_handler,
    english_practice_evaluate_handler,
    english_practice_skip_handler,
    english_practice_start_handler,
    english_practice_status_handler,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_english_practice_schema(p)
    return p


def _seed(db_path: Path, cards: list[tuple[str, str | None]]) -> list[int]:
    ids: list[int] = []
    with sqlite3.connect(db_path) as conn:
        for col, unit in cards:
            rid = insert_card(conn, collocation=col, unit_title=unit)
            assert rid is not None
            ids.append(rid)
    return ids


# --- schema ---------------------------------------------------------------

def test_insert_card_dedups_on_collocation(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        a = insert_card(conn, collocation="fierce competition")
        b = insert_card(conn, collocation="fierce competition")
    assert a is not None
    assert b is None


def test_review_correct_promotes_box_and_extends_due(db_path: Path) -> None:
    ids = _seed(db_path, [("fierce competition", "Business")])
    with sqlite3.connect(db_path) as conn:
        before = get_card(conn, ids[0])
        updated = review_card(conn, ids[0], correct=True)
    assert before["box"] == 1
    assert updated["box"] == 2
    assert updated["correct_count"] == 1
    # next_due_at should be about LEITNER_INTERVALS_DAYS[2] days in the future
    expected_delta = LEITNER_INTERVALS_DAYS[2] * 86400
    delta = updated["next_due_at"] - int(time.time())
    assert abs(delta - expected_delta) < 120  # within 2 minutes


def test_review_wrong_resets_to_box_1(db_path: Path) -> None:
    ids = _seed(db_path, [("fierce competition", None)])
    with sqlite3.connect(db_path) as conn:
        review_card(conn, ids[0], correct=True)
        review_card(conn, ids[0], correct=True)  # box 3
        updated = review_card(conn, ids[0], correct=False)
    assert updated["box"] == 1
    assert updated["wrong_count"] == 1
    # ~1 day in the future
    assert (updated["next_due_at"] - int(time.time())) >= 86000


def test_box_clamped_at_5(db_path: Path) -> None:
    ids = _seed(db_path, [("fierce competition", None)])
    with sqlite3.connect(db_path) as conn:
        for _ in range(10):
            review_card(conn, ids[0], correct=True)
        card = get_card(conn, ids[0])
    assert card["box"] == 5


def test_state_singleton_starts_empty(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        state = get_state(conn)
    assert state["singleton"] == 1
    assert state["active_session_id"] is None
    assert state["active_card_id"] is None


def test_start_and_end_session_lifecycle(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        sid = start_session(conn)
        state_mid = get_state(conn)
        end_session(conn, sid)
        state_after = get_state(conn)
    assert state_mid["active_session_id"] == sid
    assert state_after["active_session_id"] is None


# --- tools handlers -------------------------------------------------------

def test_start_with_no_due_cards_returns_inactive(db_path: Path) -> None:
    out = english_practice_start_handler(db_path, {})
    assert out["ok"] is True
    assert out["active"] is False


def test_start_picks_a_due_card_and_sets_active(db_path: Path) -> None:
    _seed(db_path, [
        ("fierce competition", "Business reports"),
        ("market share", "Business reports"),
    ])
    out = english_practice_start_handler(db_path, {})
    assert out["ok"] is True
    assert out["active"] is True
    card = out["card"]
    assert card["collocation"] in {"fierce competition", "market share"}

    with sqlite3.connect(db_path) as conn:
        state = get_state(conn)
    assert state["active_card_id"] == card["card_id"]


def test_start_with_unit_filter(db_path: Path) -> None:
    _seed(db_path, [
        ("fierce competition", "Business reports"),
        ("blond hair", "Personal appearance"),
    ])
    out = english_practice_start_handler(db_path, {"unit_filter": "business"})
    assert out["active"] is True
    assert "Business" in (out["card"]["unit_title"] or "")


def test_evaluate_without_active_card_errors(db_path: Path) -> None:
    out = english_practice_evaluate_handler(
        db_path, {"answer": "We face fierce competition."}, gateway=None,
    )
    assert "error" in out


def test_evaluate_correct_promotes_card_and_records_stats(db_path: Path) -> None:
    _seed(db_path, [("fierce competition", "Business")])
    started = english_practice_start_handler(db_path, {})
    assert started["active"] is True

    out = english_practice_evaluate_handler(
        db_path,
        {"answer": "Our company faces fierce competition in Q3.",
         "continue_session": False},
        gateway=None,
    )
    assert out["ok"] is True
    assert out["correct"] is True
    assert out["graded_card"]["new_box"] == 2

    with sqlite3.connect(db_path) as conn:
        state = get_state(conn)
        row = conn.execute(
            "SELECT cards_reviewed, correct, wrong FROM english_sessions "
            "WHERE id=?", (state["active_session_id"],),
        ).fetchone()
    assert state["active_card_id"] is None
    assert (row[0], row[1], row[2]) == (1, 1, 0)


def test_evaluate_wrong_sentence_marks_wrong(db_path: Path) -> None:
    _seed(db_path, [("fierce competition", None)])
    english_practice_start_handler(db_path, {})
    out = english_practice_evaluate_handler(
        db_path,
        {"answer": "Today is sunny.", "continue_session": False},
        gateway=None,
    )
    assert out["correct"] is False
    assert out["graded_card"]["new_box"] == 1  # reset / stays at 1


def test_evaluate_with_continue_loads_next_card(db_path: Path) -> None:
    _seed(db_path, [
        ("fierce competition", "Business"),
        ("market share", "Business"),
    ])
    english_practice_start_handler(db_path, {})
    out = english_practice_evaluate_handler(
        db_path,
        {"answer": "We have a strong market share and face fierce competition.",
         "continue_session": True},
        gateway=None,
    )
    assert out["next_card"] is not None
    assert out["next_card"]["collocation"] != out["graded_card"]["collocation"]


def test_skip_picks_next_card_when_available(db_path: Path) -> None:
    _seed(db_path, [
        ("fierce competition", None),
        ("market share", None),
    ])
    english_practice_start_handler(db_path, {})
    out = english_practice_skip_handler(db_path, {})
    assert out["skipped"] is True
    assert out["next_card"] is not None


def test_status_reports_due_and_box_counts(db_path: Path) -> None:
    _seed(db_path, [
        ("fierce competition", None),
        ("market share", None),
    ])
    out = english_practice_status_handler(db_path, {})
    assert out["due_total"] == 2
    assert out["by_box"] == {1: 2}


def test_end_returns_totals(db_path: Path) -> None:
    _seed(db_path, [("fierce competition", None)])
    english_practice_start_handler(db_path, {})
    english_practice_evaluate_handler(
        db_path, {"answer": "fierce competition is everywhere.",
                  "continue_session": False},
        gateway=None,
    )
    out = english_practice_end_handler(db_path, {})
    assert out["ended"] is True
    assert out["cards_reviewed"] == 1


# --- evaluate path with mock gateway --------------------------------------

class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeGateway:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, **kwargs) -> _FakeResp:  # noqa: ANN003
        self.calls += 1
        return _FakeResp(self.payload)


def test_evaluate_uses_gateway_json_when_available(db_path: Path) -> None:
    _seed(db_path, [("fierce competition", None)])
    english_practice_start_handler(db_path, {})
    gateway = _FakeGateway(
        '{"correct": false, "uses_collocation": true, '
        '"feedback": "Phrasing is awkward.", '
        '"better_example": "We face fierce competition."}'
    )
    out = english_practice_evaluate_handler(
        db_path,
        {"answer": "Fierce competition does happen always.",
         "continue_session": False},
        gateway=gateway,
    )
    assert gateway.calls == 1
    assert out["correct"] is False
    assert "awkward" in out["verdict"]["feedback"].lower()


def test_evaluate_falls_back_when_gateway_returns_non_json(db_path: Path) -> None:
    _seed(db_path, [("fierce competition", None)])
    english_practice_start_handler(db_path, {})
    gateway = _FakeGateway("I'd say correct because the words appear.")
    out = english_practice_evaluate_handler(
        db_path,
        {"answer": "fierce competition shapes our quarter.",
         "continue_session": False},
        gateway=gateway,
    )
    # heuristic fallback kicks in — words are present in order → correct
    assert out["correct"] is True


def test_set_active_card_clears_when_none(db_path: Path) -> None:
    ids = _seed(db_path, [("fierce competition", None)])
    with sqlite3.connect(db_path) as conn:
        set_active_card(conn, ids[0])
        assert get_state(conn)["active_card_id"] == ids[0]
        set_active_card(conn, None)
        assert get_state(conn)["active_card_id"] is None
