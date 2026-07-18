"""pending_proposals schema — één rij per detectie waarvoor Rosa the user
wil bellen (iMessage) met een concept-mailreply + 3 voorgestelde slots."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comm_item_id INTEGER NOT NULL UNIQUE
        REFERENCES comm_items(id),
    sender TEXT NOT NULL,
    subject TEXT,
    thread_ref TEXT,                  -- gmail thread_id of imap message-id voor reply-threading
    reply_via_source TEXT NOT NULL,   -- 'gmail' / 'imap'
    reply_via_account TEXT,           -- imap account name (NULL voor gmail)
    reply_from_address TEXT NOT NULL, -- exact From-adres dat we gebruiken
    duration_minutes INTEGER NOT NULL DEFAULT 30,
    add_meet_link INTEGER NOT NULL DEFAULT 1,    -- bool: 0/1
    slots_json TEXT NOT NULL,         -- JSON array van {start, end} ISO strings
    draft_subject TEXT NOT NULL,
    draft_body TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sent','cancelled')),
    sent_at INTEGER,
    sent_message_id TEXT,
    sent_calendar_event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON pending_proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON pending_proposals(created_at DESC);
"""


def init_scheduler_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


@dataclass
class PendingProposal:
    comm_item_id: int
    sender: str
    subject: str
    thread_ref: str | None
    reply_via_source: str            # 'gmail' | 'imap'
    reply_via_account: str | None    # imap account name
    reply_from_address: str
    duration_minutes: int
    add_meet_link: bool
    slots: list[dict[str, str]] = field(default_factory=list)  # [{"start": iso, "end": iso}]
    draft_subject: str = ""
    draft_body: str = ""


def insert_proposal(
    conn: sqlite3.Connection, p: PendingProposal,
) -> int | None:
    """Insert proposal — returns id, of None bij UNIQUE-conflict op
    comm_item_id (al een proposal voor deze mail)."""
    try:
        cur = conn.execute(
            """
            INSERT INTO pending_proposals
              (comm_item_id, sender, subject, thread_ref, reply_via_source,
               reply_via_account, reply_from_address, duration_minutes,
               add_meet_link, slots_json, draft_subject, draft_body)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                p.comm_item_id, p.sender, p.subject, p.thread_ref,
                p.reply_via_source, p.reply_via_account, p.reply_from_address,
                p.duration_minutes, 1 if p.add_meet_link else 0,
                json.dumps(p.slots, ensure_ascii=False),
                p.draft_subject, p.draft_body,
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_proposals WHERE id=?", (proposal_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["slots"] = json.loads(d["slots_json"] or "[]")
    return d


def find_recent_in_thread(
    conn: sqlite3.Connection, *, thread_ref: str | None,
    max_age_days: int = 30,
) -> dict[str, Any] | None:
    """Zoek recente proposal (sent of pending) in dezelfde mail-thread.
    Gebruikt door scheduler_assist multi-turn — als een counter-reply
    arriveert op een thread waar Rosa al een voorstel heeft gestuurd,
    routeren we naar de follow-up-flow ipv een nieuwe proposal."""
    if not thread_ref:
        return None
    import time as _time
    cutoff = int(_time.time()) - max_age_days * 86400
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_proposals "
        "WHERE thread_ref = ? AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (thread_ref, cutoff),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["slots"] = json.loads(d["slots_json"] or "[]")
    return d


def list_pending(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM pending_proposals WHERE status='pending' "
        "ORDER BY created_at DESC LIMIT ?", (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["slots"] = json.loads(d["slots_json"] or "[]")
        out.append(d)
    return out


def mark_sent(
    conn: sqlite3.Connection, proposal_id: int, *,
    message_id: str | None, calendar_event_id: str | None,
) -> bool:
    cur = conn.execute(
        "UPDATE pending_proposals SET status='sent', "
        "sent_at=strftime('%s','now'), sent_message_id=?, "
        "sent_calendar_event_id=? WHERE id=? AND status='pending'",
        (message_id, calendar_event_id, proposal_id),
    )
    return cur.rowcount > 0


def mark_cancelled(conn: sqlite3.Connection, proposal_id: int) -> bool:
    cur = conn.execute(
        "UPDATE pending_proposals SET status='cancelled' "
        "WHERE id=? AND status='pending'", (proposal_id,),
    )
    return cur.rowcount > 0
