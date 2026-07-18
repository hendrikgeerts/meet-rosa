#!/usr/bin/env python3
"""CLI voor Slack-workspacebeheer.

Voegt workspaces toe aan `config/slack_workspaces.yaml` en slaat user-tokens
op in macOS Keychain (service "pa-agent-slack"). Tokens komen NOOIT in de yaml.

Voorwerk per workspace (eenmalig, in een browser):
    1. Ga naar https://api.slack.com/apps → "Create New App" → "From scratch".
    2. App-naam: "PA Agent" (zichtbaar in workspace). Pick the workspace.
    3. Tab "OAuth & Permissions" → User Token Scopes (negen scopes):
         channels:read,    channels:history,
         groups:read,      groups:history,
         im:read,          im:history,
         mpim:read,        mpim:history,
         users:read
       (read-only — de agent post nooit zelf).
    4. Bovenaan dezelfde tab: "Install to Workspace" → autoriseer.
    5. Kopieer de "User OAuth Token" (begint met `xoxp-`).
    6. Run hieronder `add` en plak die token bij de prompt.

Gebruik:
    ./venv/bin/python scripts/slack_workspace.py list
    ./venv/bin/python scripts/slack_workspace.py add
    ./venv/bin/python scripts/slack_workspace.py edit <name>
    ./venv/bin/python scripts/slack_workspace.py remove <name>
    ./venv/bin/python scripts/slack_workspace.py test <name>
    ./venv/bin/python scripts/slack_workspace.py channels <name> [type]
    ./venv/bin/python scripts/slack_workspace.py recent <name> <channel> [limit]
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from integrations.slack import (  # noqa: E402
    SlackClient, SlackWorkspace,
    delete_token, get_token, load_workspaces, save_workspaces, set_token,
)

WORKSPACES_YAML = REPO_ROOT / "config" / "slack_workspaces.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


# --- helpers --------------------------------------------------------------

def _workspaces() -> list[SlackWorkspace]:
    return load_workspaces(WORKSPACES_YAML)


def _find(name: str) -> SlackWorkspace | None:
    for w in _workspaces():
        if w.name == name:
            return w
    return None


def _save(workspaces: list[SlackWorkspace]) -> None:
    save_workspaces(WORKSPACES_YAML, workspaces)


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    if not val and default is not None:
        return default
    return val


def _ask_int(prompt: str, default: int) -> int:
    val = _ask(prompt, str(default))
    try:
        return int(val)
    except ValueError:
        print(f"  ! geen geldig getal — gebruik {default}")
        return default


def _ask_bool(prompt: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "ja", "j", "1", "true")


def _client(w: SlackWorkspace) -> SlackClient:
    token = get_token(w)
    if not token:
        sys.exit(f"! geen token in Keychain voor '{w.name}' — "
                 f"run `slack_workspace.py edit {w.name}` om er een te zetten.")
    return SlackClient(w, token)


# --- commands -------------------------------------------------------------

def cmd_list(_args: argparse.Namespace) -> None:
    ws = _workspaces()
    if not ws:
        print("(geen workspaces geconfigureerd — gebruik `add`)")
        return
    print(f"{'name':<20} {'enabled':<7} {'workspace_url':<35} {'label'}")
    print("-" * 90)
    for w in ws:
        token_mark = "✓" if get_token(w) else "✗"
        en = "yes" if w.enabled else "no"
        print(f"{w.name:<20} {en:<7} {w.workspace_url:<35} {w.label}  token={token_mark}")
    print()
    print("token=✓ aanwezig in Keychain  |  token=✗ ontbrekend (run `edit`)")


def cmd_add(_args: argparse.Namespace) -> None:
    workspaces = _workspaces()
    print("Nieuw Slack-workspace toevoegen.\n")
    print("  Voorwerk: maak op https://api.slack.com/apps een Slack-app voor")
    print("  je workspace met negen User Token Scopes:")
    print("    channels:read   channels:history")
    print("    groups:read     groups:history")
    print("    im:read         im:history")
    print("    mpim:read       mpim:history")
    print("    users:read")
    print("  Installeer en kopieer de 'User OAuth Token' (xoxp-...).\n")

    name = _ask("name (kort: [a-z0-9_-], bv. 'initiale')")
    if not NAME_RE.match(name):
        sys.exit("! ongeldige naam — alleen [a-z0-9_-], 2-32 chars")
    if any(w.name == name for w in workspaces):
        sys.exit(f"! workspace '{name}' bestaat al — gebruik `edit` of `remove`")

    label = _ask("label", default=name.title())
    workspace_url = _ask("workspace_url (bv. 'initiale.slack.com', optioneel)", default="")
    poll = _ask_int("poll-interval seconden", 300)
    enabled = _ask_bool("nu inschakelen?", default=True)

    print("\nUser-OAuth-token wordt verborgen ingelezen en in macOS Keychain opgeslagen.")
    tok1 = getpass.getpass("token (xoxp-...): ")
    if not tok1.startswith("xoxp-"):
        if not _ask_bool("! token begint niet met xoxp- — toch opslaan?", default=False):
            sys.exit("geannuleerd")

    w = SlackWorkspace(
        name=name, label=label, workspace_url=workspace_url,
        enabled=enabled, poll_interval_seconds=poll,
    )
    set_token(w, tok1)
    _save([*workspaces, w])
    print(f"\n✓ workspace '{name}' toegevoegd. Test met:  slack_workspace.py test {name}")


def cmd_edit(args: argparse.Namespace) -> None:
    workspaces = _workspaces()
    w = _find(args.name)
    if w is None:
        sys.exit(f"! geen workspace '{args.name}'")
    print(f"Workspace '{w.name}' bewerken (Enter laat ongewijzigd).\n")
    label = _ask("label", default=w.label)
    workspace_url = _ask("workspace_url", default=w.workspace_url)
    poll = _ask_int("poll-interval seconden", w.poll_interval_seconds)
    enabled = _ask_bool("ingeschakeld?", default=w.enabled)

    new_token = ""
    if _ask_bool("token vervangen?", default=False):
        tok = getpass.getpass("nieuw token: ")
        if not tok.startswith("xoxp-") and not _ask_bool(
                "! token begint niet met xoxp- — toch opslaan?", default=False):
            sys.exit("geannuleerd")
        new_token = tok

    new = SlackWorkspace(
        name=w.name, label=label, workspace_url=workspace_url,
        enabled=enabled, poll_interval_seconds=poll,
    )
    if new_token:
        set_token(new, new_token)
    _save([new if x.name == w.name else x for x in workspaces])
    print(f"\n✓ workspace '{w.name}' bijgewerkt.")


def cmd_remove(args: argparse.Namespace) -> None:
    workspaces = _workspaces()
    w = _find(args.name)
    if w is None:
        sys.exit(f"! geen workspace '{args.name}'")
    confirm = _ask(f"verwijder '{w.name}' ({w.label})? typ de naam ter bevestiging")
    if confirm != w.name:
        sys.exit("! geannuleerd")
    delete_token(w)
    _save([x for x in workspaces if x.name != w.name])
    print(f"✓ workspace '{w.name}' verwijderd (yaml + Keychain).")


def cmd_test(args: argparse.Namespace) -> None:
    w = _find(args.name)
    if w is None:
        sys.exit(f"! geen workspace '{args.name}'")
    print(f"Verbinden met Slack als token-eigenaar van '{w.name}' ...")
    try:
        info = _client(w).test_connection()
    except Exception as exc:
        sys.exit(f"! auth.test mislukt: {exc}")
    if not info["ok"]:
        sys.exit(f"! auth.test gaf ok=False: {info}")
    print(f"✓ auth OK — team='{info['team']}', user='{info['user']}'")
    print(f"  url={info['url']}")


def cmd_channels(args: argparse.Namespace) -> None:
    w = _find(args.name)
    if w is None:
        sys.exit(f"! geen workspace '{args.name}'")
    types = tuple(args.types.split(",")) if args.types else (
        "public_channel", "private_channel", "im", "mpim")
    chans = _client(w).list_channels(types=types)
    print(f"{len(chans)} channels op {w.name}:")
    for c in sorted(chans, key=lambda x: (x.type, x.name)):
        member = "✓" if c.is_member else " "
        print(f"  [{c.type:<7}] {member} {c.name}  ({c.id})")


def cmd_recent(args: argparse.Namespace) -> None:
    w = _find(args.name)
    if w is None:
        sys.exit(f"! geen workspace '{args.name}'")
    print(f"Laatste {args.limit} berichten in {args.channel} op {w.name}:\n")
    for m in _client(w).list_recent(args.channel, limit=args.limit):
        thread_marker = " ↪" if m.thread_ts and m.thread_ts != m.ts else "  "
        print(f"{thread_marker}{m.user_name[:20]:<20} | {m.text_snippet[:80]}")


# --- main -----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Slack-workspacebeheer voor pa-agent.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("add").set_defaults(func=cmd_add)

    p_edit = sub.add_parser("edit"); p_edit.add_argument("name"); p_edit.set_defaults(func=cmd_edit)
    p_remove = sub.add_parser("remove"); p_remove.add_argument("name"); p_remove.set_defaults(func=cmd_remove)
    p_test = sub.add_parser("test"); p_test.add_argument("name"); p_test.set_defaults(func=cmd_test)

    p_channels = sub.add_parser("channels")
    p_channels.add_argument("name")
    p_channels.add_argument("types", nargs="?", default=None,
                            help="comma-separated subset of public_channel,private_channel,im,mpim")
    p_channels.set_defaults(func=cmd_channels)

    p_recent = sub.add_parser("recent")
    p_recent.add_argument("name")
    p_recent.add_argument("channel", help="channel name or id")
    p_recent.add_argument("limit", nargs="?", type=int, default=20)
    p_recent.set_defaults(func=cmd_recent)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
