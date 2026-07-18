"""Orchestrator-tool: upcoming_birthdays."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from extensions.birthdays.tracker import list_upcoming

BIRTHDAY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "upcoming_birthdays",
        "description": (
            "List birthdays + jubilea + organisatie-anniversaries vanuit "
            "vip_contacts.yaml binnen de komende N dagen. Use bij vragen "
            "als 'wie is er deze week jarig', 'staan er nog jubilea aan', "
            "'wat zijn de relatie-momenten deze maand'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 0, "maximum": 365, "default": 14},
            },
        },
    },
]


def upcoming_birthdays_handler(
    db_path: Path, args: dict[str, Any], *, vip_path: Path,
) -> list[dict[str, Any]]:
    return list_upcoming(vip_path, days_forward=int(args.get("days", 14)))


BIRTHDAY_HANDLERS = {"upcoming_birthdays": upcoming_birthdays_handler}
