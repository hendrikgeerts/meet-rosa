"""`rosa setup` — re-run the setup wizard.

Deletes `.wizard_state.json` so the bootstrap flow starts the wizard
again on next `main.py` launch. Does NOT delete config.yaml or
secrets.env — those stay untouched so you can re-do individual steps.

Usage:
    rosa setup          # re-arm the wizard
    rosa setup --reset  # ALSO wipe config.yaml + secrets.env (destructive!)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa setup", description=__doc__)
    ap.add_argument("--reset", action="store_true",
                    help="Also wipe config.yaml + secrets.env (destructive)")
    args = ap.parse_args(argv)

    from core.config import get_rosa_home
    home = get_rosa_home()
    state = home / ".wizard_state.json"

    if state.exists():
        state.unlink()
        print(f"✓ removed {state.name}")

    if args.reset:
        confirm = input(
            "This will DELETE your config.yaml and secrets.env "
            "(API keys!). Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            print("aborted.")
            return 1
        for f in [home / "config.yaml", home / "secrets.env",
                  home / "google_token.json"]:
            if f.exists():
                f.unlink()
                print(f"✓ removed {f.name}")

    print("\nNext: start Rosa with `python src/main.py` from the repo.")
    print("The wizard will re-open at http://localhost:8765/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
