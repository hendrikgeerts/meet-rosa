"""Orchestrator-tools voor decision log."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import logging

from core.llm_helpers import llm_json_object
from core.query_safety import QUERY_SCHEMA, validate_query
from extensions.decisions.schema import (
    insert_decision, recent_decisions, search_decisions,
    update_decision_tags,
)

TZ = ZoneInfo("Europe/Amsterdam")


DECISIONS_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "log_decision",
        "description": (
            "Capture a decision the user wants to remember (vendor-keuze, "
            "scope-shift, hire decision, strategic direction). Use wanneer "
            "the user zegt 'leg vast dat...', 'noteer als beslissing dat...', "
            "of na een meeting waarin een concrete keuze viel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "1-line title (max 120 chars)"},
                "body": {"type": "string", "description": "Reasoning + context: waarom, alternatieven overwogen, betrokkenen"},
                "attendees": {"type": "array", "items": {"type": "string"},
                                "description": "Mensen betrokken bij de beslissing"},
                "source_ref": {"type": "string",
                                "description": "Bv. 'plaud:meeting:7' / 'gmail:thread:abc' / 'manual'"},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "find_decisions",
        "description": (
            "Search past decisions by free text in title or body. Use bij "
            "'waarom hadden we ook alweer X gekozen' / 'wat hebben we besloten "
            "over Y' / 'beslissingen rond project Z'. Query must be ≥3 chars "
            "without wildcards (%, _, *, ')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {**QUERY_SCHEMA},
                "days": {"type": "integer", "minimum": 1, "maximum": 1825, "default": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recent_decisions",
        "description": (
            "List decisions logged in the last N days, newest first. Use "
            "bij 'wat heb ik recent besloten' / 'beslissingen deze week'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 7},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
    },
]


_TAG_PROMPT = """Je krijgt een gelogde beslissing. Bepaal:
1. category: één van [strategy, hire, vendor, scope, ops, finance, other]
2. project_slugs: lijst van bekende project-slugs die hierbij betrokken zijn (uit de gegeven 'projects'-lijst). Lege lijst als geen match.

Output STRIKT als JSON-object: {"category": "...", "project_slugs": ["..."]}. Geen prose."""


def log_decision_handler(
    db_path: Path, args: dict[str, Any], *, ollama: Any = None,
) -> dict[str, Any]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        did = insert_decision(
            conn,
            title=str(args["title"])[:200],
            body=str(args["body"]),
            attendees=list(args.get("attendees") or []),
            source_ref=(args.get("source_ref") or "manual"),
        )
        # Optional: Llama auto-tag tegen bestaande projects
        tags: dict[str, Any] | None = None
        if ollama is not None:
            try:
                projects = [
                    {"slug": r[0], "title": r[1]}
                    for r in conn.execute(
                        "SELECT slug, title FROM projects WHERE status='active' LIMIT 50"
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                projects = []
            payload = (
                f"Decision title: {args['title']}\n"
                f"Decision body: {args['body']}\n\n"
                f"Active projects (slug + title):\n"
                + json.dumps(projects, ensure_ascii=False)
            )
            tags = llm_json_object(ollama, system=_TAG_PROMPT, user=payload)
            if tags:
                update_decision_tags(conn, did, tags)
    return {"ok": True, "decision_id": did, "tags": tags}


_log = logging.getLogger(__name__)


def find_decisions_handler(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = str(args.get("query") or "").strip()
    ok, err = validate_query(query)
    if not ok:
        _log.info("find_decisions rejected: %s", err)
        return []
    query = query.translate(str.maketrans("", "", "%_"))
    if not query:
        return []
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = search_decisions(
            conn, query=query,
            days=int(args.get("days", 365)),
            limit=int(args.get("limit", 10)),
        )
    return [_format(r) for r in rows]


def recent_decisions_handler(db_path: Path, args: dict[str, Any]) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        rows = recent_decisions(
            conn, days=int(args.get("days", 7)),
            limit=int(args.get("limit", 10)),
        )
    return [_format(r) for r in rows]


def _format(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "title": r["title"],
        "body": r["body"],
        "attendees": r["attendees"],
        "source_ref": r["source_ref"],
        "decided_at": datetime.fromtimestamp(r["decided_at"], TZ).isoformat(),
        "status": r["status"],
    }


DECISIONS_HANDLERS = {
    "log_decision": log_decision_handler,
    "find_decisions": find_decisions_handler,
    "recent_decisions": recent_decisions_handler,
}
