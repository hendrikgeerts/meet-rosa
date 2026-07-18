"""Orchestrator tools for English collocations practice.

The flow:
- `english_practice_start` picks the next due card, marks it active, and
  returns the collocation + a prompt instruction. Rosa relays this to
  the user via iMessage.
- the user writes a sentence. The orchestrator notices `active_card_id`
  is set and calls `english_practice_evaluate(answer)`, which uses Claude
  to judge BUSINESS-English correctness strictly. The Leitner box is
  updated, the active card is cleared (or replaced with the next due one
  if `continue_session=true`), and the verdict + next prompt are returned.
- `english_practice_skip` lets the user bail on a card without scoring.
- `english_practice_end` closes the session and returns the score.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from extensions.english_practice.schema import (
    due_cards,
    end_session,
    get_card,
    get_state,
    increment_session_count,
    review_card,
    set_active_card,
    start_session,
)

log = logging.getLogger(__name__)


ENGLISH_PRACTICE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "english_practice_start",
        "description": (
            "Start an English collocation practice session and present the "
            "first card. USE THIS when the user says things like 'practice', "
            "'english practice', 'start english', 'oefenen', 'engels "
            "oefenen', or replies positively to a daily English-practice "
            "reminder. Returns a collocation the user must use in a sentence. "
            "After this tool runs, the user's NEXT iMessage will be his "
            "answer — call `english_practice_evaluate` with it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_filter": {
                    "type": "string",
                    "description": (
                        "Optional substring to filter unit_title "
                        "(e.g. 'business', 'marketing'). Case-insensitive."
                    ),
                },
            },
        },
    },
    {
        "name": "english_practice_evaluate",
        "description": (
            "Evaluate the user's sentence against the currently active "
            "collocation card. USE THIS whenever there is an active card "
            "(check via `english_practice_status` if unsure) and the user's "
            "message is plausibly an attempt at the exercise sentence. "
            "Strict business-English judging: correct usage, natural "
            "phrasing, and proper register. Updates Leitner box. If "
            "`continue_session=true` and more cards are due, automatically "
            "presents the next collocation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": (
                        "the user's full sentence, verbatim."
                    ),
                },
                "continue_session": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "If true, after scoring this card immediately load "
                        "the next due card so the session keeps rolling."
                    ),
                },
            },
            "required": ["answer"],
        },
    },
    {
        "name": "english_practice_skip",
        "description": (
            "Skip the current active card without scoring. Picks the next "
            "due card (or ends the session if none). Use when the user says "
            "'skip', 'overslaan', 'next', 'volgende', or 'I don't know'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "english_practice_status",
        "description": (
            "Show practice state: active card (if any), session stats, "
            "and how many cards are due today. Use when the user asks "
            "'how am I doing with english' or 'engelse stand'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "english_practice_end",
        "description": (
            "End the active English practice session, return totals. Use "
            "when the user says 'stop', 'klaar', 'done', 'enough', or it "
            "is clearly time to wrap up."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# --- evaluation prompt ---------------------------------------------------

EVAL_SYSTEM = (
    "You are an encouraging English-language coach for a Dutch professional "
    "(CEO of a digital-signage company). Judge whether the user's sentence "
    "uses the given English COLLOCATION in a sensible way.\n\n"
    "Be LENIENT — the goal is practice, not exam-grading. Mark `correct=true` "
    "when: the collocation appears (allowing free inflection: "
    "'meeting'/'meetings', 'adjourn'/'adjourned'/'adjourns'), AND the "
    "surrounding sentence shows the user grasps the meaning. Minor "
    "phrasing awkwardness, mild grammar errors elsewhere in the sentence, "
    "or non-business contexts are FINE — don't penalise those.\n\n"
    "Only mark `correct=false` when: (a) the collocation is missing or "
    "fundamentally misused (meaning is wrong), or (b) the sentence is so "
    "broken that the collocation usage can't be assessed. In feedback, lead "
    "with what's good before noting any improvement.\n\n"
    "Return ONLY a JSON object — no prose, no markdown — with keys:\n"
    "  correct: bool\n"
    "  uses_collocation: bool   (did the sentence actually contain it?)\n"
    "  feedback: string         (one short, warm sentence)\n"
    "  better_example: string|null  (a model sentence; nice-to-have even when correct)\n"
)


def _build_eval_prompt(collocation: str, answer: str) -> str:
    return (
        f"Collocation: {collocation}\n"
        f"User sentence: {answer}\n\n"
        "Judge now."
    )


def _evaluate_with_gateway(
    gateway: Any, collocation: str, answer: str,
) -> dict[str, Any]:
    """Call Claude via gateway, parse JSON. Falls back to a heuristic if
    LLM is unreachable so the user isn't blocked when offline."""
    if gateway is None:
        return _heuristic_eval(collocation, answer)
    try:
        resp = gateway.complete(
            task="english_practice_eval",
            system=EVAL_SYSTEM,
            messages=[{"role": "user",
                       "content": _build_eval_prompt(collocation, answer)}],
            max_tokens=300,
            force_label="public",  # generic English-grammar judgment
        )
    except Exception:
        log.exception("english_practice: gateway eval failed, using heuristic")
        return _heuristic_eval(collocation, answer)

    text = _extract_text(resp).strip()
    text = text.strip("`")
    if text.startswith("json"):
        text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("english_practice: model returned non-JSON: %r", text[:200])
        return _heuristic_eval(collocation, answer)
    return {
        "correct": bool(data.get("correct")),
        "uses_collocation": bool(data.get("uses_collocation")),
        "feedback": str(data.get("feedback") or "").strip(),
        "better_example": data.get("better_example"),
    }


def _extract_text(resp: Any) -> str:
    """Anthropic SDK Message → first text block."""
    content = getattr(resp, "content", None)
    if not content:
        return ""
    for block in content:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


def _heuristic_eval(collocation: str, answer: str) -> dict[str, Any]:
    """Offline fallback — only checks that the collocation words appear in
    order with at most a few intervening tokens. Marks correct only if
    that's true; otherwise wrong with a soft 'I couldn't reach the
    grading service' note."""
    a = answer.lower()
    c_words = collocation.lower().split()
    pos = -1
    for w in c_words:
        # trim possessive/apostrophe markers from c_words for relaxed match
        stem = w.rstrip("'s").rstrip("s")
        idx = a.find(stem, pos + 1)
        if idx < 0:
            return {
                "correct": False,
                "uses_collocation": False,
                "feedback": (
                    "Sentence doesn't contain the collocation "
                    "(offline grading)."
                ),
                "better_example": None,
            }
        pos = idx
    return {
        "correct": True,
        "uses_collocation": True,
        "feedback": "Looks ok (offline grading — no semantic check).",
        "better_example": None,
    }


# --- handlers ------------------------------------------------------------

def _ensure_session(conn: sqlite3.Connection) -> int:
    state = get_state(conn)
    sid = state.get("active_session_id")
    if sid:
        return int(sid)
    return start_session(conn)


def _pick_due_card(
    conn: sqlite3.Connection, unit_filter: str | None = None,
) -> dict[str, Any] | None:
    cards = due_cards(conn, limit=50)
    if unit_filter:
        needle = unit_filter.lower()
        cards = [c for c in cards if needle in (c.get("unit_title") or "").lower()]
    return cards[0] if cards else None


def _present_card(card: dict[str, Any]) -> dict[str, Any]:
    """Shape a card for the orchestrator → Rosa relays it to the user."""
    return {
        "card_id": card["id"],
        "collocation": card["collocation"],
        "unit_title": card.get("unit_title"),
        "box": card.get("box"),
        "instructions": (
            "Write one business-English sentence that uses this collocation "
            "naturally and correctly."
        ),
    }


def english_practice_start_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    unit_filter = args.get("unit_filter")
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        sid = _ensure_session(conn)
        card = _pick_due_card(conn, unit_filter)
        if card is None:
            return {
                "ok": True,
                "active": False,
                "message": (
                    "No cards are due right now."
                    if not unit_filter else
                    f"No due cards match unit '{unit_filter}'."
                ),
                "session_id": sid,
            }
        set_active_card(conn, card["id"])
    return {"ok": True, "active": True, "session_id": sid, "card": _present_card(card)}


def english_practice_evaluate_handler(
    db_path: Path, args: dict[str, Any], *, gateway: Any = None,
) -> dict[str, Any]:
    answer = str(args.get("answer", "")).strip()
    continue_session = bool(args.get("continue_session", True))
    if not answer:
        return {"error": "answer is required"}

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        state = get_state(conn)
        active_id = state.get("active_card_id")
        if not active_id:
            return {"error": "no active english practice card"}
        sid = state.get("active_session_id") or _ensure_session(conn)
        card = get_card(conn, int(active_id))
        if card is None:
            return {"error": f"active card {active_id} not found"}

        verdict = _evaluate_with_gateway(gateway, card["collocation"], answer)
        is_correct = bool(verdict["correct"] and verdict["uses_collocation"])
        updated = review_card(conn, int(active_id), correct=is_correct)
        increment_session_count(conn, int(sid), correct=is_correct)

        next_payload: dict[str, Any] | None = None
        if continue_session:
            nxt = _pick_due_card(conn)
            if nxt and nxt["id"] != int(active_id):
                set_active_card(conn, nxt["id"])
                next_payload = _present_card(nxt)
            else:
                set_active_card(conn, None)
        else:
            set_active_card(conn, None)

    return {
        "ok": True,
        "graded_card": {
            "collocation": card["collocation"],
            "unit_title": card.get("unit_title"),
            "old_box": card["box"],
            "new_box": updated["box"] if updated else card["box"],
        },
        "verdict": verdict,
        "correct": is_correct,
        "next_card": next_payload,
        "session_id": sid,
    }


def english_practice_skip_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        state = get_state(conn)
        active_id = state.get("active_card_id")
        nxt = _pick_due_card(conn)
        # Move past the active card by picking a different one if available.
        if nxt and active_id and nxt["id"] == int(active_id):
            # Same card surfaced — try another batch and pick a different one.
            others = [c for c in due_cards(conn, limit=20)
                      if c["id"] != int(active_id)]
            nxt = others[0] if others else None
        if nxt:
            set_active_card(conn, nxt["id"])
            return {"ok": True, "skipped": True,
                    "next_card": _present_card(nxt)}
        set_active_card(conn, None)
    return {"ok": True, "skipped": True, "next_card": None,
            "message": "No more cards due."}


def english_practice_status_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        state = get_state(conn)
        active_id = state.get("active_card_id")
        active_card = get_card(conn, int(active_id)) if active_id else None
        # Stats
        totals = conn.execute(
            "SELECT box, COUNT(*) AS c FROM english_cards GROUP BY box"
        ).fetchall()
        due_total = conn.execute(
            "SELECT COUNT(*) FROM english_cards "
            "WHERE next_due_at <= strftime('%s','now')"
        ).fetchone()[0]
        session_id = state.get("active_session_id")
        session_stats = None
        if session_id:
            row = conn.execute(
                "SELECT cards_reviewed, correct, wrong FROM english_sessions "
                "WHERE id=?", (int(session_id),),
            ).fetchone()
            if row:
                session_stats = {
                    "cards_reviewed": row[0],
                    "correct": row[1],
                    "wrong": row[2],
                }
    return {
        "active_card": (
            _present_card(active_card) if active_card else None
        ),
        "due_total": due_total,
        "by_box": {int(b): int(c) for b, c in totals},
        "session_id": session_id,
        "session_stats": session_stats,
    }


def english_practice_end_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        state = get_state(conn)
        sid = state.get("active_session_id")
        if not sid:
            return {"ok": True, "ended": False, "message": "No active session."}
        row = conn.execute(
            "SELECT cards_reviewed, correct, wrong FROM english_sessions "
            "WHERE id=?", (int(sid),),
        ).fetchone()
        end_session(conn, int(sid))
    if not row:
        return {"ok": True, "ended": True, "session_id": sid}
    return {
        "ok": True,
        "ended": True,
        "session_id": sid,
        "cards_reviewed": row[0],
        "correct": row[1],
        "wrong": row[2],
    }


ENGLISH_PRACTICE_HANDLERS = {
    "english_practice_start": english_practice_start_handler,
    "english_practice_evaluate": english_practice_evaluate_handler,
    "english_practice_skip": english_practice_skip_handler,
    "english_practice_status": english_practice_status_handler,
    "english_practice_end": english_practice_end_handler,
}
