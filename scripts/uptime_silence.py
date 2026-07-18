"""Silence één uptime-target voor N minuten met audit-event.

Vervangt de rauwe SQL UPDATE die in UPTIME_NTFY_SETUP.md gedocumenteerd
stond — die mist een audit-spoor (A.12.4.3 administrator logs).

Gebruik:
  ./venv/bin/python scripts/uptime_silence.py --name "YourProduct CMS" --minutes 30
  ./venv/bin/python scripts/uptime_silence.py --name "YourProduct CMS" --minutes 30 --reason "deploy v2.4"
  ./venv/bin/python scripts/uptime_silence.py --name "YourProduct CMS" --clear
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from core.config import load_settings  # noqa: E402
from extensions.uptime.schema import silence_with_audit  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True,
                        help="Target-naam zoals in config/uptime.yaml")
    parser.add_argument("--minutes", type=int, default=0,
                        help="Hoe lang silencen (minuten). Vereist als --clear ontbreekt.")
    parser.add_argument("--reason", default=None,
                        help="Optionele reden — komt in audit-event detail")
    parser.add_argument("--clear", action="store_true",
                        help="Maak silence-window leeg (= weer monitoring aan)")
    args = parser.parse_args()

    if not args.clear and args.minutes <= 0:
        parser.error("--minutes > 0 vereist, of --clear")

    settings = load_settings(config_path=_REPO_ROOT / "config" / "settings.yaml")
    actor = os.environ.get("USER", "operator")
    until = None if args.clear else int(time.time()) + args.minutes * 60

    with sqlite3.connect(settings.db_path, isolation_level=None) as conn:
        # Controleer dat target bestaat
        row = conn.execute(
            "SELECT 1 FROM uptime_checks WHERE name=?", (args.name,),
        ).fetchone()
        if row is None:
            print(f"error: target {args.name!r} bestaat niet in uptime_checks", file=sys.stderr)
            print("Beschikbare targets:", file=sys.stderr)
            for (n,) in conn.execute("SELECT name FROM uptime_checks ORDER BY name"):
                print(f"  - {n}", file=sys.stderr)
            sys.exit(2)
        silence_with_audit(
            conn, name=args.name, until=until,
            reason=args.reason, actor=actor,
        )
    # Admin-action audit-stream (apart van uptime_events) zodat ISO-
    # auditors één file kunnen openen voor alle admin-acties.
    from core.audit import AdminActionLogger, bind_admin_logger, log_admin_action
    bind_admin_logger(AdminActionLogger(settings.audit_dir))
    log_admin_action(
        action="uptime_silence",
        actor=actor,
        from_value=None,
        to_value=until,
        target=args.name,
        reason=args.reason,
    )

    if args.clear:
        print(f"Silence cleared for {args.name!r} (by {actor})")
    else:
        end_str = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(until or 0),
        )
        print(f"Silenced {args.name!r} until {end_str} (by {actor})")


if __name__ == "__main__":
    main()
