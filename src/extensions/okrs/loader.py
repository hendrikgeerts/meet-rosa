"""OKR-loader — pure read-only over config/okrs.yaml.

Yaml is bron-van-waarheid (kwartaal-doelen wijzigen zelden, git-traceable,
en Claude leest ze direct in briefing/dayclose-context). Geen DB-overlay
voor de OKRs zelf; voortgangs-updates editen het yaml-bestand via
`update_kr_progress()` (of the user bewerkt het file zelf).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeyResult:
    id: str
    text: str
    target: float
    unit: str
    current: float

    @property
    def progress_pct(self) -> int:
        if self.target <= 0:
            return 0
        return max(0, min(100, int(round(self.current / self.target * 100))))


@dataclass(frozen=True)
class Objective:
    id: str
    title: str
    why: str
    status: str
    company: str | None
    key_results: list[KeyResult] = field(default_factory=list)

    @property
    def avg_progress_pct(self) -> int:
        if not self.key_results:
            return 0
        return int(round(sum(kr.progress_pct for kr in self.key_results) / len(self.key_results)))


@dataclass(frozen=True)
class OkrPeriod:
    period: str
    period_start: str
    period_end: str
    objectives: list[Objective] = field(default_factory=list)

    def active(self) -> list[Objective]:
        return [o for o in self.objectives if o.status == "active"]

    def find(self, objective_id: str) -> Objective | None:
        for o in self.objectives:
            if o.id == objective_id:
                return o
        return None


def load_okrs(yaml_path: Path) -> OkrPeriod | None:
    """Load + parse okrs.yaml. Returns None if file missing or empty."""
    if not yaml_path.exists():
        return None
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.exception("okrs.yaml parse failed")
        return None

    objectives: list[Objective] = []
    for o in data.get("objectives") or []:
        if not isinstance(o, dict) or not o.get("id") or not o.get("title"):
            continue
        krs: list[KeyResult] = []
        for kr in o.get("key_results") or []:
            if not isinstance(kr, dict) or not kr.get("id"):
                continue
            try:
                krs.append(KeyResult(
                    id=str(kr["id"]),
                    text=str(kr.get("text", "")),
                    target=float(kr.get("target", 0)),
                    unit=str(kr.get("unit", "")),
                    current=float(kr.get("current", 0)),
                ))
            except (TypeError, ValueError):
                log.warning("okrs.yaml: skip KR with bad numeric: %r", kr)
        objectives.append(Objective(
            id=str(o["id"]),
            title=str(o["title"]),
            why=str(o.get("why", "")),
            status=str(o.get("status", "active")),
            company=(str(o["company"]) if o.get("company") else None),
            key_results=krs,
        ))

    return OkrPeriod(
        period=str(data.get("period", "")),
        period_start=str(data.get("period_start", "")),
        period_end=str(data.get("period_end", "")),
        objectives=objectives,
    )


def update_kr_progress(
    yaml_path: Path, *, objective_id: str, kr_id: str, current: float,
) -> bool:
    """In-place update van `current`-waarde voor één KR. Behoudt yaml-comments
    NIET (PyYAML round-trip is niet comment-aware) — bedoeld voor occasional
    voortgangs-updates vanuit iMessage. the user kan zelf het yaml editen voor
    structurele wijzigingen."""
    if not yaml_path.exists():
        return False
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    found = False
    for o in data.get("objectives") or []:
        if o.get("id") != objective_id:
            continue
        for kr in o.get("key_results") or []:
            if kr.get("id") == kr_id:
                kr["current"] = current
                found = True
                break
    if not found:
        return False
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return True


def to_briefing_snapshot(period: OkrPeriod | None) -> list[dict[str, Any]]:
    """Compact representation voor briefing/dayclose context — actief
    objectief + per-KR progress in 1 regel. Claude leest deze als achtergrond
    om beslissingen op te kunnen reflecteren ('je doel is X, dit raakt dat')."""
    if period is None:
        return []
    out: list[dict[str, Any]] = []
    for o in period.active():
        out.append({
            "id": o.id,
            "title": o.title,
            "company": o.company,
            "avg_progress_pct": o.avg_progress_pct,
            "key_results": [
                {
                    "id": kr.id, "text": kr.text,
                    "current": kr.current, "target": kr.target,
                    "unit": kr.unit, "progress_pct": kr.progress_pct,
                }
                for kr in o.key_results
            ],
        })
    return out
