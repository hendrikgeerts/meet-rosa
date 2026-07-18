"""Orchestrator-tool: person_brief — vraag Rosa om een 1-page dossier
per persoon (uit alle bekende bronnen)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.llm_helpers import llm_short_text
from core.query_safety import QUERY_SCHEMA
from extensions.person_brief.lookup import build_person_brief
from integrations.gcal import CalendarClient

_SUMMARY_PROMPT = """Je krijgt aggregatie-data over één persoon (recente mails, meetings, open loops, komende agenda). Schrijf 1 zin (max 25 woorden) wat de huidige stand is met deze persoon. Praktisch, geen jargon, Engels.

Geen disclaimers, geen 'er zijn X mails'. Eén concrete inzicht-zin: 'Recently active on X — Y is pending response.'"""

PERSON_BRIEF_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "person_brief",
        "description": (
            "Build a 1-page dossier on a person from all available sources: "
            "VIP-list (config), recent mail/Slack interactions, Plaud "
            "meeting participations, open loops, upcoming calendar events. "
            "Use when the user asks 'wie is X', 'wat hebben we besproken met "
            "X', 'geef me een briefing over X', 'wat staat er met X open'. "
            "Match is fuzzy: pass naam OR email; aliases en VIP-emails "
            "worden meegezocht."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    **QUERY_SCHEMA,
                    "description": (
                        "Naam, email, of fragment (min 3 chars, geen "
                        "wildcards %/_/*/' — die worden afgewezen)"
                    ),
                },
                "days_back": {"type": "integer", "minimum": 7, "maximum": 365, "default": 90,
                               "description": "Hoever terug in mail/Slack zoeken"},
                "days_forward": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30,
                                  "description": "Hoever vooruit voor agenda-events"},
            },
            "required": ["query"],
        },
    },
]


def person_brief_handler(
    db_path: Path, args: dict[str, Any], *,
    calendar: CalendarClient, vip_path: Path,
    ollama: Any = None,
) -> dict[str, Any]:
    brief = build_person_brief(
        query=str(args["query"]),
        db_path=db_path, calendar=calendar, vip_path=vip_path,
        days_back=int(args.get("days_back", 90)),
        days_forward=int(args.get("days_forward", 30)),
    )
    # Optional: Llama 1-zin summary boven de raw aggregatie
    if ollama is not None and brief and not brief.get("error"):
        compact = {
            "name": brief.get("name") or args["query"],
            "recent_comm_count": len(brief.get("recent_communications") or []),
            "recent_subjects": [
                c.get("subject", "")[:80]
                for c in (brief.get("recent_communications") or [])[:3]
            ],
            "open_loops_count": len(brief.get("open_loops") or []),
            "open_loops_titles": [
                l.get("title", "")[:80]
                for l in (brief.get("open_loops") or [])[:3]
            ],
            "upcoming_event_count": len(brief.get("upcoming_events") or []),
            "next_event": ((brief.get("upcoming_events") or [{}])[0]
                            if brief.get("upcoming_events") else None),
        }
        summary = llm_short_text(
            ollama, system=_SUMMARY_PROMPT,
            user=json.dumps(compact, ensure_ascii=False, default=str),
        )
        if summary:
            brief["llm_summary"] = summary
    return brief


PERSON_BRIEF_HANDLERS = {"person_brief": person_brief_handler}
