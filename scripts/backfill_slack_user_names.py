"""Backfill: vervang raw Slack user-IDs (U…/B…) in `comm_items.from_addr`
en `open_loops.who` door geresolveerde namen via de Slack users_list-API.

Gebruik:
    PYTHONPATH=src ./venv/bin/python scripts/backfill_slack_user_names.py --dry-run
    PYTHONPATH=src ./venv/bin/python scripts/backfill_slack_user_names.py --commit

Per workspace: één API-call (users_list paginated) → cache. Dan
bulk-UPDATE. Onbekende IDs (deactivated users, externe guests die niet
in users_list staan) blijven raw — geen kwaad, betere optie dan een
verkeerde naam.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.config import load_settings
from integrations.slack import all_enabled, load_workspaces


_SLACK_ID_RE = re.compile(r"^[UB][0-9A-Z]+$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    commit = bool(args.commit) and not args.dry_run

    settings = load_settings()
    yaml_path = ROOT / "config" / "slack_workspaces.yaml"

    # Verzamel naam-mapping over alle enabled workspaces. Eén Slack-ID is
    # workspace-uniek, dus we kunnen flat mappen.
    id_to_name: dict[str, str] = {}
    for workspace, token in all_enabled(yaml_path):
        from integrations.slack import SlackClient
        client = SlackClient(workspace, token)
        try:
            names = client._user_names(client._api())  # noqa: SLF001
        except Exception as e:
            print(f"  [WARN] {workspace.name}: users_list failed: {e}")
            continue
        before = len(id_to_name)
        id_to_name.update({k: v for k, v in names.items() if v and v != k})
        print(f"  {workspace.name}: +{len(id_to_name) - before} users gemapt")
    print(f"\nTotaal users in mapping: {len(id_to_name)}")
    if not id_to_name:
        print("Geen mapping verzameld — abort.")
        return 1

    # comm_items backfill
    print("\n=== comm_items ===")
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, from_addr FROM comm_items "
            "WHERE source = 'slack' "
            "  AND from_addr GLOB '[UB][0-9A-Z]*'"
        ).fetchall()
    print(f"  {len(rows)} kandidaat-rijen")
    updates_comm: list[tuple[str, int]] = []
    unresolved: dict[str, int] = {}
    for r in rows:
        old = r["from_addr"] or ""
        if not _SLACK_ID_RE.match(old):
            continue
        new = id_to_name.get(old)
        if not new:
            unresolved[old] = unresolved.get(old, 0) + 1
            continue
        updates_comm.append((new, int(r["id"])))
    print(f"  {len(updates_comm)} updates voorgesteld, "
          f"{sum(unresolved.values())} onbekende IDs "
          f"({len(unresolved)} distinct)")

    # open_loops backfill
    print("\n=== open_loops ===")
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        loop_rows = conn.execute(
            "SELECT id, who FROM open_loops "
            "WHERE who GLOB '[UB][0-9A-Z]*'"
        ).fetchall()
    updates_loops: list[tuple[str, int]] = []
    for r in loop_rows:
        old = r["who"] or ""
        if not _SLACK_ID_RE.match(old):
            continue
        new = id_to_name.get(old)
        if not new:
            continue
        updates_loops.append((new, int(r["id"])))
    print(f"  {len(updates_loops)} open_loops updates voorgesteld")

    if not commit:
        print("\n(dry-run — niets geschreven. Gebruik --commit om door te voeren.)")
        return 0

    print("\n=== Schrijven ===")
    with sqlite3.connect(settings.db_path, isolation_level=None) as conn:
        conn.executemany(
            "UPDATE comm_items SET from_addr = ? WHERE id = ?",
            updates_comm,
        )
        conn.executemany(
            "UPDATE open_loops SET who = ? WHERE id = ?",
            updates_loops,
        )
    print(f"  comm_items: {len(updates_comm)} geüpdated")
    print(f"  open_loops: {len(updates_loops)} geüpdated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
