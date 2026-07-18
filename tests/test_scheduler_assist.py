"""Tests voor scheduler_assist: detect, schema, propose pipeline, tools."""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from extensions.comm_intel.schema import CommItem, init_comm_schema, insert_item
from extensions.scheduler_assist.detect import is_scheduling_request
from extensions.scheduler_assist.propose import (
    _fmt_slot,
    estimate_duration_minutes,
    notify_followup_for_item,
    pick_slots,
)
from extensions.scheduler_assist.schema import (
    PendingProposal,
    find_recent_in_thread,
    get_proposal,
    init_scheduler_schema,
    insert_proposal,
    list_pending,
    mark_cancelled,
    mark_sent,
)
from extensions.scheduler_assist.tools import (
    cancel_proposal,
    send_proposal,
)

TZ = ZoneInfo("Europe/Amsterdam")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "sched.db"
    init_comm_schema(p)
    init_scheduler_schema(p)
    return p


# --- detect ---------------------------------------------------------------

@pytest.mark.parametrize("subject,body,expected", [
    ("Re: project", "Kunnen we ergens komende week afspreken?", True),
    ("Quick question", "Any time that works for a 30 min call?", True),
    ("Lunch", "Wanneer ben je beschikbaar deze week?", True),
    ("Newsletter", "Lees meer over ons product hier.", False),
    ("Update", "Zoals afgesproken stuur ik je de offerte.", False),  # negative
    ("Notulen", "Agenda punt 3 over Q3 cijfers.", False),  # negative
    # V2: noisy losse keywords mogen NIET meer triggeren zonder context
    ("Q3 plannen", "Hoe staat het met de plannen voor Q3?", False),  # plannen los
    ("Publishing schedule", "Our publishing schedule for next month", False),  # schedule los
    ("Webinar invitation", "Save the date for our webinar Tuesday", False),  # webinar negative
    ("Meeting reminder", "Reminder: your appointment is tomorrow at 3pm", False),  # reminder
    ("Confirmed", "Confirming our meeting tomorrow at 10am", False),  # confirmation
    ("Demo request", "Free webinar live demo - register now!", False),  # promo blast
    ("Agenda-overleg", "Agenda voor onze quarterly review staat hieronder", False),  # agenda-ref
])
def test_is_scheduling_request_keyword_paths(subject: str, body: str, expected: bool) -> None:
    item = {"direction": "in", "intent": "question",
            "subject": subject, "body_full": body}
    assert is_scheduling_request(item) is expected


def test_is_scheduling_request_llama_gate_says_no() -> None:
    """Regex hit, maar Llama zegt NO → False (vangt false positives)."""
    from unittest.mock import MagicMock
    fake_ollama = MagicMock()
    fake_resp = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "NO"
    fake_resp.content = [fake_block]
    fake_ollama.chat.return_value = fake_resp

    item = {"direction": "in", "intent": "question",
            "subject": "Re: contract", "body_full":
                "Are you free for a call about the new template designs?"}
    assert is_scheduling_request(item, ollama=fake_ollama) is False
    fake_ollama.chat.assert_called_once()


def test_is_scheduling_request_llama_gate_says_yes() -> None:
    """Regex hit + Llama YES → True."""
    from unittest.mock import MagicMock
    fake_ollama = MagicMock()
    fake_resp = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "YES"
    fake_resp.content = [fake_block]
    fake_ollama.chat.return_value = fake_resp

    item = {"direction": "in", "intent": "question",
            "subject": "Quick call?",
            "body_full": "Are you free Thursday afternoon for a quick call?"}
    assert is_scheduling_request(item, ollama=fake_ollama) is True


def test_is_scheduling_request_llama_failure_is_conservative() -> None:
    """Ollama gooit → False (geen ten-onrechte-proposal)."""
    from unittest.mock import MagicMock
    fake_ollama = MagicMock()
    fake_ollama.chat.side_effect = Exception("ollama down")

    item = {"direction": "in", "intent": "question",
            "subject": "Re: project",
            "body_full": "Wanneer kunnen we afspreken?"}
    assert is_scheduling_request(item, ollama=fake_ollama) is False


def test_is_scheduling_request_llama_unclear_answer_is_no() -> None:
    """Llama antwoordt iets onverwachts (niet YES/NO) → conservatieve NO."""
    from unittest.mock import MagicMock
    fake_ollama = MagicMock()
    fake_resp = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "Maybe? It depends..."
    fake_resp.content = [fake_block]
    fake_ollama.chat.return_value = fake_resp

    item = {"direction": "in", "intent": "question",
            "subject": "x", "body_full": "Wanneer kunnen we afspreken?"}
    assert is_scheduling_request(item, ollama=fake_ollama) is False


def test_is_scheduling_request_skips_llama_if_regex_misses() -> None:
    """Geen regex-hit → geen Llama-call (kostenbesparing)."""
    from unittest.mock import MagicMock
    fake_ollama = MagicMock()
    item = {"direction": "in", "intent": "question",
            "subject": "Just a heads up",
            "body_full": "Wanted to keep you informed about the rollout"}
    assert is_scheduling_request(item, ollama=fake_ollama) is False
    fake_ollama.chat.assert_not_called()


def test_is_scheduling_request_skips_outgoing() -> None:
    item = {"direction": "out", "intent": "question",
            "subject": "x", "body_full": "wanneer kunnen we afspreken?"}
    assert not is_scheduling_request(item)


def test_is_scheduling_request_skips_non_question_intent() -> None:
    item = {"direction": "in", "intent": "fyi",
            "subject": "x", "body_full": "afspraak vrijdag"}
    assert not is_scheduling_request(item)


def test_is_scheduling_request_passes_when_intent_none() -> None:
    """Summarize-fail (Ollama timeout) → intent=None → don't block detection."""
    item = {"direction": "in", "intent": None,
            "subject": "Overleg", "body_full": "Wanneer kunnen we afspreken?"}
    assert is_scheduling_request(item)


# --- duration heuristic --------------------------------------------------

def test_duration_explicit_hour() -> None:
    assert estimate_duration_minutes("Heb je 2 uur volgende week?") == 120


def test_duration_explicit_minutes() -> None:
    assert estimate_duration_minutes("kunnen we een 45 min call doen?") == 45


def test_duration_kort_means_30() -> None:
    assert estimate_duration_minutes("kort vragen of we even kunnen bellen") == 30


def test_duration_demo_signal_45() -> None:
    assert estimate_duration_minutes("Ik wil graag een demo bekijken") == 45


def test_duration_default_30() -> None:
    assert estimate_duration_minutes("kunnen we ergens praten?") == 30


# --- pick_slots ----------------------------------------------------------

def test_pick_slots_one_per_day() -> None:
    """Find_free_slots geeft meerdere kandidaten — pick_slots houdt max 1
    per dag voor spreiding."""
    base = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
    raw_slots = [
        {"start": (base + timedelta(days=1, hours=h)).isoformat(),
         "end": (base + timedelta(days=1, hours=h + 1)).isoformat()}
        for h in range(0, 4)  # 4 slots op dag 1
    ] + [
        {"start": (base + timedelta(days=2)).isoformat(),
         "end": (base + timedelta(days=2, hours=1)).isoformat()},
        {"start": (base + timedelta(days=3)).isoformat(),
         "end": (base + timedelta(days=3, hours=1)).isoformat()},
    ]
    cal = MagicMock()
    cal.find_free_slots.return_value = raw_slots
    out = pick_slots(cal, duration_minutes=30, max_slots=3)
    assert len(out) == 3
    dates = {datetime.fromisoformat(s["start"]).date() for s in out}
    assert len(dates) == 3   # 1 per dag


# --- schema CRUD ---------------------------------------------------------

def _insert_test_item(db: Path) -> int:
    """Voeg een test comm_item toe en return id."""
    item = CommItem(
        source="gmail", account="gmail", external_id="ext1",
        folder=None, direction="in", from_addr="piet@klant.nl",
        to_addrs=["you@example.com"],
        subject="Re: planning", occurred_at=int(_time.time()),
        body_full="Wanneer kunnen we afspreken?", thread_ref="thread-abc",
    )
    with sqlite3.connect(db) as c:
        rid = insert_item(c, item, summary="vraag om afspraak",
                          intent="question", sentiment="neutral")
    return rid


def test_insert_proposal_dedups_per_comm_item(db: Path) -> None:
    item_id = _insert_test_item(db)
    p = PendingProposal(
        comm_item_id=item_id, sender="piet@klant.nl", subject="Re: planning",
        thread_ref="thread-abc", reply_via_source="gmail",
        reply_via_account=None,
        reply_from_address="you@example.com",
        duration_minutes=30, add_meet_link=True, slots=[],
        draft_subject="Re: planning", draft_body="hoi",
    )
    with sqlite3.connect(db) as c:
        first = insert_proposal(c, p)
        second = insert_proposal(c, p)
    assert first is not None
    assert second is None  # UNIQUE comm_item_id


def test_list_pending_only_pending(db: Path) -> None:
    item_id = _insert_test_item(db)
    p = PendingProposal(
        comm_item_id=item_id, sender="x@y.nl", subject="x",
        thread_ref=None, reply_via_source="gmail", reply_via_account=None,
        reply_from_address="me@me.nl", duration_minutes=30, add_meet_link=True,
        slots=[], draft_subject="x", draft_body="x",
    )
    with sqlite3.connect(db) as c:
        pid = insert_proposal(c, p)
        rows = list_pending(c)
    assert any(r["id"] == pid for r in rows)

    with sqlite3.connect(db) as c:
        mark_cancelled(c, pid)
        rows2 = list_pending(c)
    assert all(r["id"] != pid for r in rows2)


# --- send_proposal tool --------------------------------------------------

def test_send_proposal_routes_to_gmail_for_gmail_source(db: Path) -> None:
    item_id = _insert_test_item(db)
    slot_start = datetime.now(TZ) + timedelta(days=2, hours=2)
    p = PendingProposal(
        comm_item_id=item_id, sender="piet@klant.nl", subject="Re: planning",
        thread_ref="thread-abc", reply_via_source="gmail",
        reply_via_account=None,
        reply_from_address="you@example.com",
        duration_minutes=30, add_meet_link=True,
        slots=[{"start": slot_start.isoformat(),
                "end": (slot_start + timedelta(minutes=30)).isoformat()}],
        draft_subject="Re: planning", draft_body="Hoi Piet, hier zijn 3 slots...",
    )
    with sqlite3.connect(db) as c:
        pid = insert_proposal(c, p)

    gmail = MagicMock()
    gmail.send.return_value = {"id": "msg-1", "thread_id": "thread-abc"}
    cal = MagicMock()
    cal.create_event.return_value = {"id": "evt-1", "meet_url": "https://meet.google.com/abc"}

    result = send_proposal(
        db, {"proposal_id": pid, "chosen_slot_index": 1},
        gmail=gmail, calendar=cal, imap_accounts=[],
    )
    assert result["ok"] is True
    assert result["calendar_event_id"] == "evt-1"
    gmail.send.assert_called_once()
    cal.create_event.assert_called_once()
    # add_meet_link doorgegeven
    assert cal.create_event.call_args.kwargs["add_meet_link"] is True

    with sqlite3.connect(db) as c:
        prop = get_proposal(c, pid)
    assert prop["status"] == "sent"


def test_send_proposal_without_chosen_slot_skips_calendar(db: Path) -> None:
    """Eerste reply: alleen mail uit, nog geen event (klant moet kiezen)."""
    item_id = _insert_test_item(db)
    p = PendingProposal(
        comm_item_id=item_id, sender="piet@klant.nl", subject="Re: planning",
        thread_ref=None, reply_via_source="gmail", reply_via_account=None,
        reply_from_address="you@example.com",
        duration_minutes=30, add_meet_link=True,
        slots=[], draft_subject="x", draft_body="x",
    )
    with sqlite3.connect(db) as c:
        pid = insert_proposal(c, p)

    gmail = MagicMock()
    gmail.send.return_value = {"id": "msg-2", "thread_id": None}
    cal = MagicMock()
    result = send_proposal(
        db, {"proposal_id": pid},
        gmail=gmail, calendar=cal, imap_accounts=[],
    )
    assert result["ok"] is True
    assert result["calendar_event_id"] is None
    cal.create_event.assert_not_called()


def test_cancel_proposal(db: Path) -> None:
    item_id = _insert_test_item(db)
    p = PendingProposal(
        comm_item_id=item_id, sender="x@y.nl", subject="x",
        thread_ref=None, reply_via_source="gmail", reply_via_account=None,
        reply_from_address="me@me.nl", duration_minutes=30, add_meet_link=True,
        slots=[], draft_subject="x", draft_body="x",
    )
    with sqlite3.connect(db) as c:
        pid = insert_proposal(c, p)

    out = cancel_proposal(db, {"proposal_id": pid})
    assert out["ok"] is True
    with sqlite3.connect(db) as c:
        prop = get_proposal(c, pid)
    assert prop["status"] == "cancelled"


# --- _fmt_slot -----------------------------------------------------------

def test_find_recent_in_thread_matches_sent_proposal(db: Path) -> None:
    """Counter-reply op een thread → vinden we de eerdere proposal."""
    item_id = _insert_test_item(db)
    p = PendingProposal(
        comm_item_id=item_id, sender="piet@klant.nl", subject="Re: planning",
        thread_ref="thread-abc", reply_via_source="gmail",
        reply_via_account=None, reply_from_address="me@me.nl",
        duration_minutes=30, add_meet_link=True, slots=[],
        draft_subject="x", draft_body="x",
    )
    with sqlite3.connect(db) as c:
        pid = insert_proposal(c, p)
        mark_sent(c, pid, message_id="<m>", calendar_event_id=None)
        found = find_recent_in_thread(c, thread_ref="thread-abc")
    assert found is not None
    assert found["id"] == pid


def test_find_recent_in_thread_returns_none_for_unknown(db: Path) -> None:
    with sqlite3.connect(db) as c:
        assert find_recent_in_thread(c, thread_ref="never-existed") is None
        assert find_recent_in_thread(c, thread_ref=None) is None


def test_find_recent_in_thread_skips_old_proposals(db: Path) -> None:
    """Proposals ouder dan max_age_days tellen niet als 'related'."""
    item_id = _insert_test_item(db)
    with sqlite3.connect(db) as c:
        p = PendingProposal(
            comm_item_id=item_id, sender="x@y.nl", subject="x",
            thread_ref="thread-old", reply_via_source="gmail",
            reply_via_account=None, reply_from_address="me@me.nl",
            duration_minutes=30, add_meet_link=True, slots=[],
            draft_subject="x", draft_body="x",
        )
        pid = insert_proposal(c, p)
        # Force created_at to 60 days ago.
        c.execute(
            "UPDATE pending_proposals SET created_at = strftime('%s','now','-60 days') "
            "WHERE id=?", (pid,),
        )
        found = find_recent_in_thread(c, thread_ref="thread-old", max_age_days=30)
    assert found is None


def test_notify_followup_includes_prev_proposal_context(db: Path) -> None:
    sent_calls: list[tuple[str, str]] = []
    item = {
        "from_addr": "piet@klant.nl",
        "subject": "Re: Re: planning",
        "body_full": "Nee dan kan ik niet, volgende week dan?",
    }
    prev = {
        "id": 7,
        "status": "sent",
        "slots": [
            {"start": "2026-04-29T10:00:00+02:00", "end": "2026-04-29T10:30:00+02:00"},
            {"start": "2026-04-30T14:00:00+02:00", "end": "2026-04-30T14:30:00+02:00"},
        ],
    }
    notify_followup_for_item(
        item=item, prev_proposal=prev,
        send_imessage=lambda h, b: sent_calls.append((h, b)),
        primary_handle="+316",
    )
    assert len(sent_calls) == 1
    body = sent_calls[0][1]
    assert "proposal #7" in body
    assert "piet@klant.nl" in body
    assert "volgende week" in body
    assert "send 7 met slot N" in body
    assert "opnieuw N voorstellen voor 7" in body
    assert "cancel 7" in body


def test_fmt_slot_human_readable() -> None:
    s = "2026-04-29T14:00:00+02:00"
    e = "2026-04-29T14:30:00+02:00"
    assert _fmt_slot({"start": s, "end": e}) == "wo 29/04 14:00–14:30"
