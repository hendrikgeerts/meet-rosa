"""Duplicate-detection over reminders + Todoist voor set_reminder-pad.

Reused: `text_similarity` uit todoist_sync/cleanup.py — dezelfde
SequenceMatcher + token-Jaccard heuristiek. Threshold defaults iets
strenger dan Todoist-cleanup omdat we hier PROACTIEF weigeren, niet
reactief opruimen.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from extensions.todoist_sync.cleanup import text_similarity

log = logging.getLogger(__name__)

_SEQ_THRESHOLD = 0.72
_JACCARD_THRESHOLD = 0.55


def find_similar(
    *, db_path: Path,
    handle: str | None,
    new_body: str,
    todoist_client: Any = None,
    todoist_project_id: str | None = None,
    max_hits: int = 5,
) -> list[dict[str, Any]]:
    """Vind pending reminders + open Todoist-tasks die semantisch lijken
    op `new_body`. Returnt max_hits kandidaten met similariteit-score.

    Bronnen:
      - reminders WHERE sent_at IS NULL AND cancelled_at IS NULL
      - Todoist open tasks in het geconfigureerde project
    """
    hits: list[dict[str, Any]] = []
    body = (new_body or "").strip()
    if len(body) < 4:
        return []

    # --- pending reminders ---
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            params: list[Any] = []
            sql = ("SELECT id, body, remind_at FROM reminders "
                   "WHERE sent_at IS NULL AND cancelled_at IS NULL")
            if handle:
                sql += " AND handle = ?"
                params.append(handle)
            sql += " ORDER BY remind_at DESC LIMIT 200"
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                seq, jac = text_similarity(body, r["body"] or "")
                if seq >= _SEQ_THRESHOLD or jac >= _JACCARD_THRESHOLD:
                    hits.append({
                        "source": "reminder",
                        "id": r["id"],
                        "body": r["body"],
                        "remind_at": r["remind_at"],
                        "seq_ratio": round(seq, 3),
                        "jaccard": round(jac, 3),
                    })
    except sqlite3.OperationalError:
        pass

    # --- Todoist open tasks ---
    if todoist_client is not None:
        try:
            tasks = todoist_client.list_tasks(project_id=todoist_project_id)
        except Exception:
            log.exception("dedup: Todoist list_tasks failed")
            tasks = []
        for t in tasks:
            seq, jac = text_similarity(body, t.content or "")
            if seq >= _SEQ_THRESHOLD or jac >= _JACCARD_THRESHOLD:
                hits.append({
                    "source": "todoist",
                    "id": t.id,
                    "body": t.content,
                    "due_date": t.due_date,
                    "due_datetime": t.due_datetime,
                    "labels": t.labels,
                    "seq_ratio": round(seq, 3),
                    "jaccard": round(jac, 3),
                })

    hits.sort(
        key=lambda h: max(h["seq_ratio"], h["jaccard"]),
        reverse=True,
    )
    return hits[:max_hits]
