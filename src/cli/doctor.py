"""`rosa doctor` — verify that a Rosa installation is healthy.

Run this when something isn't working. Output is designed to be
pasted into a bug-report: it lists everything a maintainer would
want to know without leaking secrets.

Usage:
    rosa doctor              # human-readable
    rosa doctor --json       # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def _bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}TB"


def _mask(secret: str) -> str:
    if not secret:
        return "MISSING"
    if len(secret) < 12:
        return "***"
    return secret[:6] + "…" + secret[-4:]


def _run_ok(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.returncode == 0, (r.stdout or r.stderr).strip()[:200]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def collect_diagnostics() -> dict:
    """Verzamel alle info. Returns a JSON-serializable dict."""
    # Late imports zodat --help snel is en zodat de doctor werkt zelfs
    # als sommige modules niet geladen kunnen worden.
    from core.config import get_rosa_home, is_configured

    home = get_rosa_home()
    diag: dict = {
        "rosa_home": str(home),
        "is_configured": is_configured(),
        "env": {
            "ROSA_HOME": os.environ.get("ROSA_HOME", ""),
            "ROSA_DEV": os.environ.get("ROSA_DEV", ""),
            "python": sys.version.split()[0],
            "platform": sys.platform,
        },
        "config_file": {},
        "secrets": {},
        "data": {},
        "logs": {},
        "prereqs": {},
        "services": {},
    }

    # Config file
    cfg_path = home / "config.yaml"
    diag["config_file"]["path"] = str(cfg_path)
    diag["config_file"]["exists"] = cfg_path.exists()
    if cfg_path.exists():
        diag["config_file"]["size"] = _bytes(cfg_path.stat().st_size)
        diag["config_file"]["perms"] = oct(cfg_path.stat().st_mode & 0o777)

    # Secrets (masked)
    sec_path = home / "secrets.env"
    diag["secrets"]["path"] = str(sec_path)
    diag["secrets"]["exists"] = sec_path.exists()
    if sec_path.exists():
        diag["secrets"]["perms"] = oct(sec_path.stat().st_mode & 0o777)
        # Read keys only (values masked)
        keys = {}
        for line in sec_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            keys[k.strip()] = _mask(v.strip().strip('"').strip("'"))
        diag["secrets"]["keys"] = keys

    # Data
    data_dir = home / "data"
    diag["data"]["dir"] = str(data_dir)
    diag["data"]["exists"] = data_dir.exists()
    db_path = data_dir / "memory.db"
    if db_path.exists():
        diag["data"]["db_size"] = _bytes(db_path.stat().st_size)
        try:
            with sqlite3.connect(db_path) as conn:
                tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
                diag["data"]["db_tables"] = tables
        except sqlite3.Error as e:
            diag["data"]["db_error"] = str(e)

    # Logs
    logs_dir = home / "logs"
    diag["logs"]["dir"] = str(logs_dir)
    diag["logs"]["exists"] = logs_dir.exists()
    if logs_dir.exists():
        log_files = sorted(logs_dir.glob("*.log"))
        diag["logs"]["files"] = [
            {"name": f.name, "size": _bytes(f.stat().st_size)}
            for f in log_files
        ]

    # Prereqs
    diag["prereqs"]["ollama"] = shutil.which("ollama") is not None
    diag["prereqs"]["brew"] = shutil.which("brew") is not None
    diag["prereqs"]["git"] = shutil.which("git") is not None
    diag["prereqs"]["python312"] = shutil.which("python3.12") is not None

    # Disk-space in home
    if home.exists():
        stats = shutil.disk_usage(home)
        diag["prereqs"]["disk_free"] = _bytes(stats.free)
        diag["prereqs"]["disk_total"] = _bytes(stats.total)

    # macOS-specific: iMessage bridge
    chat_db = Path.home() / "Library" / "Messages" / "chat.db"
    diag["services"]["chat_db_exists"] = chat_db.exists()
    if chat_db.exists():
        try:
            with chat_db.open("rb") as f:
                f.read(16)
            diag["services"]["chat_db_readable"] = True
        except PermissionError:
            diag["services"]["chat_db_readable"] = False
            diag["services"]["fda_hint"] = (
                "Grant Full Disk Access to the terminal/python "
                "in System Settings → Privacy & Security."
            )

    # Live services
    from wizard.health_checks import check_anthropic, check_ollama
    ollama_check = check_ollama()
    diag["services"]["ollama"] = {
        "ok": ollama_check["ok"], "message": ollama_check["message"],
    }
    if "keys" in diag["secrets"] and diag["secrets"]["keys"].get(
        "ANTHROPIC_API_KEY", "MISSING"
    ) != "MISSING":
        # Read the actual key (we don't want to leak it, but the check
        # itself needs it — it happens locally only).
        for line in sec_path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                key = line.partition("=")[2].strip().strip('"').strip("'")
                ant = check_anthropic(key)
                diag["services"]["anthropic"] = {
                    "ok": ant["ok"], "message": ant["message"],
                }
                break

    # LaunchAgent status (macOS-only)
    if sys.platform == "darwin":
        ok, out = _run_ok(["launchctl", "list"])
        if ok:
            diag["services"]["launchagent_registered"] = (
                "com.rosa.pa-agent" in out
            )

        # M17j: FileVault-status. Rosa's privacy claim leunt op
        # het feit dat data lokaal blijft — als de disk niet
        # encrypted is, is "verlies je Mac" een compleet dataverlies.
        ok, out = _run_ok(["fdesetup", "status"])
        if ok:
            enabled = "on" in out.lower()
            diag["prereqs"]["filevault"] = enabled
            if not enabled:
                diag["prereqs"]["filevault_hint"] = (
                    "FileVault is off. Rosa's 'privacy-first' claim "
                    "assumes disk-encryption. Enable in System Settings "
                    "→ Privacy & Security → FileVault."
                )

    return diag


def _print_human(diag: dict) -> None:
    def line(k, v, indent=0):
        print(" " * indent + f"{k:<25} {v}")

    print("═" * 60)
    print("Rosa doctor — installation diagnostics")
    print("═" * 60)
    line("ROSA_HOME", diag["rosa_home"])
    line("configured", "yes" if diag["is_configured"] else "no ← run wizard")

    print("\n─── Environment")
    for k, v in diag["env"].items():
        line(k, v or "-")

    print("\n─── Config")
    cf = diag["config_file"]
    line("config.yaml", "present" if cf.get("exists") else "MISSING")
    if cf.get("exists"):
        line("size", cf.get("size"))
        line("perms", cf.get("perms"))

    print("\n─── Secrets")
    sec = diag["secrets"]
    line("secrets.env", "present" if sec.get("exists") else "MISSING")
    if sec.get("exists"):
        line("perms", sec.get("perms") + " " + (
            "(0600 OK)" if sec.get("perms") == "0o600" else "⚠ should be 0o600"
        ))
        for k, v in sec.get("keys", {}).items():
            line(k, v, indent=2)

    print("\n─── Data")
    d = diag["data"]
    line("data dir", "present" if d.get("exists") else "MISSING")
    if d.get("db_size"):
        line("memory.db", f"{d['db_size']} ({d.get('db_tables', '?')} tables)")

    print("\n─── Logs")
    lg = diag["logs"]
    if lg.get("exists"):
        for f in lg.get("files", [])[:5]:
            line(f["name"], f["size"], indent=2)
    else:
        line("logs dir", "MISSING")

    print("\n─── Prereqs")
    p = diag["prereqs"]
    line("Ollama installed", "✓" if p.get("ollama") else "✗ brew install ollama")
    line("Homebrew installed", "✓" if p.get("brew") else "✗")
    line("git installed", "✓" if p.get("git") else "✗")
    line("Python 3.12", "✓" if p.get("python312") else "✗")
    if p.get("disk_free"):
        line("Disk free", f"{p['disk_free']} of {p.get('disk_total','?')}")
    if "filevault" in p:
        line("FileVault", "✓ enabled" if p["filevault"] else "⚠ OFF — "
             + p.get("filevault_hint", "")[:80])

    print("\n─── Live services")
    s = diag["services"]
    if "chat_db_readable" in s:
        line(
            "iMessage FDA",
            "✓" if s["chat_db_readable"] else "✗ " + s.get("fda_hint", ""),
        )
    if "ollama" in s:
        st = s["ollama"]
        line("Ollama", ("✓ " if st["ok"] else "✗ ") + st["message"])
    if "anthropic" in s:
        st = s["anthropic"]
        line("Anthropic", ("✓ " if st["ok"] else "✗ ") + st["message"])
    if "launchagent_registered" in s:
        line("LaunchAgent", "loaded" if s["launchagent_registered"] else "not loaded")

    print("\n═" * 30)
    print("Paste this output into your bug report if something's off.")
    print("═" * 30)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa doctor", description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="Output machine-readable JSON")
    args = ap.parse_args(argv)

    diag = collect_diagnostics()
    if args.json:
        print(json.dumps(diag, indent=2, default=str))
    else:
        _print_human(diag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
