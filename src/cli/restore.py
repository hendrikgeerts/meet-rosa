"""`rosa restore <backup.tar.gz>` — restore a Rosa backup.

Usage:
    rosa restore ~/rosa-backup-20260718-193000.tar.gz
    rosa restore <path> --dry-run       # list what would be restored
    rosa restore <path> --force         # overwrite existing files

By default restore FAILS if ROSA_HOME already has a config.yaml to
prevent accidental data-loss. Use --force to override.
"""
from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa restore", description=__doc__)
    ap.add_argument("archive", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing files in ROSA_HOME")
    args = ap.parse_args(argv)

    archive = args.archive.expanduser()
    if not archive.exists():
        print(f"backup not found: {archive}", file=sys.stderr)
        return 1

    from core.config import get_rosa_home
    home = get_rosa_home()

    if (home / "config.yaml").exists() and not args.force and not args.dry_run:
        print(f"error: {home / 'config.yaml'} already exists.", file=sys.stderr)
        print("Use --force to overwrite, or move it aside first.", file=sys.stderr)
        return 1

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if args.dry_run:
            print(f"Would restore {len(members)} entries to {home}/:")
            for m in members:
                print(f"  {m.name}  ({m.size} bytes)")
            return 0
        home.mkdir(parents=True, exist_ok=True)
        # Safety: reject paths that try to escape ROSA_HOME.
        for m in members:
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                print(f"refusing unsafe path in archive: {m.name}",
                      file=sys.stderr)
                return 1
        # `filter="data"` is Python 3.12+ safe-extract mode.
        tar.extractall(home, filter="data")

    print(f"✓ Restored {len(members)} entries to {home}/")
    print("\nNext: start Rosa with `python src/main.py` from the repo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
