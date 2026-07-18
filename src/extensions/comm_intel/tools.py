"""Orchestrator-tools voor comm-intel: comm_recent / comm_search /
comm_about_person / comm_thread.

Returns zijn beknopt (samenvattingen + metadata, geen body's). Zo stappen
ze klein door de privacy-gateway naar Claude. Body-full blijft 100% lokaal
in de DB tenzij de specifieke `comm_thread`-tool er expliciet om vraagt
(met inhoudelijke truncation).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

from core.query_safety import QUERY_SCHEMA, validate_query

log = logging.getLogger(__name__)

COMM_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "comm_recent",
        "description": (
            "List recent communications across mail (Gmail/IMAP) and Slack. "
            "Returns summaries only (body's stay local). Use to answer "
            "questions like 'wat is er vandaag binnengekomen' or 'wat heb "
            "ik gisteren gestuurd'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["gmail", "imap", "slack", "all"], "default": "all"},
                "direction": {"type": "string", "enum": ["in", "out", "both"], "default": "both"},
                "days": {"type": "integer", "minimum": 1, "maximum": 1825, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
            },
        },
    },
    {
        "name": "comm_search",
        "description": (
            "Search through stored communications for a keyword. Matches both "
            "summary and body. Use for 'heb ik iets gehoord over X' / "
            "'welke offertes lopen' / etc. Query must be ≥3 chars and contain "
            "no wildcards (%, _, *, ')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {**QUERY_SCHEMA},
                "days": {"type": "integer", "minimum": 1, "maximum": 1825, "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
            },
            "required": ["query"],
        },
    },
    {
        "name": "comm_about_person",
        "description": (
            "Find communications involving a specific person (matched by "
            "email address or display-name substring in from/to/cc/Slack-user). "
            "Person must be ≥3 chars and contain no wildcards (%, _, *, ')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {**QUERY_SCHEMA, "description": "name fragment or email"},
                "days": {"type": "integer", "minimum": 1, "maximum": 1825, "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15},
            },
            "required": ["person"],
        },
    },
    {
        "name": "comm_thread",
        "description": (
            "Show all known messages in one thread (Gmail threadId / IMAP "
            "References / Slack thread_ts). Returns summaries plus a "
            "truncated body excerpt per message so Claude can reason over "
            "the conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_ref": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 15},
            },
            "required": ["thread_ref"],
        },
    },
    {
        "name": "comm_thread_summary",
        "description": (
            "Vat een complete thread samen via Claude. Use bij lange threads "
            "(>5 berichten) waar the user de samenvatting wil ipv de volledige "
            "comm_thread output door te lezen. Returns: 1-paragraf overzicht + "
            "key decisions + open vragen + wie-zei-wat highlights."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_ref": {"type": "string"},
                "max_items": {"type": "integer", "minimum": 3, "maximum": 50, "default": 30},
            },
            "required": ["thread_ref"],
        },
    },
    {
        "name": "comm_unanswered",
        "description": (
            "Threads waar het LAATSTE bericht een inkomende is — d.w.z. "
            "the user heeft nog niet (in dezelfde thread) geantwoord. "
            "Bredere set dan loops_open: bevat ook FYI-mail, geen Llama-"
            "intent-filter. Gebruik bij vragen als 'wat staat er nog open "
            "in mijn Slack', 'welke mails moet ik nog beantwoorden', "
            "'inbox-status'. Newsletters/social worden standaard "
            "weggefilterd; zet include_noise=true om ze ook te tonen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["gmail", "imap", "slack", "all"], "default": "all"},
                "account": {"type": "string", "description": "filter op bv. 'hendrikdpm'"},
                "days": {"type": "integer", "minimum": 1, "maximum": 1825, "default": 14},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                "include_noise": {"type": "boolean", "default": False,
                                   "description": "include newsletters/social/fyi-only items"},
            },
        },
    },
    {
        "name": "comm_semantic_search",
        "description": (
            "Semantische zoektocht over de hele opgeslagen mail-historie via "
            "embedding-similarity (lokaal nomic-embed-text + sqlite-vec). "
            "Gebruik bij vragen die NIET op exact-keyword maar op betekenis "
            "draaien — 'wat speelt er rond [klant]', 'is er gediscussieerd "
            "over [thema]', 'welke mails gingen over [project]'. Returns "
            "top-K relevante items met body-excerpt zodat Claude kan "
            "redeneren over context. Voor exacte 'wanneer mailde X me?' "
            "zoekvragen: gebruik comm_search of comm_about_person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "natuurlijke-taal vraag (NL of EN)"},
                "k": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
                "days": {"type": "integer", "minimum": 1, "maximum": 1825,
                         "description": "alleen items uit laatste N dagen (optioneel)"},
                "source": {"type": "string", "enum": ["gmail", "imap", "slack"],
                           "description": "filter op één bron (optioneel)"},
            },
            "required": ["query"],
        },
    },
]


# --- handlers --------------------------------------------------------------

def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_summary(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "source": r["source"],
        "account": r["account"],
        "folder": r["folder"],
        "direction": r["direction"],
        "from": r["from_addr"],
        "to": _json_or_empty(r["to_addrs"]),
        "subject": r["subject"],
        "at": _iso(r["occurred_at"]),
        "summary": r["summary"],
        "intent": r["intent"],
        "sentiment": r["sentiment"],
        "thread_ref": r["thread_ref"],
    }


def _json_or_empty(s: str | None) -> list[str]:
    if not s:
        return []
    try:
        return list(json.loads(s))
    except (ValueError, TypeError):
        return []


def _iso(ts: int) -> str:
    if not ts:
        return ""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo("Europe/Amsterdam")).isoformat()


def comm_recent(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    source = args.get("source", "all")
    direction = args.get("direction", "both")
    days = int(args.get("days", 1))
    limit = int(args.get("limit", 15))
    since = int(_time.time()) - days * 86400

    sql = "SELECT * FROM comm_items WHERE occurred_at >= ?"
    params: list[Any] = [since]
    if source != "all":
        sql += " AND source = ?"
        params.append(source)
    if direction != "both":
        sql += " AND direction = ?"
        params.append(direction)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)

    with _conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_summary(r) for r in rows]


def comm_search(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = (args.get("query") or "").strip()
    # Validate first (rejects too-short / wildcards / non-alnum-only); strip
    # is defence-in-depth for any wildcard char that slips through (e.g. a
    # caller bypassing the JSON-schema layer). SECURITY_REVIEW_2 HIGH-4.
    ok, err = validate_query(query)
    if not ok:
        log.info("comm_search rejected: %s", err)
        return []
    query = query.translate(str.maketrans("", "", "%_"))
    if not query:
        return []
    days = int(args.get("days", 30))
    limit = int(args.get("limit", 15))
    since = int(_time.time()) - days * 86400
    like = f"%{query}%"
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM comm_items "
            "WHERE occurred_at >= ? "
            "AND (summary LIKE ? OR subject LIKE ? OR body_full LIKE ?) "
            "ORDER BY occurred_at DESC LIMIT ?",
            (since, like, like, like, limit),
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


def comm_about_person(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    person = (args.get("person") or "").strip()
    ok, err = validate_query(person)
    if not ok:
        log.info("comm_about_person rejected: %s", err)
        return []
    person = person.translate(str.maketrans("", "", "%_"))
    if not person:
        return []
    days = int(args.get("days", 30))
    limit = int(args.get("limit", 15))
    since = int(_time.time()) - days * 86400
    like = f"%{person}%"
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM comm_items "
            "WHERE occurred_at >= ? "
            "AND (from_addr LIKE ? OR to_addrs LIKE ? OR cc_addrs LIKE ?) "
            "ORDER BY occurred_at DESC LIMIT ?",
            (since, like, like, like, limit),
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


def comm_thread(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    thread_ref = (args.get("thread_ref") or "").strip()
    if not thread_ref:
        return []
    limit = int(args.get("limit", 15))
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM comm_items WHERE thread_ref = ? "
            "ORDER BY occurred_at ASC LIMIT ?",
            (thread_ref, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_summary(r)
        # Truncated body excerpt — useful for Claude in thread context.
        d["body_excerpt"] = (r["body_full"] or "")[:500]
        out.append(d)
    return out


_THREAD_SUMMARY_PROMPT = (
    "Je bent Rosa. Vat onderstaande mail/Slack-thread samen voor the user. "
    "Schrijf in het Engels, beknopt:\n"
    "- Eén paragraaf (max 4 zinnen) wat de thread tot dusver inhoudt.\n"
    "- Key decisions/agreements (bullets, max 5).\n"
    "- Open questions (bullets, max 3).\n"
    "- Wie heeft welke commitment gemaakt (\"X said Y\", max 3).\n"
    "Gebruik geen plichtplegingen. Sla over wat onbelangrijk is."
)


def comm_thread_summary(
    db_path: Path, args: dict[str, Any], *, gateway: Any,
) -> dict[str, Any]:
    """Vat een thread samen door alle items op te halen + Claude
    synthesis. Gebruikt gateway met internal-label (mail = niet publiek)."""
    thread_ref = (args.get("thread_ref") or "").strip()
    if not thread_ref:
        return {"error": "thread_ref required"}
    max_items = int(args.get("max_items", 30))

    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT direction, from_addr, subject, body_full, occurred_at "
            "FROM comm_items WHERE thread_ref = ? "
            "ORDER BY occurred_at ASC LIMIT ?",
            (thread_ref, max_items),
        ).fetchall()
    if not rows:
        return {"error": f"no items in thread {thread_ref!r}"}

    parts = []
    for r in rows:
        excerpt = (r["body_full"] or "")[:1500]
        parts.append(
            f"[{r['direction']}] {_iso(r['occurred_at'])} — {r['from_addr']}\n"
            f"Subject: {r['subject']}\n"
            f"{excerpt}\n"
        )
    payload = "Thread items (chronologisch):\n\n" + "\n---\n".join(parts)

    response = gateway.complete(
        task="thread_summary",
        system=_THREAD_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": payload}],
        max_tokens=600,
    )
    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    return {
        "thread_ref": thread_ref,
        "item_count": len(rows),
        "summary": text or "(empty summary)",
    }


def comm_unanswered(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Threads waar het laatste bericht inkomend is = jij nog niet
    geantwoord. Per thread alleen het laatste in-bericht teruggeven
    zodat de lijst kort blijft."""
    source = args.get("source", "all")
    account = (args.get("account") or "").strip() or None
    days = int(args.get("days", 14))
    limit = int(args.get("limit", 30))
    include_noise = bool(args.get("include_noise", False))
    since = int(_time.time()) - days * 86400

    # Items zonder thread_ref (eenmalige mailtjes) tellen we als
    # afzonderlijke "thread" met thread_ref = '__noref:<id>' zodat ook
    # die in de unanswered-set kunnen verschijnen — anders zou bv. een
    # Slack-DM zonder thread nooit zichtbaar zijn.
    sql = """
    WITH latest AS (
      SELECT
        COALESCE(NULLIF(thread_ref, ''), '__noref:' || id) AS tref,
        MAX(occurred_at) AS latest_at
      FROM comm_items
      WHERE occurred_at >= ?
      GROUP BY tref
    )
    SELECT ci.*
    FROM comm_items ci
    JOIN latest l
      ON COALESCE(NULLIF(ci.thread_ref, ''), '__noref:' || ci.id) = l.tref
     AND ci.occurred_at = l.latest_at
    WHERE ci.direction = 'in'
    """
    params: list[Any] = [since]
    if source != "all":
        sql += " AND ci.source = ?"
        params.append(source)
    if account:
        sql += " AND ci.account = ?"
        params.append(account)
    if not include_noise:
        sql += " AND (ci.intent IS NULL OR ci.intent NOT IN ('newsletter','social'))"
    sql += " ORDER BY ci.occurred_at DESC LIMIT ?"
    params.append(limit)

    with _conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_summary(r) for r in rows]


def comm_semantic_search(
    db_path: Path, args: dict[str, Any],
) -> list[dict[str, Any]]:
    """RAG-stijl retrieval: top-K mails op semantische similarity. Geeft
    body-excerpt mee zodat Claude meteen context heeft."""
    from extensions.comm_intel.embeddings import _open_with_vec, search

    query = str(args.get("query", "")).strip()
    if not query:
        return []
    k = int(args.get("k", 8))
    days = args.get("days")
    source = args.get("source")
    since_unix = (int(_time.time()) - int(days) * 86400) if days else None

    with _open_with_vec(db_path) as conn:
        return search(conn, query, k=k,
                      since_unix=since_unix, source=source)


def response_time_stats(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    from extensions.comm_intel.response_time import collect_per_sender_stats
    raw_days = args.get("days", 90)
    try:
        days = max(7, min(int(raw_days), 365))
    except (TypeError, ValueError):
        days = 90
    stats = collect_per_sender_stats(db_path, days=days)
    raw_limit = args.get("limit", 20)
    try:
        limit = max(1, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        limit = 20
    return {
        "days": days, "count": len(stats),
        "stats": stats[:limit],
    }


def response_time_overdue(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    from extensions.comm_intel.response_time import find_overdue_threads
    raw_factor = args.get("factor", 1.5)
    try:
        factor = max(1.0, min(float(raw_factor), 10.0))
    except (TypeError, ValueError):
        factor = 1.5
    raw_min = args.get("min_age_hours", 24.0)
    try:
        min_age = max(1.0, min(float(raw_min), 720.0))
    except (TypeError, ValueError):
        min_age = 24.0
    raw_limit = args.get("limit", 5)
    try:
        limit = max(1, min(int(raw_limit), 50))
    except (TypeError, ValueError):
        limit = 5
    items = find_overdue_threads(
        db_path, factor=factor, min_age_hours=min_age, limit=limit,
    )
    return {"factor": factor, "min_age_hours": min_age,
             "count": len(items), "items": items}


COMM_TOOL_SCHEMAS.extend([
    {
        "name": "response_time_stats",
        "description": (
            "Per-afzender gemiddelde response-tijd (uren) over de "
            "afgelopen N dagen. Geeft per from_addr het aantal threads, "
            "mean_hours en median_hours. Use bij vragen 'hoe snel reageer "
            "ik op X?', 'wie wacht het langst?', 'wat is mijn baseline?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 7, "maximum": 365, "default": 90},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    {
        "name": "response_time_overdue",
        "description": (
            "Threads waar het laatste bericht inkomend is EN ouder dan "
            "het normale antwoord-tempo voor die afzender (median × factor) "
            "OF ouder dan min_age_hours globaal. Toont wie 'het langst' "
            "wacht naar the user's eigen baseline gemeten. Use bij 'wie "
            "wacht het langst?' / 'welke draft moet ik nu maken?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "factor": {"type": "number", "minimum": 1.0, "maximum": 10.0, "default": 1.5},
                "min_age_hours": {"type": "number", "minimum": 1.0, "maximum": 720.0, "default": 24.0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
            },
        },
    },
])


def comm_topics_active(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    from extensions.comm_intel.topics import collect_active_topics
    raw_days = args.get("days", 14)
    try:
        days = max(3, min(int(raw_days), 90))
    except (TypeError, ValueError):
        days = 14
    raw_min = args.get("min_items", 3)
    try:
        min_items = max(2, min(int(raw_min), 20))
    except (TypeError, ValueError):
        min_items = 3
    topics = collect_active_topics(
        db_path, days=days, min_items=min_items,
    )
    return {"days": days, "min_items": min_items,
             "count": len(topics), "topics": topics}


def comm_topic_items(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    from extensions.comm_intel.topics import collect_topic_items
    topic = str(args.get("topic") or "").strip().lower()
    if len(topic) < 3:
        return {"error": "topic too short (min 3 chars)"}
    raw_days = args.get("days", 30)
    try:
        days = max(3, min(int(raw_days), 365))
    except (TypeError, ValueError):
        days = 30
    raw_limit = args.get("limit", 20)
    try:
        limit = max(1, min(int(raw_limit), 50))
    except (TypeError, ValueError):
        limit = 20
    items = collect_topic_items(db_path, topic=topic, days=days, limit=limit)
    return {"topic": topic, "days": days,
             "count": len(items), "items": items}


COMM_TOOL_SCHEMAS.extend([
    {
        "name": "comm_topics_active",
        "description": (
            "Detecteer thematische clusters in mail/Slack van de "
            "afgelopen N dagen: tokens uit subject/summary die in "
            "≥ min_items distinct items voorkomen. Use bij vragen "
            "'wat speelt er deze week' / 'waar gaat de meeste mail "
            "over' / 'topics?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 3, "maximum": 90, "default": 14},
                "min_items": {"type": "integer", "minimum": 2, "maximum": 20, "default": 3},
            },
        },
    },
    {
        "name": "comm_topic_items",
        "description": (
            "Geef comm_items waar één topic-string in subject of "
            "summary voorkomt. Use bij 'wat zit er allemaal in over "
            "Q3-cijfers' / 'laat me alle threads over X zien'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "minLength": 3},
                "days": {"type": "integer", "minimum": 3, "maximum": 365, "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["topic"],
        },
    },
])


COMM_HANDLERS = {
    "comm_recent": comm_recent,
    "comm_search": comm_search,
    "comm_about_person": comm_about_person,
    "comm_thread": comm_thread,
    "comm_thread_summary": comm_thread_summary,   # needs gateway via wiring
    "comm_unanswered": comm_unanswered,
    "comm_semantic_search": comm_semantic_search,
    "response_time_stats": response_time_stats,
    "response_time_overdue": response_time_overdue,
    "comm_topics_active": comm_topics_active,
    "comm_topic_items": comm_topic_items,
}
