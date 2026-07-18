"""`rosa logs` — tail Rosa's log-files with sensible defaults.

Usage:
    rosa logs                # tail latest agent.log
    rosa logs --follow       # tail -f
    rosa logs --lines 200
    rosa logs stdout         # LaunchAgent stdout
    rosa logs stderr         # LaunchAgent stderr
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _log_path(kind: str) -> Path:
    from core.config import get_rosa_home
    home = get_rosa_home()
    if kind == "agent":
        return home / "logs" / "agent.log"
    if kind == "stdout":
        return home / "logs" / "stdout.log"
    if kind == "stderr":
        return home / "logs" / "stderr.log"
    raise ValueError(f"unknown log kind {kind!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa logs", description=__doc__)
    ap.add_argument("kind", nargs="?", default="agent",
                    choices=("agent", "stdout", "stderr"))
    ap.add_argument("-n", "--lines", type=int, default=50,
                    help="Number of lines to show (default: 50)")
    ap.add_argument("-f", "--follow", action="store_true",
                    help="Follow the log (like tail -f)")
    args = ap.parse_args(argv)

    try:
        path = _log_path(args.kind)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not path.exists():
        print(f"log not found: {path}", file=sys.stderr)
        print("Has Rosa run yet?", file=sys.stderr)
        return 1

    cmd = ["tail", f"-n{args.lines}"]
    if args.follow:
        cmd.append("-f")
    cmd.append(str(path))
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
