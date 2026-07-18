"""Tools om het user_profile via iMessage te lezen/bewerken."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from core.perms import secure_file
from extensions.user_profile.profile import load_user_profile

log = logging.getLogger(__name__)

_LIST_FIELDS = frozenset({
    "companies", "expertise_areas", "growth_areas", "goals",
})
_SCALAR_FIELDS = frozenset({
    "name", "role", "working_style",
    "communication_preferences", "energy_patterns", "notes",
})
_ALL_FIELDS = _LIST_FIELDS | _SCALAR_FIELDS


def _save_profile(path: Path, profile: dict[str, Any]) -> None:
    """Atomic-ish save via temp-write. Review M8: forceer 0600 na
    replace zodat profile-content niet leesbaar is voor andere user-
    processes (`config/user_profile.yaml` bevat zelf-reflectie/PII)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=True,
                        default_flow_style=False)
    tmp.replace(path)
    secure_file(path)


def _user_profile_get(profile_path: Path, _args: dict[str, Any]) -> dict[str, Any]:
    return load_user_profile(profile_path)


def _user_profile_update(
    profile_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Update één veld van het profile.

    Voor list-velden: action='append'/'remove' om items toe te voegen of
    verwijderen (zonder de hele lijst kwijt te raken).
    Voor scalar-velden: action='set' (default) overschrijft.
    """
    field = str(args.get("field") or "").strip().lower()
    if field not in _ALL_FIELDS:
        return {"error": f"unknown field {field!r}. Allowed: {sorted(_ALL_FIELDS)}"}
    value = args.get("value")
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"error": "value required"}

    action = str(args.get("action") or "set").lower()
    profile = load_user_profile(profile_path)

    if field in _LIST_FIELDS:
        current = list(profile.get(field) or [])
        v = str(value).strip()
        if action in ("append", "add"):
            if v not in current:
                current.append(v)
        elif action == "remove":
            current = [x for x in current if x != v]
        else:
            # Review H7: action='set' op list-fields zou de hele lijst
            # wissen. Te ambigu — wijs af en dwing append/remove af.
            return {
                "error": (
                    f"action {action!r} not allowed for list-field "
                    f"{field!r}. Use 'append' to add an item or "
                    f"'remove' to drop one."
                ),
            }
        profile[field] = current
    else:
        if action != "set":
            return {"error": f"action {action!r} not supported for scalar field"}
        profile[field] = str(value).strip()

    _save_profile(profile_path, profile)
    return {
        "ok": True, "field": field, "action": action,
        "current_value": profile[field],
    }


USER_PROFILE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "user_profile_get",
        "description": (
            "Read the user's user-profile (name, role, companies, "
            "expertise, growth-areas, goals, working-style, "
            "communication preferences). Use when he asks 'wat weet je "
            "over mij', 'wat staat er in mijn profiel', or when you want "
            "to verify a stored field before answering."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "user_profile_update",
        "description": (
            "Update one field of the user's profile. Call when he says "
            "'voeg toe aan mijn profiel ...', 'ik ben goed in X' / "
            "'ik wil beter worden in Y', 'mijn werkstijl is ...'. "
            "List-fields (companies, expertise_areas, growth_areas, "
            "goals): action MUST be 'append' (add item) or 'remove' "
            "(drop item). 'set' is rejected for list-fields to prevent "
            "the user losing his entire list by accident. Scalar-fields "
            "(name, role, working_style, communication_preferences, "
            "energy_patterns, notes): use action='set'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": sorted(_ALL_FIELDS),
                },
                "value": {"type": "string", "minLength": 1, "maxLength": 1000},
                "action": {
                    "type": "string",
                    "enum": ["set", "append", "add", "remove"],
                    "default": "set",
                },
            },
            "required": ["field", "value"],
        },
    },
]


USER_PROFILE_HANDLERS: dict[str, Any] = {
    "user_profile_get": _user_profile_get,
    "user_profile_update": _user_profile_update,
}
