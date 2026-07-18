"""`rosa backup` — snapshot config + memory-db + logs to a timestamped tar.gz.

Usage:
    rosa backup                        # → ~/rosa-backup-YYYYMMDD-HHMMSS.tar.gz
    rosa backup --out /path/to/dir     # custom destination
    rosa backup --include-audit        # also include audit-logs (larger)

Restore with `rosa restore <path>`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import tarfile
from pathlib import Path


def _default_out() -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.home() / f"rosa-backup-{ts}.tar.gz"


def _add_if_exists(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    if path.exists():
        tar.add(path, arcname=arcname)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa backup", description=__doc__)
    ap.add_argument("--out", type=Path, default=None,
                    help="Output file path (default: ~/rosa-backup-<ts>.tar.gz)")
    ap.add_argument("--include-audit", action="store_true",
                    help="Also include audit/ (bigger)")
    ap.add_argument("--include-logs", action="store_true",
                    help="Also include logs/")
    args = ap.parse_args(argv)

    from core.config import get_rosa_home
    home = get_rosa_home()
    if not home.exists():
        print(f"ROSA_HOME does not exist: {home}", file=sys.stderr)
        return 1

    out = args.out or _default_out()
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(out, "w:gz") as tar:
        _add_if_exists(tar, home / "config.yaml", "config.yaml")
        _add_if_exists(tar, home / "config", "config")
        _add_if_exists(tar, home / "secrets.env", "secrets.env")
        _add_if_exists(tar, home / "data" / "memory.db", "data/memory.db")
        _add_if_exists(tar, home / "google_token.json", "google_token.json")
        if args.include_audit:
            _add_if_exists(tar, home / "audit", "audit")
        if args.include_logs:
            _add_if_exists(tar, home / "logs", "logs")

    size = out.stat().st_size
    print(f"✓ Backup written: {out} ({size / 1024:.1f} KB)")
    print("\nRestore later with: rosa restore " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
