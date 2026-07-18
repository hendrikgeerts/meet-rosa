"""Person-brief: aggregator over alle bronnen die we al hebben.

Geen nieuw schema — pure read-only join over comm_items, plaud_meetings,
open_loops, calendar (live), en vip_contacts.yaml.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from core.query_safety import validate_query  # noqa: F401 — re-exported
from integrations.gcal import CalendarClient

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

# Hard output-cap: ongeacht args mag een single brief max 5 interactions
# + 5 meetings + 5 loops + 5 events richting Claude sturen. Voorheen
# defaults 10/5/10/5 — bij wildcard-query 10 termen × 10 interactions =
# 100 records (zie SECURITY_REVIEW_2 HIGH-4 bewijs).
_MAX_PER_BUCKET = 5
# Max search-terms per brief — consistent met _MAX_PER_BUCKET zodat de
# scan-cost ook gecapped is (was voorheen unbounded; de "100 records max"
# framing in het HIGH-4 commit-message gold alleen voor output, niet voor
# de SQL-LIKE branches).
_MAX_SEARCH_TERMS = 5


def load_vip_contacts(vip_path: Path) -> list[dict[str, Any]]:
    if not vip_path.exists():
        return []
    cfg = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}
    return list(cfg.get("people") or [])


def find_vip_match(query: str, vips: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match op naam (incl. aliases) of email — case-insensitive substring."""
    q = query.strip().lower()
    if not q:
        return None
    for p in vips:
        names = [str(p.get("name") or "")] + list(p.get("aliases") or [])
        if any(q in n.lower() for n in names if n):
            return p
        if any(q == e.lower() for e in (p.get("emails") or [])):
            return p
    return None


def _aliases_for_search(vip: dict[str, Any] | None, raw_query: str) -> list[str]:
    """Alle handles waarmee iemand in DB kan staan: naam + aliases + emails.
    Plus, als de raw_query meerdere woorden bevat ("Martijn Scholten"),
    voeg het eerste woord apart toe — vaak zo hoe mensen in mail-body
    worden genoemd ('ik sprak Martijn over X').
    Gebruikt voor LIKE-queries op comm_items.

    Cap op _MAX_SEARCH_TERMS — meer dan 3 aliases × 6 LIKE-kolommen × N
    dagen wordt een dure full-scan zonder veel extra recall. Prioriteit:
    raw query > VIP name > eerste alias > eerste email."""
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str | None) -> None:
        if s and s not in seen:
            out.append(s)
            seen.add(s)

    raw = raw_query.strip()
    if raw:
        add(raw)
        parts = raw.split()
        if len(parts) > 1 and len(parts[0]) >= 3:
            add(parts[0])
    if vip:
        add(vip.get("name"))
        for alias in (vip.get("aliases") or []):
            add(alias)
        for email in (vip.get("emails") or []):
            add(email)
    return out[:_MAX_SEARCH_TERMS]


def _comm_interactions(
    conn: sqlite3.Connection, *, search_terms: list[str], days_back: int, limit: int,
) -> list[dict[str, Any]]:
    """Recente comm_items waar één van de search_terms voorkomt in
    from/to/cc/subject — én als fallback ook in body_full + summary
    (voor mensen die alleen genoemd worden door derden)."""
    if not search_terms:
        return []
    since = int(_time.time()) - days_back * 86400
    conditions = []
    params: list[Any] = [since]
    for term in search_terms[:10]:  # cap voor SQL-lengte
        like = f"%{term}%"
        conditions.append(
            "(from_addr LIKE ? OR to_addrs LIKE ? OR cc_addrs LIKE ? "
            "OR subject LIKE ? OR body_full LIKE ? OR summary LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])
    sql = (
        "SELECT id, source, account, direction, from_addr, subject, "
        "       summary, intent, occurred_at, thread_ref "
        "FROM comm_items WHERE occurred_at >= ? AND ("
        + " OR ".join(conditions) + ") "
        "ORDER BY occurred_at DESC LIMIT ?"
    )
    params.append(limit)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "source": r["source"],
            "account": r["account"],
            "direction": r["direction"],
            "from_addr": r["from_addr"],
            "subject": r["subject"],
            "summary": r["summary"],
            "intent": r["intent"],
            "at": datetime.fromtimestamp(r["occurred_at"], TZ).isoformat(),
            "thread_ref": r["thread_ref"],
        }
        for r in rows
    ]


def _meeting_history(
    conn: sqlite3.Connection, *, search_terms: list[str], limit: int,
) -> list[dict[str, Any]]:
    """Plaud-meetings waar deze persoon als participant in de JSON-list
    voorkomt."""
    if not search_terms:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT m.id, m.transcript_id, m.summary, m.participants, "
        "       m.decisions, m.analyzed_at "
        "FROM plaud_meetings m ORDER BY m.analyzed_at DESC LIMIT 100"
    ).fetchall()
    out: list[dict[str, Any]] = []
    lower_terms = [t.lower() for t in search_terms]
    for r in rows:
        try:
            participants = json.loads(r["participants"] or "[]")
        except (ValueError, TypeError):
            participants = []
        if not any(any(t in str(p).lower() for t in lower_terms) for p in participants):
            continue
        try:
            decisions = json.loads(r["decisions"] or "[]")
        except (ValueError, TypeError):
            decisions = []
        out.append({
            "meeting_id": r["id"],
            "summary": r["summary"],
            "participants": participants,
            "decisions": decisions[:3],
            "at": datetime.fromtimestamp(r["analyzed_at"], TZ).isoformat(),
        })
        if len(out) >= limit:
            break
    return out


def _open_loops_for(
    conn: sqlite3.Connection, *, search_terms: list[str], limit: int,
) -> list[dict[str, Any]]:
    if not search_terms:
        return []
    conn.row_factory = sqlite3.Row
    conditions = []
    params: list[Any] = []
    for term in search_terms[:5]:
        conditions.append("who LIKE ?")
        params.append(f"%{term}%")
    sql = (
        "SELECT id, kind, who, title, created_at, due_at "
        "FROM open_loops WHERE status='open' AND (" + " OR ".join(conditions) + ") "
        "ORDER BY created_at DESC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "who": r["who"],
            "title": r["title"],
            "age_days": (int(_time.time()) - r["created_at"]) // 86400,
            "due": datetime.fromtimestamp(r["due_at"], TZ).isoformat() if r["due_at"] else None,
        }
        for r in rows
    ]


def _upcoming_events(
    calendar: CalendarClient, *, search_terms: list[str], days_forward: int, limit: int,
) -> list[dict[str, Any]]:
    """Calendar-events in de komende days_forward dagen waar één van
    search_terms in attendees of title voorkomt. Live Google query
    via search_events (uses q-param)."""
    if not search_terms:
        return []
    now = datetime.now(TZ)
    horizon = now + timedelta(days=days_forward)
    matched: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for term in search_terms[:5]:
        try:
            events = calendar.search_events(
                query=term, time_min=now, time_max=horizon, max_results=20,
            )
        except Exception:
            log.exception("person-brief: calendar.search_events failed for %r", term)
            continue
        for e in events:
            if e["id"] in seen_ids:
                continue
            seen_ids.add(e["id"])
            matched.append({
                "id": e["id"],
                "title": e["title"],
                "start": e["start"],
                "end": e["end"],
                "location": e.get("location") or "",
                "attendees": e.get("attendees") or [],
                "meet_url": e.get("meet_url"),
            })
            if len(matched) >= limit:
                break
        if len(matched) >= limit:
            break
    matched.sort(key=lambda ev: ev["start"] or "")
    return matched


def build_person_brief(
    *,
    query: str,
    db_path: Path,
    calendar: CalendarClient,
    vip_path: Path,
    days_back: int = 90,
    days_forward: int = 30,
    interaction_limit: int = 5,
    meeting_limit: int = 5,
    loop_limit: int = 5,
    event_limit: int = 5,
) -> dict[str, Any]:
    """Bouw een 1-page brief voor een persoon. Returnt dict met vip-info,
    recent interactions, meetings, open loops, upcoming events.

    Defends against prompt-injection-driven bulk exfiltration: rejects
    wildcard / too-short queries up front, and hard-caps each bucket at
    _MAX_PER_BUCKET regardless of the limits passed in.
    """
    ok, err = validate_query(query)
    if not ok:
        return {"query": query, "error": err, "rejected": True}

    # Hard caps — caller-provided limits can lower but never raise.
    interaction_limit = min(int(interaction_limit), _MAX_PER_BUCKET)
    meeting_limit = min(int(meeting_limit), _MAX_PER_BUCKET)
    loop_limit = min(int(loop_limit), _MAX_PER_BUCKET)
    event_limit = min(int(event_limit), _MAX_PER_BUCKET)

    vips = load_vip_contacts(vip_path)
    vip = find_vip_match(query, vips)
    search_terms = _aliases_for_search(vip, query)

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        interactions = _comm_interactions(
            conn, search_terms=search_terms,
            days_back=days_back, limit=interaction_limit,
        )
        meetings = _meeting_history(
            conn, search_terms=search_terms, limit=meeting_limit,
        )
        loops = _open_loops_for(
            conn, search_terms=search_terms, limit=loop_limit,
        )
    events = _upcoming_events(
        calendar, search_terms=search_terms,
        days_forward=days_forward, limit=event_limit,
    )

    return {
        "query": query,
        "vip": vip,                       # of None — Rosa kan dan zeggen "niet in VIP-lijst"
        "search_terms_used": search_terms,
        "recent_interactions": interactions,
        "meeting_history": meetings,
        "open_loops": loops,
        "upcoming_events": events,
        "stats": {
            "interactions_count": len(interactions),
            "open_loops_count": len(loops),
            "upcoming_events_count": len(events),
        },
    }
