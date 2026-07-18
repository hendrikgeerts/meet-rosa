"""`rosa update` — pull latest from git + reinstall deps + doctor.

Usage:
    rosa update              # git pull + pip install -r requirements + doctor
    rosa update --check      # only check if there's an update available
    rosa update --restart    # after update, reload the LaunchAgent

Not for production use if you have uncommitted changes to the repo.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _repo_dir() -> Path:
    # cli/update_cmd.py → src/cli/update_cmd.py → repo/src/cli/update_cmd.py
    return Path(__file__).resolve().parent.parent.parent


def _git(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    # M-6: GIT_TERMINAL_PROMPT=0 en GIT_ASKPASS=true voorkomen dat git
    # gaat hangen op interactive credential-prompts (bv. expired token).
    # Timeout=60s zorgt dat we niet oneindig wachten op network-flake.
    import os as _os
    env = {**_os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True,
            env=env, timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, f"git {args[0]} timed out after {timeout}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa update", description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="Only check whether an update is available")
    ap.add_argument("--restart", action="store_true",
                    help="Reload LaunchAgent after successful update")
    args = ap.parse_args(argv)

    repo = _repo_dir()
    if not (repo / ".git").exists():
        print(f"Not a git repo: {repo}", file=sys.stderr)
        print("Rosa was installed from a tarball; update manually.",
              file=sys.stderr)
        return 1

    print(f"Checking {repo}...")

    # 1. Uncommitted changes?
    rc, out = _git(["status", "--porcelain"], repo)
    if out:
        print("⚠ Uncommitted changes present:")
        print(out)
        print("Commit or stash before updating.")
        return 1

    # 2. Fetch
    rc, out = _git(["fetch", "--tags"], repo)
    if rc != 0:
        print(f"git fetch failed: {out}", file=sys.stderr)
        return 1

    # 3. Behind how many commits?
    rc, ahead_behind = _git(
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], repo,
    )
    if rc != 0:
        print("Cannot determine branch upstream — set one with "
              "`git branch --set-upstream-to=origin/main`",
              file=sys.stderr)
        return 1
    parts = ahead_behind.split()
    if len(parts) != 2:
        print(f"unexpected rev-list output: {ahead_behind}", file=sys.stderr)
        return 1
    ahead, behind = int(parts[0]), int(parts[1])

    if behind == 0:
        print("✓ Already up to date.")
        return 0

    print(f"→ {behind} new commit(s) upstream.")
    if ahead > 0:
        print(f"⚠ You have {ahead} local commit(s) not upstream — "
              "resolve before update.")
        return 1

    if args.check:
        return 0

    # 4. Remember current HEAD zodat we kunnen rollbacken bij pip-failure.
    _, current_head = _git(["rev-parse", "HEAD"], repo)

    # 5. Pull
    rc, out = _git(["pull", "--ff-only"], repo)
    if rc != 0:
        print(f"git pull failed: {out}", file=sys.stderr)
        return 1
    print("✓ Code updated.")

    # 6. pip install (M-5: rollback bij failure zodat je geen half-install
    #    achterlaat — code+deps moeten samen consistent zijn)
    from core.config import get_rosa_home
    venv_pip = get_rosa_home() / "venv" / "bin" / "pip"
    if venv_pip.exists():
        print("Installing dependencies...")
        r = subprocess.run(
            [str(venv_pip), "install", "--quiet", "-r",
             str(repo / "requirements.txt")],
            timeout=600,
        )
        if r.returncode != 0:
            print("⚠ pip install failed — rolling back to previous commit",
                  file=sys.stderr)
            _git(["reset", "--hard", current_head.strip()], repo)
            print(f"✓ rolled back to {current_head[:8]}")
            return 1

    # 6. Re-doctor
    from cli import doctor
    print("\n" + "═" * 60)
    doctor.main([])

    # 7. Optional restart
    if args.restart and sys.platform == "darwin":
        home = get_rosa_home()
        plist = Path.home() / "Library" / "LaunchAgents" / "com.rosa.pa-agent.plist"
        if plist.exists():
            print("\nReloading LaunchAgent...")
            subprocess.run(["launchctl", "unload", str(plist)])
            subprocess.run(["launchctl", "load", str(plist)])
            print("✓ LaunchAgent reloaded.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
