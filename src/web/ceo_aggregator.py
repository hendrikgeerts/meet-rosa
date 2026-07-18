"""Aggregator voor de CEO-dashboard pagina.

Pure SQL-queries over bestaande tabellen — geen Llama-call, geen
externe HTTP. Resultaat is dict-of-dicts dat door de Jinja-template
direct gerenderd kan worden.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")


def build_ceo_snapshot(
    db_path: Path, *, okrs_path: Path | None = None,
) -> dict[str, Any]:
    """Bouw alle secties van het CEO-dashboard in één pass."""
    now = datetime.now(TZ)
    week_start = int((now - timedelta(days=7)).timestamp())
    prev_week_start = int((now - timedelta(days=14)).timestamp())

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        return {
            "now": now.strftime("%Y-%m-%d %H:%M"),
            "volumes": _volumes(conn, week_start, prev_week_start),
            "loops": _loops(conn, now),
            "decisions": _decisions(conn, week_start),
            "patterns": _patterns(conn),
            "okrs": _okrs(okrs_path),
            "todoist": _todoist_summary(conn),
        }


def _volumes(
    conn: sqlite3.Connection, week_start: int, prev_week_start: int,
) -> dict[str, Any]:
    """Mail/Slack volumes deze week + delta t.o.v. vorige week."""
    cur = conn.execute("""
        SELECT source, direction, COUNT(*) n
          FROM comm_items
         WHERE occurred_at >= ?
         GROUP BY source, direction
    """, (week_start,))
    by_source: dict[str, dict[str, int]] = {}
    week_total_in = week_total_out = 0
    for r in cur.fetchall():
        by_source.setdefault(r["source"], {"in": 0, "out": 0})[r["direction"]] = r["n"]
        if r["direction"] == "in":
            week_total_in += r["n"]
        else:
            week_total_out += r["n"]

    prev = conn.execute("""
        SELECT direction, COUNT(*) n
          FROM comm_items
         WHERE occurred_at >= ? AND occurred_at < ?
         GROUP BY direction
    """, (prev_week_start, week_start)).fetchall()
    prev_in = next((r["n"] for r in prev if r["direction"] == "in"), 0)
    prev_out = next((r["n"] for r in prev if r["direction"] == "out"), 0)

    # Top correspondents (incoming + outgoing combined) — deze week
    top_corr = conn.execute("""
        SELECT
            CASE WHEN direction='in' THEN from_addr ELSE
                 (SELECT json_extract(to_addrs, '$[0]')) END as person,
            COUNT(*) n
          FROM comm_items
         WHERE occurred_at >= ? AND source IN ('gmail','imap')
         GROUP BY person
        HAVING person IS NOT NULL AND person != ''
         ORDER BY n DESC
         LIMIT 5
    """, (week_start,)).fetchall()

    return {
        "by_source": by_source,
        "week_in": week_total_in,
        "week_out": week_total_out,
        "delta_in": week_total_in - prev_in,
        "delta_out": week_total_out - prev_out,
        "top_correspondents": [dict(r) for r in top_corr],
    }


def _loops(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Open-loops aging buckets + top-5 oudste."""
    now_unix = int(now.timestamp())
    cur = conn.execute("""
        SELECT id, kind, who, title, action_summary, created_at, due_at
          FROM open_loops
         WHERE status='open'
         ORDER BY created_at ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]

    buckets = {"today": 0, "1-7d": 0, "8-14d": 0, "15-30d": 0, ">30d": 0}
    overdue = 0
    for r in rows:
        age = (now_unix - r["created_at"]) // 86400
        if age == 0:
            buckets["today"] += 1
        elif age <= 7:
            buckets["1-7d"] += 1
        elif age <= 14:
            buckets["8-14d"] += 1
        elif age <= 30:
            buckets["15-30d"] += 1
        else:
            buckets[">30d"] += 1
        if r["due_at"] and r["due_at"] < now_unix:
            overdue += 1

    top_oldest = []
    for r in rows[:5]:
        age = (now_unix - r["created_at"]) // 86400
        top_oldest.append({
            "id": r["id"], "kind": r["kind"], "who": r["who"],
            "action": r["action_summary"] or r["title"],
            "age_days": age,
            "overdue": bool(r["due_at"] and r["due_at"] < now_unix),
        })

    # Outgoing requests (the user wacht op anderen)
    waiting = [r for r in rows if r["kind"] == "outgoing_request"]
    return {
        "total_open": len(rows),
        "buckets": buckets,
        "overdue": overdue,
        "top_oldest": top_oldest,
        "waiting_count": len(waiting),
    }


def _decisions(
    conn: sqlite3.Connection, week_start: int,
) -> dict[str, Any]:
    """Decisions gelogd deze week."""
    try:
        rows = conn.execute("""
            SELECT id, title, context, decided_at
              FROM decisions
             WHERE decided_at >= ? AND status='active'
             ORDER BY decided_at DESC
             LIMIT 10
        """, (week_start,)).fetchall()
    except sqlite3.OperationalError:
        return {"count": 0, "recent": []}
    return {
        "count": len(rows),
        "recent": [
            {
                "id": r["id"], "title": r["title"],
                "context": (r["context"] or "")[:200],
                "decided": datetime.fromtimestamp(r["decided_at"], TZ).strftime("%a %d %b"),
            }
            for r in rows
        ],
    }


def _patterns(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("""
            SELECT id, kind, title, body, detected_at, snoozed_until
              FROM patterns
             WHERE (snoozed_until IS NULL OR snoozed_until <= strftime('%s','now'))
             ORDER BY detected_at DESC
             LIMIT 5
        """).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "id": r["id"], "title": r["title"],
            "body": (r["body"] or "")[:200],
            "detected": datetime.fromtimestamp(r["detected_at"], TZ).strftime("%d %b"),
        }
        for r in rows
    ]


def _okrs(okrs_path: Path | None) -> list[dict[str, Any]]:
    if okrs_path is None or not okrs_path.exists():
        return []
    try:
        from extensions.okrs.loader import load_okrs, to_briefing_snapshot
        return to_briefing_snapshot(load_okrs(okrs_path))
    except Exception:
        return []


def _todoist_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        open_count = conn.execute("""
            SELECT COUNT(*) FROM todoist_links
             WHERE completed_at_remote IS NULL
        """).fetchone()[0]
    except sqlite3.OperationalError:
        return {"open_count": 0, "upcoming": []}
    # Upcoming 3 reminders met due_at (combineer reminders + open_loops met due)
    upcoming = conn.execute("""
        SELECT 'reminder' as kind, r.id, r.body as title, r.remind_at as due_at
          FROM reminders r
          JOIN todoist_links l
            ON l.local_kind='reminder' AND l.local_id=r.id
         WHERE r.sent_at IS NULL AND r.cancelled_at IS NULL
           AND r.remind_at >= strftime('%s','now')
           AND l.completed_at_remote IS NULL
        UNION
        SELECT 'loop' as kind, lo.id, COALESCE(lo.action_summary, lo.title) as title,
               lo.due_at
          FROM open_loops lo
          JOIN todoist_links l
            ON l.local_kind='open_loop' AND l.local_id=lo.id
         WHERE lo.status='open' AND lo.due_at IS NOT NULL
           AND lo.due_at >= strftime('%s','now')
           AND l.completed_at_remote IS NULL
         ORDER BY due_at ASC
         LIMIT 5
    """).fetchall()
    return {
        "open_count": open_count,
        "upcoming": [
            {
                "kind": r["kind"], "title": (r["title"] or "")[:80],
                "due": datetime.fromtimestamp(r["due_at"], TZ).strftime("%a %d %b %H:%M"),
            }
            for r in upcoming
        ],
    }
