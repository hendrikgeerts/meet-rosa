"""`rosa reload` — signal the running daemon to reload config.yaml.

Sends SIGHUP to Rosa. On next poll-tick the daemon re-reads
`config.yaml` without restarting. External clients (Ollama, Gmail,
Claude) are NOT re-initialised.

Usage:
    rosa reload
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _find_rosa_pid() -> int | None:
    """Find the PID of the running Rosa main.py process.

    Preferred: read `ROSA_HOME/rosa.pid` (written by main.py at boot).
    Fallback: pgrep -f 'src/main.py' — but only accept if EXACTLY one
    match, to avoid killing test-runners or editors that happen to have
    'src/main.py' in their command line.
    """
    from core.config import get_rosa_home
    pidfile = get_rosa_home() / "rosa.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            # Verify the process is still alive.
            import os
            os.kill(pid, 0)  # signal 0 = "is this PID alive?"
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale pidfile; fall through to pgrep
        except OSError:
            pass

    try:
        r = subprocess.run(
            ["pgrep", "-f", "src/main.py"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            pids = r.stdout.strip().splitlines()
            if len(pids) == 1:
                # Verify het is écht een Python-process (via ps)
                r2 = subprocess.run(
                    ["ps", "-p", pids[0], "-o", "comm="],
                    capture_output=True, text=True, timeout=3,
                )
                if r2.returncode == 0 and "python" in r2.stdout.lower():
                    return int(pids[0])
            elif len(pids) > 1:
                print(f"⚠ multiple main.py processes found: {pids}. "
                      f"Refusing to signal to avoid killing the wrong one. "
                      f"Kill manually or start Rosa fresh to reset pidfile.",
                      file=sys.stderr)
                return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa reload", description=__doc__)
    args = ap.parse_args(argv)

    pid = _find_rosa_pid()
    if pid is None:
        print("Rosa doesn't seem to be running (no 'src/main.py' process).",
              file=sys.stderr)
        return 1

    import signal
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        print(f"process {pid} exited before we could signal it",
              file=sys.stderr)
        return 1
    except PermissionError:
        print(f"no permission to signal pid {pid}", file=sys.stderr)
        return 1

    print(f"✓ Sent SIGHUP to pid {pid}. Config will reload on next poll.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
