"""Orchestrator-tools voor OKR-engine.

- okrs_list — snelle inventaris van actieve objectieven + voortgang
- okrs_check — gateway-call die een vrij voorstel toetst aan elk actief
  objectief (geeft per-objective alignment score 0-10 + 1-zin rationale).
  Bedoeld om beslissingen / kalender-conflicten / nieuwe ideeën snel te
  spiegelen aan kwartaal-doelen.
- okrs_update_progress — handmatige `current`-update voor één KR.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from extensions.okrs.loader import (
    load_okrs, to_briefing_snapshot, update_kr_progress,
)
from privacy.gateway import Gateway

log = logging.getLogger(__name__)


OKR_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "okrs_list",
        "description": (
            "List active OKRs (kwartaal-objectieven) met voortgang per "
            "key-result. Use bij 'wat zijn mijn doelen', 'hoe sta ik ervoor', "
            "'OKR-stand'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string",
                            "description": "Optioneel: filter op company-tag (bv. 'DST', 'HGE')"},
            },
        },
    },
    {
        "name": "okrs_check",
        "description": (
            "Score a proposal/decision/idea against each active OKR. "
            "Use wanneer the user twijfelt of iets de moeite waard is "
            "('moet ik vrijdag naar die conferentie', 'is deze klant de "
            "moeite waard', 'doe ik dit project of niet'). Returns per "
            "objective: alignment score 0-10 + 1-zin rationale + recommend "
            "(go / skip / discuss)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal": {"type": "string",
                             "description": "Het voorstel/de keuze in vrije tekst (1-3 zinnen)."},
            },
            "required": ["proposal"],
        },
    },
    {
        "name": "okrs_update_progress",
        "description": (
            "Update de huidige waarde van een key-result. Use bij 'noteer "
            "dat we nu op €40K ARR zitten' of 'we hebben er 3 enterprise-"
            "klanten bij — update'. Wijzigt config/okrs.yaml direct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "objective_id": {"type": "string",
                                  "description": "Bv. 'dst-arr' — uit okrs_list"},
                "kr_id": {"type": "string",
                          "description": "Bv. 'kr1' — uit okrs_list"},
                "current": {"type": "number",
                             "description": "Nieuwe huidige waarde (in unit van het KR)"},
            },
            "required": ["objective_id", "kr_id", "current"],
        },
    },
]


def okrs_list_handler(yaml_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    period = load_okrs(yaml_path)
    if period is None:
        return {"period": None, "objectives": [],
                "note": "okrs.yaml ontbreekt — copy okrs.example.yaml en vul je doelen in."}
    snapshot = to_briefing_snapshot(period)
    company_filter = (args.get("company") or "").strip()
    if company_filter:
        snapshot = [o for o in snapshot if (o.get("company") or "").lower()
                    == company_filter.lower()]
    return {
        "period": period.period,
        "period_start": period.period_start,
        "period_end": period.period_end,
        "objectives": snapshot,
    }


_CHECK_SYSTEM = """You are Rosa's OKR-checker. the user geeft een voorstel/idee/keuze. Jouw taak: per actief objectief 1) score 0-10 hoeveel het voorstel het objectief vooruit helpt, 2) 1 korte zin waarom, 3) recommend "go" / "skip" / "discuss".

Score-rubriek:
- 9-10: dit voorstel is direct werk aan dit objectief
- 6-8: helpt indirect (relatie, leerervaring, klant-pijplijn)
- 3-5: neutraal/zwak verband
- 0-2: trekt energie wég van dit objectief

Output STRIKT als JSON-array, één object per objectief. Niets eromheen — geen prose, geen markdown.

Format per item: {"objective_id": "...", "score": 0-10, "rationale": "...", "recommend": "go|skip|discuss"}

Aan het eind: kies de hoogste score. Als max_score >= 7 → overall = "go". Als alle scores <= 3 → "skip". Anders → "discuss". Voeg dat als laatste array-item toe: {"overall": "go|skip|discuss", "max_score": N}."""


def okrs_check_handler(
    yaml_path: Path, args: dict[str, Any], *, gateway: Gateway,
    settings: Any | None = None,
) -> dict[str, Any]:
    proposal = str(args.get("proposal", "")).strip()
    if not proposal:
        return {"error": "proposal required"}
    period = load_okrs(yaml_path)
    if period is None or not period.active():
        return {"note": "Geen actieve OKRs — okrs.yaml ontbreekt of leeg."}

    objectives_compact = [
        {
            "id": o.id, "title": o.title, "company": o.company,
            "why": o.why,
            "key_results": [{"text": kr.text, "progress_pct": kr.progress_pct}
                            for kr in o.key_results],
        }
        for o in period.active()
    ]

    user_payload = (
        "Actieve OKRs (JSON):\n"
        + json.dumps(objectives_compact, ensure_ascii=False, indent=2)
        + f"\n\nVoorstel:\n{proposal}\n\nGeef de JSON-array zoals beschreven."
    )
    system = _CHECK_SYSTEM
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    response = gateway.complete(
        task="okrs_check",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=800,
    )
    text = "".join(b.text for b in response.content
                   if getattr(b, "type", None) == "text").strip()
    parsed = _try_parse_json_array(text)
    if parsed is None:
        return {"error": "Claude returned non-JSON", "raw": text[:500]}
    return {"proposal": proposal, "scores": parsed}


def okrs_update_progress_handler(
    yaml_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    objective_id = str(args["objective_id"])
    kr_id = str(args["kr_id"])
    current = float(args["current"])
    ok = update_kr_progress(
        yaml_path,
        objective_id=objective_id, kr_id=kr_id, current=current,
    )
    if not ok:
        return {"ok": False, "error": f"objective={objective_id} / kr={kr_id} not found"}
    period = load_okrs(yaml_path)
    obj = period.find(objective_id) if period else None
    kr = next((k for k in (obj.key_results if obj else []) if k.id == kr_id), None)
    return {
        "ok": True,
        "objective": obj.title if obj else None,
        "kr": kr.text if kr else None,
        "current": current,
        "progress_pct": kr.progress_pct if kr else None,
    }


def _try_parse_json_array(text: str) -> list[Any] | None:
    """Strip optionele markdown-fence + prose, vind eerste [...] blok, parse."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        result = json.loads(text[start:end + 1])
        return result if isinstance(result, list) else None
    except json.JSONDecodeError:
        return None


OKR_HANDLERS = {
    "okrs_list": okrs_list_handler,
    "okrs_check": okrs_check_handler,
    "okrs_update_progress": okrs_update_progress_handler,
}
