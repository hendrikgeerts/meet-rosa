"""project_status — aggregates linked items per project (keyword-scan).

Returnt voor één project: recente comm_items / decisions / open_loops die
één van de project-keywords matchen + komende calendar-events met een match
in title/description (caller passes calendar). Geen embeddings — simpele
LIKE over subject/body/title is genoeg voor "wat gebeurt er met project X?"
"""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.projects.schema import get_project

TZ = ZoneInfo("Europe/Amsterdam")


def project_status(
    db_path: Path, *,
    slug: str | None = None,
    project_id: int | None = None,
    days_back: int = 30,
    calendar: Any = None,           # CalendarClient — optional
    days_forward: int = 30,
) -> dict[str, Any]:
    """Return project + linked recent activity. Keyword-match over comm/
    decisions/open_loops. Calendar passes if provided."""
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        proj = get_project(conn, slug=slug, project_id=project_id)
        if not proj:
            return {"error": f"project not found: {slug or project_id}"}

        keywords = proj["keywords"] or []
        # Always include the title + slug as implicit search terms.
        search_terms = list({proj["title"], proj["slug"], *keywords})
        # Defense-in-depth: skip terms with SQL-LIKE wildcards. project
        # keywords come from the projects table (the user-curated via
        # dashboard); if a defaced dashboard somehow inserted "%" we'd
        # blast-scan everything. CRIT-B closes the dashboard hardening
        # but skipping wildcard terms here is cheap insurance.
        search_terms = [
            t for t in search_terms
            if t and len(t) >= 3 and not any(c in t for c in "%_*'")
        ]

        cutoff = int(_time.time()) - days_back * 86400

        recent_comm = _scan_comm(conn, terms=search_terms, since=cutoff,
                                  limit=10)
        recent_decisions = _scan_decisions(conn, terms=search_terms,
                                            since=cutoff, limit=10)
        open_loops = _scan_open_loops(conn, terms=search_terms, limit=10)

    upcoming_events: list[dict[str, Any]] = []
    if calendar is not None and search_terms:
        try:
            now = datetime.now(TZ)
            horizon = now + timedelta(days=days_forward)
            all_events = calendar.list_events(
                time_min=now, time_max=horizon, max_results=50,
            )
            upcoming_events = [
                e for e in all_events
                if any(t.lower() in (e.get("summary", "") or "").lower()
                       or t.lower() in (e.get("description", "") or "").lower()
                       for t in search_terms)
            ][:5]
        except Exception:
            pass

    return {
        "project": proj,
        "recent_comm": recent_comm,
        "recent_decisions": recent_decisions,
        "open_loops": open_loops,
        "upcoming_events": upcoming_events,
        "matched_terms": search_terms,
    }


def _scan_comm(
    conn: sqlite3.Connection, *, terms: list[str], since: int, limit: int,
) -> list[dict[str, Any]]:
    if not terms:
        return []
    try:
        sql = (
            "SELECT id, source, account, direction, from_addr, subject, "
            "occurred_at, summary FROM comm_items "
            "WHERE occurred_at >= ? AND ("
            + " OR ".join(["subject LIKE ? OR body_full LIKE ? OR summary LIKE ?"]
                          * len(terms))
            + ") ORDER BY occurred_at DESC LIMIT ?"
        )
        params: list[Any] = [since]
        for t in terms:
            like = f"%{t}%"
            params.extend([like, like, like])
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def _scan_decisions(
    conn: sqlite3.Connection, *, terms: list[str], since: int, limit: int,
) -> list[dict[str, Any]]:
    if not terms:
        return []
    try:
        sql = (
            "SELECT id, title, body, decided_at, status FROM decisions "
            "WHERE decided_at >= ? AND ("
            + " OR ".join(["title LIKE ? OR body LIKE ?"] * len(terms))
            + ") ORDER BY decided_at DESC LIMIT ?"
        )
        params: list[Any] = [since]
        for t in terms:
            like = f"%{t}%"
            params.extend([like, like])
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def _scan_open_loops(
    conn: sqlite3.Connection, *, terms: list[str], limit: int,
) -> list[dict[str, Any]]:
    if not terms:
        return []
    try:
        sql = (
            "SELECT id, kind, who, title, body_excerpt, created_at, status "
            "FROM open_loops WHERE status = 'open' AND ("
            + " OR ".join(["title LIKE ? OR body_excerpt LIKE ?"] * len(terms))
            + ") ORDER BY created_at DESC LIMIT ?"
        )
        params: list[Any] = []
        for t in terms:
            like = f"%{t}%"
            params.extend([like, like])
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []
