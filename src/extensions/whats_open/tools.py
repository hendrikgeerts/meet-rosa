"""whats_open orchestrator-tool — één call die alle openstaande
items aggregateert over mail/Slack/Plaud/reminders/Todoist."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from extensions.whats_open.aggregator import collect_whats_open


def _whats_open(
    db_path: Path, args: dict[str, Any], *,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
    user_handle: str | None = None,
) -> dict[str, Any]:
    raw_limit = args.get("per_section_limit", 5)
    try:
        limit = max(1, min(int(raw_limit), 20))
    except (TypeError, ValueError):
        limit = 5
    return collect_whats_open(
        db_path,
        todoist_client=todoist_client,
        todoist_project_id=todoist_project_id,
        per_section_limit=limit,
        user_handle=user_handle,
    )


WHATS_OPEN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "whats_open",
        "description": (
            "Geeft een geconsolideerd overzicht van ALLE openstaande items "
            "over kanalen: inkomende vragen/taken uit mail+Slack+Plaud, "
            "delegated wachtposten, onbeantwoorde mail/Slack-threads, "
            "pending reminders, Todoist (vandaag + overdue). Gebruik bij "
            "vragen als 'wat heb ik allemaal open', 'geef me een overzicht', "
            "'wat staat er nog te doen', 'wat is m'n status'. ÉÉN call hoeft "
            "i.p.v. 4-5 losse calls naar loops_open / comm_unanswered / "
            "list_reminders / todoist_list_open_tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "per_section_limit": {
                    "type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                    "description": "Max items per sectie in output.",
                },
            },
        },
    },
]


WHATS_OPEN_HANDLERS: dict[str, Any] = {
    "whats_open": _whats_open,
}
