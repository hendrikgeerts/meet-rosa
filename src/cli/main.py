"""Rosa CLI dispatcher — `rosa <command> [args...]`.

Available commands:
    doctor      Diagnose your installation
    logs        View log-files
    backup      Backup config + memory database
    restore     Restore from a backup
    setup       Re-run the setup wizard

Usage:
    rosa                 # prints help
    rosa doctor
    rosa logs --follow
"""
from __future__ import annotations

import sys

COMMANDS = {
    "doctor": "cli.doctor",
    "logs": "cli.logs_view",
    "backup": "cli.backup",
    "restore": "cli.restore",
    "setup": "cli.setup_cmd",
    "cost": "cli.cost",
    "update": "cli.update_cmd",
    "simulate": "cli.simulate",
    "reload": "cli.reload_cmd",
}


def _print_help() -> None:
    print("Rosa — privacy-first personal AI assistant\n")
    print("Usage: rosa <command> [args...]\n")
    print("Commands:")
    print("  doctor          Diagnose your installation")
    print("  logs            View log-files (--follow to tail)")
    print("  cost            Show current-month Anthropic spend")
    print("  backup          Backup config + memory database")
    print("  restore         Restore from a backup")
    print("  update          Pull latest from GitHub + re-doctor")
    print("  setup           Re-run the setup wizard")
    print("  reload          Signal running daemon to reload config.yaml")
    print("  simulate        Feed a synthetic iMessage for testing")
    print("\nRun `rosa <command> --help` for command-specific options.")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        _print_help()
        return 1

    import importlib
    module = importlib.import_module(COMMANDS[cmd])
    return module.main(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
