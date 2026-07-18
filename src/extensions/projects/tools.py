"""Orchestrator-tools voor project-tracker.

- project_list — actieve initiatives + status
- project_create — nieuw project
- project_update — partial edit (status, deadline, owner, keywords...)
- project_close — shorthand: status → done/abandoned
- project_status — aggregator: project + recent comm + decisions + loops + events
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.projects.aggregator import project_status as _project_status
from extensions.projects.schema import (
    VALID_STATUS,
    delete_project,
    get_project,
    insert_project,
    list_projects,
    update_project,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


PROJECT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "project_list",
        "description": (
            "List projects (active initiatives). Filter by status (default: "
            "all) or company tag. Use bij 'wat zijn mijn projecten', 'welke "
            "DST-projecten lopen', 'paused projecten'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(VALID_STATUS)},
                "company": {"type": "string"},
            },
        },
    },
    {
        "name": "project_create",
        "description": (
            "Create a new project. Use bij 'leg vast als project', 'start "
            "een project voor X', 'maak project Y aan'. Slug moet uniek "
            "(letters + cijfers + dashes); wordt afgeleid van title als "
            "the user er geen geeft. Keywords helpen het project linken aan "
            "comm/decisions/loops — bv. ['rosa','pa-agent']."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "slug": {"type": "string"},
                "description": {"type": "string"},
                "company": {"type": "string"},
                "owner": {"type": "string"},
                "status": {"type": "string", "enum": list(VALID_STATUS)},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "deadline": {"type": "string",
                              "description": "ISO date YYYY-MM-DD of YYYY-MM-DDTHH:MM"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "project_update",
        "description": (
            "Partial update van een project (slug of id). Use bij 'project "
            "X is af' (status=done), 'pause project Y', 'verschuif deadline "
            "van Z naar 31 mei'. Pass alleen de velden die wijzigen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "company": {"type": "string"},
                "owner": {"type": "string"},
                "status": {"type": "string", "enum": list(VALID_STATUS)},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "deadline": {"type": "string",
                              "description": "ISO date; lege string = clear"},
            },
        },
    },
    {
        "name": "project_status",
        "description": (
            "Show full status van één project: gekoppelde recente mails/"
            "Slack-items, beslissingen, open loops, en komende calendar-"
            "events. Use bij 'hoe staat project X ervoor', 'status van Y', "
            "'wat speelt er rond Z'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "days_back": {"type": "integer", "minimum": 1, "maximum": 180,
                               "default": 30},
            },
            "required": ["slug"],
        },
    },
]


def project_list_handler(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = list_projects(
            conn, status=(args.get("status") or None),
            company=(args.get("company") or None),
        )
    return [_format(r) for r in rows]


def project_create_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title", "")).strip()
    if not title:
        return {"error": "title required"}
    slug = (args.get("slug") or "").strip() or _slugify(title)
    if not slug:
        return {"error": "could not derive slug from title"}
    deadline_at = _parse_deadline(args.get("deadline"))
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        try:
            pid = insert_project(
                conn,
                slug=slug, title=title,
                description=(args.get("description") or None),
                company=(args.get("company") or None),
                owner=(args.get("owner") or None),
                status=(args.get("status") or "active"),
                keywords=list(args.get("keywords") or []),
                deadline_at=deadline_at,
            )
        except sqlite3.IntegrityError:
            return {"error": f"slug already exists: {slug}"}
        except ValueError as e:
            return {"error": str(e)}
        proj = get_project(conn, project_id=pid)
    return {"ok": True, "project": _format(proj) if proj else None}


def project_update_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        proj = _resolve_project(conn, args)
        if not proj:
            return {"error": "project not found"}
        try:
            deadline_raw = args.get("deadline")
            clear_deadline = isinstance(deadline_raw, str) and deadline_raw.strip() == ""
            deadline_at = (None if clear_deadline
                            else _parse_deadline(deadline_raw))
            ok = update_project(
                conn,
                project_id=proj["id"],
                title=(args.get("title") or None),
                description=(args.get("description") or None),
                company=(args.get("company") or None),
                owner=(args.get("owner") or None),
                status=(args.get("status") or None),
                keywords=(list(args["keywords"]) if "keywords" in args else None),
                deadline_at=deadline_at,
                clear_deadline=clear_deadline,
            )
        except ValueError as e:
            return {"error": str(e)}
        if not ok:
            return {"ok": False, "note": "no fields changed"}
        updated = get_project(conn, project_id=proj["id"])
    return {"ok": True, "project": _format(updated) if updated else None}


def project_status_handler(
    db_path: Path, args: dict[str, Any], *, calendar: Any = None,
) -> dict[str, Any]:
    slug = str(args.get("slug", "")).strip()
    if not slug:
        return {"error": "slug required"}
    result = _project_status(
        db_path, slug=slug,
        days_back=int(args.get("days_back", 30)),
        calendar=calendar,
    )
    if result.get("project"):
        result["project"] = _format(result["project"])
    return result


def project_delete_handler(db_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    """Niet als orchestrator-tool blootgesteld (te makkelijk fout te maken
    via natural language). Wel beschikbaar voor het dashboard."""
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        proj = _resolve_project(conn, args)
        if not proj:
            return {"error": "project not found"}
        ok = delete_project(conn, proj["id"])
    return {"ok": ok}


def _resolve_project(
    conn: sqlite3.Connection, args: dict[str, Any],
) -> dict[str, Any] | None:
    project_id = args.get("id")
    slug = args.get("slug")
    if project_id:
        return get_project(conn, project_id=int(project_id))
    if slug:
        return get_project(conn, slug=str(slug))
    return None


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60]


def _parse_deadline(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _format(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    if r.get("deadline_at"):
        out["deadline"] = datetime.fromtimestamp(r["deadline_at"], TZ).date().isoformat()
    out["created"] = datetime.fromtimestamp(r["created_at"], TZ).date().isoformat()
    out["updated"] = datetime.fromtimestamp(r["updated_at"], TZ).date().isoformat()
    return out


PROJECT_HANDLERS = {
    "project_list": project_list_handler,
    "project_create": project_create_handler,
    "project_update": project_update_handler,
    "project_status": project_status_handler,
}
