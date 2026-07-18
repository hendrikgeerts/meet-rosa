"""Briefing-context generator voor de sales-sectie.

Wordt door core/briefings.collect_briefing_context aangeroepen om de
top-3 selectie + pipeline-snapshot in de JSON-context te zetten die
Claude meekrijgt.

H3 review-fix: gesplitst in pure compute_sales_pulse (read-only) +
record_briefing_served (mutate). Backward-compat wrapper
build_sales_pulse roept beide in volgorde aan, geschikt voor
production-briefing-flow. Tests + dashboard kunnen compute_*
gebruiken zonder triggers te 'verbruiken'.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .selection import mark_triggers_consumed, select_top_n


def _sales_schema_present(db_path: Path) -> bool:
    """Check of sales_accounts tabel bestaat. Voorkomt crashes bij
    one-shot scripts of fresh-install scenarios."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='sales_accounts' LIMIT 1"
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def compute_sales_pulse(
    db_path: Path,
) -> tuple[dict[str, Any], list[int]]:
    """PURE read — returnt (sales_pulse_dict, ids_of_triggers_used).

    Caller beslist of de triggers ook als 'serveer' moeten worden
    gemarkeerd via record_briefing_served. Voor dashboard-preview,
    test-runs of debug-scripts wil je dit NIET aanroepen.
    """
    if not _sales_schema_present(db_path):
        return {"top_three": [], "pipeline_snapshot": {}}, []

    selections = select_top_n(db_path, n=3)
    top_three: list[dict[str, Any]] = []
    trigger_ids: list[int] = []
    for s in selections:
        acc = s.account
        top_three.append({
            "id": acc.get("id"),
            "naam": acc.get("naam"),
            "target": acc.get("target"),
            "status": acc.get("status"),
            "reason": s.reason_text,
            "suggestion": s.suggestion,
        })
        if s.related_trigger_id:
            trigger_ids.append(s.related_trigger_id)

    pipeline_snapshot: dict[str, dict[str, int]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT target, status, COUNT(*) AS n "
            "FROM sales_accounts "
            "WHERE status NOT IN ('won','lost','snoozed') "
            "GROUP BY target, status"
        ).fetchall()
        for r in rows:
            pipeline_snapshot.setdefault(r["target"], {})[r["status"]] = int(r["n"])

    return {
        "top_three": top_three,
        "pipeline_snapshot": pipeline_snapshot,
    }, trigger_ids


def record_briefing_served(db_path: Path, trigger_ids: list[int]) -> None:
    """MUTATE — markeer dat de gegeven triggers in een briefing zijn
    verschenen. Aparte call zodat compute_sales_pulse puur read blijft.
    Idempotent — herhaalde call met zelfde id heeft geen effect."""
    mark_triggers_consumed(db_path, trigger_ids)


def build_sales_pulse(db_path: Path) -> dict[str, Any]:
    """Production-briefing-flow wrapper: compute + record in één call.
    Gebruikt door core.briefings.collect_briefing_context. Voor andere
    callers: gebruik compute_sales_pulse en bepaal zelf of triggers
    geconsumeerd moeten worden."""
    pulse, trigger_ids = compute_sales_pulse(db_path)
    record_briefing_served(db_path, trigger_ids)
    return pulse
