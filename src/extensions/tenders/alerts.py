"""iMessage-alert format voor matched TenderNed-publicaties.

Vriendelijk, kort, scanbaar. Toont de aanleiding ("WHY dit binnenkomt")
zodat the user direct kan beslissen of het relevant is.
"""
from __future__ import annotations

from typing import Any

from .feed import overview_url
from .matcher import MatchResult

NL_MONTHS = ["", "jan", "feb", "mrt", "apr", "mei", "jun",
             "jul", "aug", "sep", "okt", "nov", "dec"]


def _fmt_date(iso: str | None) -> str:
    """'2026-05-11T10:00:00' → '11 mei 2026 10:00'. Lege/ongeldig → '-'."""
    if not iso:
        return "-"
    s = str(iso)
    try:
        date_part = s.split("T")[0]
        y, m, d = (int(x) for x in date_part.split("-"))
        time_part = ""
        if "T" in s:
            t = s.split("T", 1)[1]
            hh, mm = t.split(":")[0], t.split(":")[1]
            time_part = f" {int(hh):02d}:{int(mm):02d}"
        return f"{d} {NL_MONTHS[m]} {y}{time_part}"
    except (ValueError, IndexError):
        return s[:16]


def _trigger_summary(result: MatchResult) -> str:
    """Korte one-line uitleg waarom dit door de filter kwam.

    M2 fix: gebruikt `per_layer_terms` zodat elke layer-label hangt aan
    een term die DAADWERKELIJK in die laag triggerde — niet een
    willekeurige uit de cross-layer dedupe-lijst.
    """
    layer_labels = {
        "trefwoord": "trefwoord",
        "cpv_code":  "CPV-code",
        "cpv_desc":  "CPV-omschrijving",
        "keyword":   "titel/tekst",
    }
    parts: list[str] = []
    for layer, terms in result.per_layer_terms:
        label = layer_labels.get(layer, layer)
        if terms:
            parts.append(f"{label}={terms[0]}")
        else:
            parts.append(label)
    return " · ".join(parts) if parts else "filter-match"


def format_alert(
    detail: dict[str, Any], result: MatchResult,
) -> str:
    """Bouw iMessage-tekst voor één matched publicatie.

    Format (geen markdown, scant snel):

        🏛 Nieuwe aanbesteding
        Narrowcastingoplossing (SaaS) — ROC van Amsterdam
        Sluiting: 11 mei 2026 10:00
        Trigger: trefwoord=Narrowcasting · CPV=32322000
        https://www.tenderned.nl/aankondigingen/overzicht/419614
    """
    title = (detail.get("aanbestedingNaam") or "(geen titel)").strip()
    org = (detail.get("opdrachtgeverNaam") or "").strip()
    sluiting = _fmt_date(detail.get("sluitingsDatum"))
    pub_id = int(detail.get("publicatieId") or 0)
    aankondiging_type = ""
    aank_code = detail.get("aankondigingCode")
    if isinstance(aank_code, dict):
        aankondiging_type = aank_code.get("omschrijving", "")

    trigger = _trigger_summary(result)
    url = overview_url(pub_id)

    head = "🏛 Nieuwe aanbesteding"
    if aankondiging_type and aankondiging_type.lower() != "publicatie":
        head = f"🏛 {aankondiging_type}"

    line2 = title
    if org:
        line2 = f"{title} — {org}"

    lines = [
        head,
        line2,
        f"Sluiting: {sluiting}",
        f"Trigger:  {trigger}",
        url,
    ]
    return "\n".join(lines)
