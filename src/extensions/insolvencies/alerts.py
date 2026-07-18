"""iMessage-alert format voor matched insolventies.

Per-layer indicatie wat de trigger was, zodat the user direct kan
beoordelen waarom dit bij hem terecht kwam.
"""
from __future__ import annotations

from .feed import InsolvencyItem
from .matcher import MatchResult


def _status_emoji(status: str | None) -> str:
    s = (status or "").lower()
    if "fail" in s:
        return "🔴"
    if "surse" in s:
        return "🟠"
    if "wsnp" in s or "schuldsanering" in s:
        return "🟡"
    return "⚠️"


def _trigger_summary(result: MatchResult) -> str:
    """Per-laag accuraat — gebruikt per_layer_terms zodat label en term
    bij elkaar horen (zelfde aanpak als tenders M2)."""
    labels = {
        "watchlist": "watchlist-KvK",
        "activity":  "activiteit",
        "name":      "naam",
    }
    parts: list[str] = []
    for layer, terms in result.per_layer_terms:
        label = labels.get(layer, layer)
        if terms:
            parts.append(f"{label}={terms[0]}")
        else:
            parts.append(label)
    return " · ".join(parts) if parts else "filter-match"


def format_alert(item: InsolvencyItem, result: MatchResult) -> str:
    """Renderen voor iMessage. Geen markdown."""
    emoji = _status_emoji(item.status)
    head = f"{emoji} {item.status or 'Insolventie'}: {item.naam}"

    plaats_line = item.plaats or "-"
    if item.provincie:
        plaats_line = f"{plaats_line} ({item.provincie})"
    if item.kvk:
        plaats_line = f"{plaats_line} · KvK {item.kvk}"

    lines = [head, f"Plaats: {plaats_line}"]
    if item.hoofd_activiteit:
        # Clip lange omschrijvingen — past in iMessage zonder afkappen
        act = item.hoofd_activiteit
        if len(act) > 160:
            act = act[:157] + "..."
        lines.append(f"Activiteit: {act}")
    if item.curator:
        lines.append(f"Curator: {item.curator}")
    lines.append(f"Trigger: {_trigger_summary(result)}")
    lines.append(item.link)
    return "\n".join(lines)
