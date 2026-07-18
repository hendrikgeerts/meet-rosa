"""Weekly duplicate-scan — zaterdag samen met retro.

Loopt over pending reminders + open Todoist tasks, groepeert duplicaten
via de bestaande text-similariteit. Stuurt the user ALLEEN een bericht
als er hits zijn — geen "0 duplicaten"-noise.

Voorstellen worden hetzelfde formaat als todoist_cleanup: proposal_ids
die the user per batch bevestigt via een dedicated tool die reminders
én Todoist-tasks dicht kan sluiten.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from extensions.todoist_sync.cleanup import text_similarity

log = logging.getLogger(__name__)

# Iets soepeler dan Todoist-cleanup's 0.78/0.6 omdat we hier al twee
# sources cross-checken; kleine tekstvariaties (datum-woord verschil,
# emoji, hoofdlettergebruik) mogen echt niet als "nieuwe" wens tellen.
_SEQ_THRESHOLD = 0.65
_JACCARD_THRESHOLD = 0.55


def collect_duplicate_pairs(
    *, db_path: Path,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Scan alle pending reminders + open Todoist-tasks; returnt paren
    met hoge similariteit. Elk paar bevat één keeper (voorstel: de
    Todoist-taak als die er is, anders de oudste reminder) en één
    duplicaat-kandidaat om te sluiten.

    Output-formaat is een lijst met per-paar dict — makkelijk om als
    lijst naar the user te sturen zonder proposal-store in memory
    (deze scan is one-shot, geen apply-flow via cache).
    """
    items: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, body, remind_at FROM reminders "
                "WHERE sent_at IS NULL AND cancelled_at IS NULL "
                "ORDER BY remind_at DESC LIMIT 200",
            ).fetchall()
            for r in rows:
                items.append({
                    "source": "reminder",
                    "id": r["id"],
                    "body": r["body"] or "",
                    "sort_key": int(r["remind_at"] or 0),
                })
    except sqlite3.OperationalError:
        pass

    if todoist_client is not None:
        try:
            tasks = todoist_client.list_tasks(project_id=todoist_project_id)
            for t in tasks:
                items.append({
                    "source": "todoist",
                    "id": t.id,
                    "body": t.content or "",
                    "sort_key": 0,  # Todoist tasks winnen de tiebreak (below)
                })
        except Exception:
            log.exception("dup-scan: Todoist list_tasks failed")

    # O(n²) paar-comparison. Voor the user's volume (paar honderd items)
    # ruim voldoende.
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], tuple[str, Any]]] = set()
    for i, a in enumerate(items):
        if len(a["body"]) < 4:
            continue
        for b in items[i + 1:]:
            if len(b["body"]) < 4:
                continue
            key = tuple(sorted([
                (a["source"], str(a["id"])),
                (b["source"], str(b["id"])),
            ]))
            if key in seen:
                continue
            seq, jac = text_similarity(a["body"], b["body"])
            if seq >= _SEQ_THRESHOLD or jac >= _JACCARD_THRESHOLD:
                keeper, dup = _pick_keeper(a, b)
                pairs.append({
                    "keeper": {
                        "source": keeper["source"], "id": keeper["id"],
                        "body": keeper["body"][:120],
                    },
                    "duplicate": {
                        "source": dup["source"], "id": dup["id"],
                        "body": dup["body"][:120],
                    },
                    "seq_ratio": round(seq, 3),
                    "jaccard": round(jac, 3),
                })
                seen.add(key)
    return pairs


def _pick_keeper(a: dict[str, Any], b: dict[str, Any]) -> tuple[
    dict[str, Any], dict[str, Any],
]:
    """Preference voor keeper:
      1. Todoist wint van reminder (Todoist heeft due-date + persist).
      2. Bij beide-reminder: item met vroegste remind_at (dicht op nu).
    """
    if a["source"] == "todoist" and b["source"] != "todoist":
        return a, b
    if b["source"] == "todoist" and a["source"] != "todoist":
        return b, a
    # Beide reminders — pak degene met vroegste remind_at
    if int(a.get("sort_key") or 0) <= int(b.get("sort_key") or 0):
        return a, b
    return b, a
