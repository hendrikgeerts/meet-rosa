"""`rosa cost` — laat je huidige maand's Anthropic-spend zien.

Usage:
    rosa cost                 # deze maand
    rosa cost --days 30       # per-dag laatste 30 dagen
    rosa cost --json          # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa cost", description=__doc__)
    ap.add_argument("--days", type=int, default=None,
                    help="Show per-day totals for the last N days")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from core.config import load_settings
    settings = load_settings()

    from core.cost_tracker import current_month_cost, daily_series
    month = current_month_cost(settings.db_path)

    if args.json:
        out: dict = {
            "month": {
                "calls": month.calls, "tokens_in": month.tokens_in,
                "tokens_out": month.tokens_out, "usd": month.usd,
            },
            "budget_usd": settings.monthly_anthropic_budget_usd,
        }
        if args.days:
            out["daily"] = daily_series(settings.db_path, days=args.days)
        print(json.dumps(out, indent=2, default=str))
        return 0

    print(f"Anthropic — current month:")
    print(f"  calls           {month.calls}")
    print(f"  input tokens    {month.tokens_in:,}")
    print(f"  output tokens   {month.tokens_out:,}")
    print(f"  spend           ${month.usd:.4f}")
    budget = settings.monthly_anthropic_budget_usd
    if budget > 0:
        pct = 100 * month.usd / budget if budget else 0
        icon = "⚠" if pct > 80 else "✓"
        print(f"  budget          ${budget:.2f}  ({icon} {pct:.1f}% used)")
    else:
        print(f"  budget          none set (see config.yaml → "
              f"privacy.monthly_anthropic_budget_usd)")

    if args.days:
        print(f"\nDaily breakdown (last {args.days} days):")
        for row in daily_series(settings.db_path, days=args.days):
            print(f"  {row['date']}  {row['calls']:>4} calls  "
                  f"${row['usd']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
