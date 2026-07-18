"""Print de laatste PA-LOC rijen uit `current_location` met leesbare
timestamps + "age". Handig om te verifiëren of de iOS Shortcut z'n
mail werkelijk doorkomt naar Rosa.

Gebruik:
  ./venv/bin/python scripts/check_location.py            # laatste 5
  ./venv/bin/python scripts/check_location.py --limit 20 # meer
  ./venv/bin/python scripts/check_location.py --watch    # poll elke 10s
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from core.config import load_settings  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")


def _humanize_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m ago"
    return f"{seconds // 86400}d ago"


def _print_rows(db_path: Path, limit: int) -> int:
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, lat, lon, accuracy_m, received_at, source "
            "FROM current_location ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    if not rows:
        print("(no location rows yet — iOS Shortcut hasn't sent anything,")
        print(" or the mail hasn't been ingested yet. Comm-ingest polls")
        print(" every 5 min by default.)")
        return 0

    now = int(time.time())
    print(f"{'id':>5}  {'when':<19}  {'age':<12}  {'lat':>10}  {'lon':>10}  {'acc':>6}  source")
    print("-" * 90)
    for r in rows:
        dt = datetime.fromtimestamp(r["received_at"], TZ).strftime("%Y-%m-%d %H:%M:%S")
        age = _humanize_age(now - int(r["received_at"]))
        acc = f"{r['accuracy_m']:.0f}m" if r["accuracy_m"] is not None else "—"
        print(
            f"{r['id']:>5}  {dt:<19}  {age:<12}  "
            f"{r['lat']:>10.5f}  {r['lon']:>10.5f}  {acc:>6}  {r['source']}"
        )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5,
                        help="Aantal rijen om te tonen (default 5)")
    parser.add_argument("--watch", action="store_true",
                        help="Poll elke 10s en print bij verandering")
    args = parser.parse_args()

    settings = load_settings(config_path=_REPO_ROOT / "config" / "settings.yaml")
    db_path = settings.db_path
    if not db_path.exists():
        print(f"error: memory.db not found at {db_path}", file=sys.stderr)
        sys.exit(2)

    if not args.watch:
        _print_rows(db_path, args.limit)
        return

    print("Watching current_location (Ctrl-C to stop)...\n")
    last_count = -1
    try:
        while True:
            with sqlite3.connect(db_path, isolation_level=None) as conn:
                n = conn.execute("SELECT COUNT(*) FROM current_location").fetchone()[0]
            if n != last_count:
                if last_count >= 0:
                    print(f"\n--- {n - last_count} new row(s) ---")
                else:
                    print(f"--- initial state: {n} row(s) ---")
                _print_rows(db_path, args.limit)
                last_count = n
                print()
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n(stopped)")


if __name__ == "__main__":
    main()
