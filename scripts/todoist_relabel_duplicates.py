"""Eenmalige opruim: alle Rosa-Todoist-tasks die nog zonder [DD mmm]-prefix
in de titel staan, hernoemen via update_task.

Achtergrond: tot voor deze fix werden recurring reminders (8× dezelfde
content voor 8 maanden) zonder visueel datum-onderscheid naar Todoist
gepushed. you zag ze als duplicates omdat Todoist's lijst-view de
due-datum niet prominent toont.

Sinds de fix bevatten nieuwe pushes een "[DD mmm] " prefix. Dit script
update bestaande open links retro-actief.

Usage:
    PYTHONPATH=src ./venv/bin/python scripts/todoist_relabel_duplicates.py --dry-run
    PYTHONPATH=src ./venv/bin/python scripts/todoist_relabel_duplicates.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from extensions.todoist_sync.sync import _with_date_prefix
from integrations.todoist import TodoistClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = args.apply

    settings = load_settings()
    if not settings.todoist_api_token:
        print("no token", file=sys.stderr)
        return 1
    client = TodoistClient(settings.todoist_api_token)
    projects = client.list_projects()
    rosa = next((p for p in projects
                  if p.name == settings.todoist_project_name), None)
    if rosa is None:
        print(f"project not found: {settings.todoist_project_name}", file=sys.stderr)
        return 1
    tasks = client.list_tasks(project_id=rosa.id)
    print(f"{len(tasks)} open tasks in '{rosa.name}'")

    # Bouw mapping: todoist_id → due_at uit links + reminders/loops
    with sqlite3.connect(settings.db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT l.todoist_id, l.local_kind, l.local_id,
                       r.remind_at as reminder_due,
                       lo.due_at as loop_due
                 FROM todoist_links l
                 LEFT JOIN reminders r
                   ON l.local_kind='reminder' AND l.local_id=r.id
                 LEFT JOIN open_loops lo
                   ON l.local_kind='open_loop' AND l.local_id=lo.id
                WHERE l.completed_at_remote IS NULL"""
        ).fetchall()
    due_by_tid: dict[str, int | None] = {}
    for r in rows:
        due_by_tid[r["todoist_id"]] = r["reminder_due"] or r["loop_due"]

    updates: list[tuple[str, str, str]] = []  # (id, old, new)
    for t in tasks:
        if t.content.startswith("[") and "] " in t.content[:16]:
            continue  # al geprefixt
        due = due_by_tid.get(t.id)
        if not due:
            continue  # geen due → geen prefix mogelijk
        new = _with_date_prefix(t.content, int(due))
        if new == t.content:
            continue
        updates.append((t.id, t.content, new))

    print(f"\n{len(updates)} tasks krijgen een nieuwe titel:")
    for tid, old, new in updates[:20]:
        print(f"  {tid}\n    OLD: {old[:80]}\n    NEW: {new[:80]}")

    if not apply:
        print(f"\n[dry-run] run met --apply om {len(updates)} updates door te voeren")
        return 0

    succeeded = 0
    for tid, _old, new in updates:
        if client.update_task(tid, content=new):
            succeeded += 1
    print(f"\napplied: {succeeded}/{len(updates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
