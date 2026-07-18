"""Birthday + jubilea tracker — pure read-only over vip_contacts.yaml.

Berekent per VIP-entry of vandaag + de komende N dagen een verjaardag of
jubileum valt. Geen DB-state nodig — alles uit yaml.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _next_anniversary(base_date: date, today: date) -> date:
    """Eerstvolgende keer dat dag+maand van base_date plaatsvindt vanaf today."""
    try:
        candidate = base_date.replace(year=today.year)
    except ValueError:
        # 29 februari in non-schrikkeljaar — verschuif naar 1 maart
        candidate = date(today.year, 3, 1)
    if candidate < today:
        try:
            candidate = base_date.replace(year=today.year + 1)
        except ValueError:
            candidate = date(today.year + 1, 3, 1)
    return candidate


def list_upcoming(
    vip_path: Path, *, days_forward: int = 14, today: date | None = None,
) -> list[dict[str, Any]]:
    """Returns birthdays + jubilea binnen [today, today+days_forward].
    Sorted op datum."""
    today = today or date.today()
    horizon = today + timedelta(days=days_forward)

    if not vip_path.exists():
        return []
    cfg = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}

    out: list[dict[str, Any]] = []
    for p in cfg.get("people") or []:
        name = p.get("name") or "(onbekend)"
        # Birthday
        bday = _parse_date(p.get("birthday"))
        if bday:
            next_ = _next_anniversary(bday, today)
            if next_ <= horizon:
                turning = next_.year - bday.year
                out.append({
                    "name": name,
                    "kind": "birthday",
                    "next_date": next_.isoformat(),
                    "days_until": (next_ - today).days,
                    "turning_age": turning,
                    "tier": p.get("tier"),
                    "relationship": p.get("relationship"),
                    "communication_style": p.get("communication_style"),
                })
        # Jubilea
        for j in (p.get("jubilea") or []):
            jdate = _parse_date(j.get("date"))
            if not jdate:
                continue
            next_ = _next_anniversary(jdate, today)
            if next_ <= horizon:
                years = next_.year - jdate.year
                out.append({
                    "name": name,
                    "kind": "jubileum",
                    "label": j.get("label") or "jubileum",
                    "next_date": next_.isoformat(),
                    "days_until": (next_ - today).days,
                    "years": years,
                    "tier": p.get("tier"),
                })

    # Ook organisaties kunnen oprichtingsdata hebben.
    for o in cfg.get("organizations") or []:
        founded = _parse_date(o.get("founded"))
        if not founded:
            continue
        next_ = _next_anniversary(founded, today)
        if next_ <= horizon:
            out.append({
                "name": o.get("name") or "(org)",
                "kind": "org_anniversary",
                "label": "Founded anniversary",
                "next_date": next_.isoformat(),
                "days_until": (next_ - today).days,
                "years": next_.year - founded.year,
                "tier": o.get("tier"),
            })

    out.sort(key=lambda x: x["days_until"])
    return out


def describe_today(vip_path: Path, *, today: date | None = None) -> str | None:
    """Compacte regel voor in briefings: 'Vandaag jarig: X (40), Y (35)'.
    Returns None als er niets is — caller laat sectie dan weg."""
    items = list_upcoming(vip_path, days_forward=0, today=today)
    if not items:
        return None
    parts: list[str] = []
    for it in items:
        if it["kind"] == "birthday":
            parts.append(f"🎂 {it['name']} (wordt {it['turning_age']})")
        elif it["kind"] == "jubileum":
            parts.append(f"🏆 {it['name']} — {it['label']} ({it['years']}y)")
        else:
            parts.append(f"🏢 {it['name']} — {it['label']} ({it['years']}y)")
    return "Vandaag: " + ", ".join(parts)
