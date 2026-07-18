"""User-profile loader + SYSTEM_PROMPT-renderer.

Centrale plek voor wie-is-the user. Bewuste yaml zodat hij 'em zelf
kan bewerken, of via chat ("ik ben echt slecht in X") via een update-
tool. Rosa krijgt het als een compacte sectie in SYSTEM_PROMPT zodat
ze persoon-aware antwoorden kan geven.

Privacy: profile-content gaat in SYSTEM_PROMPT mee naar Claude. the user
schrijft 'em zelf, dus consent expliciet. Het is gewone metadata, geen
mail-content.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def load_user_profile(path: Path) -> dict[str, Any]:
    """Laad het profiel; returnt leeg dict als file niet bestaat zodat
    Rosa gewoon zonder profile kan draaien."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            log.warning("user_profile: yaml is not a dict, ignoring")
            return {}
        return data
    except Exception:
        log.exception("user_profile: failed to load %s", path)
        return {}


def render_for_prompt(profile: dict[str, Any]) -> str:
    """Render het profile als een SYSTEM_PROMPT-sectie. Compact,
    bullets, no fluff. Skipt velden die leeg zijn."""
    if not profile:
        return ""

    lines: list[str] = ["About the user (use this to tailor your help):"]

    name = profile.get("name")
    role = profile.get("role")
    if name or role:
        bits = [b for b in (name, role) if b]
        lines.append(f"- Identity: {' — '.join(bits)}")

    companies = profile.get("companies") or []
    if companies:
        lines.append(f"- Companies: {', '.join(str(c) for c in companies)}")

    expertise = profile.get("expertise_areas") or []
    if expertise:
        lines.append(
            "- Strengths / expertise: "
            + ", ".join(str(e) for e in expertise)
        )

    growth = profile.get("growth_areas") or []
    if growth:
        lines.append(
            "- Growth areas / where to actively help: "
            + ", ".join(str(g) for g in growth)
        )

    style = profile.get("working_style")
    if style:
        lines.append(f"- Working style: {style}")

    comm = profile.get("communication_preferences")
    if comm:
        lines.append(f"- Communication preferences: {comm}")

    energy = profile.get("energy_patterns")
    if energy:
        lines.append(f"- Energy patterns: {energy}")

    goals = profile.get("goals") or []
    if goals:
        lines.append("- Current goals:")
        for g in goals[:5]:
            lines.append(f"  • {g}")

    notes = profile.get("notes")
    if notes:
        lines.append(f"- Notes: {notes}")

    # How to apply (instructie aan Rosa zelf)
    lines.append("")
    lines.append(
        "Use this profile to: (a) tailor tone — direct, "
        "no-disclaimers; (b) proactively offer help in growth areas, "
        "even unprompted (e.g. flag delegated items, surface "
        "stale follow-ups); (c) lean on strengths in framing "
        "advice; (d) refer to relevant company when context matches."
    )
    return "\n".join(lines)
