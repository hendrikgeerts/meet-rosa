#!/usr/bin/env python3
"""CLI voor IMAP-accountbeheer.

Voegt accounts toe aan `config/imap_accounts.yaml` en slaat passwords op in
macOS Keychain (service "pa-agent-imap"). Wachtwoorden komen NOOIT in de yaml.

Gebruik:
    ./venv/bin/python scripts/imap_account.py list
    ./venv/bin/python scripts/imap_account.py add
    ./venv/bin/python scripts/imap_account.py edit <name>
    ./venv/bin/python scripts/imap_account.py remove <name>
    ./venv/bin/python scripts/imap_account.py test <name>
    ./venv/bin/python scripts/imap_account.py folders <name>
    ./venv/bin/python scripts/imap_account.py recent <name> [folder] [limit]

`folders` is handig om de juiste Sent/Verzonden-mapnaam te vinden.
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from integrations.imap import (  # noqa: E402
    ImapAccount, ImapClient, ImapFolders,
    delete_password, get_password, load_accounts, save_accounts, set_password,
)

ACCOUNTS_YAML = REPO_ROOT / "config" / "imap_accounts.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


# --- helpers --------------------------------------------------------------

def _accounts() -> list[ImapAccount]:
    return load_accounts(ACCOUNTS_YAML)


def _find(name: str) -> ImapAccount | None:
    for a in _accounts():
        if a.name == name:
            return a
    return None


def _save(accounts: list[ImapAccount]) -> None:
    save_accounts(ACCOUNTS_YAML, accounts)


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


def _client(acc: ImapAccount) -> ImapClient:
    pw = get_password(acc)
    if not pw:
        sys.exit(f"! geen wachtwoord in Keychain voor '{acc.name}' — "
                 f"run `imap_account.py edit {acc.name}` om er een te zetten.")
    return ImapClient(acc, pw)


# --- commands -------------------------------------------------------------

def cmd_list(_args: argparse.Namespace) -> None:
    accounts = _accounts()
    if not accounts:
        print("(geen accounts geconfigureerd — gebruik `add`)")
        return
    print(f"{'name':<20} {'enabled':<7} {'host:port':<35} {'username'}")
    print("-" * 90)
    for a in accounts:
        has_pw = "✓" if get_password(a) else "✗"
        en = "yes" if a.enabled else "no"
        print(f"{a.name:<20} {en:<7} {a.host}:{a.port:<8} {a.username}  pw={has_pw}")
    print()
    print("pw=✓ password aanwezig in Keychain  |  pw=✗ ontbrekend (run `edit`)")


def cmd_add(_args: argparse.Namespace) -> None:
    accounts = _accounts()
    print("Nieuw IMAP-account toevoegen.\n")
    name = _ask("name (kort: [a-z0-9_-], bv. 'initiale')")
    if not NAME_RE.match(name):
        sys.exit("! ongeldige naam — alleen [a-z0-9_-], 2-32 chars")
    if any(a.name == name for a in accounts):
        sys.exit(f"! account '{name}' bestaat al — gebruik `edit` of `remove`")

    label = _ask("label (vrije tekst voor in iMessage)", default=name.title())
    host = _ask("IMAP-host (bv. 'mail.initiale.nl')")
    port = _ask_int("port", 993)
    ssl = _ask_bool("SSL/IMAPS gebruiken?", default=True)
    username = _ask("gebruikersnaam (vaak je email)")
    inbox = _ask("inbox-folder", default="INBOX")
    sent = _ask("verzonden-folder (Sent / Sent Items / Verzonden / INBOX.Sent)",
                default="Sent")
    poll = _ask_int("poll-interval seconden", 300)
    enabled = _ask_bool("nu inschakelen?", default=True)

    print("\n--- SMTP (uitgaande mail) — optioneel, leeg laten om te skippen.")
    smtp_host = _ask("SMTP-host (bv. 'smtp.initiale.nl')", default="")
    smtp_port = _ask_int("SMTP-port", 587) if smtp_host else 587
    smtp_starttls = _ask_bool("STARTTLS?", default=True) if smtp_host else True
    from_address = _ask("From-address (Enter = gebruik IMAP username)",
                        default=username) if smtp_host else None
    from_name = _ask("From-name (optioneel, bv. '[Your Name]')",
                     default="") if smtp_host else ""

    print("\nWachtwoord wordt verborgen ingelezen en opgeslagen in macOS Keychain.")
    pw1 = getpass.getpass("password: ")
    pw2 = getpass.getpass("herhaal:  ")
    if pw1 != pw2:
        sys.exit("! wachtwoorden komen niet overeen — geannuleerd")

    acc = ImapAccount(
        name=name, label=label, host=host, port=port, ssl=ssl,
        username=username,
        folders=ImapFolders(inbox=inbox, sent=sent),
        enabled=enabled, poll_interval_seconds=poll,
        smtp_host=(smtp_host or None),
        smtp_port=smtp_port,
        smtp_use_starttls=smtp_starttls,
        from_address=from_address,
        from_name=(from_name or None),
    )
    set_password(acc, pw1)
    _save([*accounts, acc])
    print(f"\n✓ account '{name}' toegevoegd. Test met:  imap_account.py test {name}")


def cmd_edit(args: argparse.Namespace) -> None:
    accounts = _accounts()
    acc = _find(args.name)
    if acc is None:
        sys.exit(f"! geen account '{args.name}'")
    print(f"Account '{acc.name}' bewerken (Enter laat ongewijzigd).\n")
    label = _ask("label", default=acc.label)
    host = _ask("host", default=acc.host)
    port = _ask_int("port", acc.port)
    ssl = _ask_bool("SSL?", default=acc.ssl)
    username = _ask("username", default=acc.username)
    inbox = _ask("inbox-folder", default=acc.folders.inbox)
    sent = _ask("sent-folder", default=acc.folders.sent)
    poll = _ask_int("poll-interval seconden", acc.poll_interval_seconds)
    enabled = _ask_bool("ingeschakeld?", default=acc.enabled)

    new_pw = ""
    if _ask_bool("password vervangen?", default=False):
        pw1 = getpass.getpass("nieuw password: ")
        pw2 = getpass.getpass("herhaal:       ")
        if pw1 != pw2:
            sys.exit("! wachtwoorden komen niet overeen — geannuleerd")
        new_pw = pw1

    print("\n--- SMTP (uitgaande mail) — leeg laten om uit te schakelen.")
    smtp_host = _ask("SMTP-host", default=acc.smtp_host or "")
    smtp_port = _ask_int("SMTP-port", acc.smtp_port) if smtp_host else acc.smtp_port
    smtp_starttls = _ask_bool("STARTTLS?", default=acc.smtp_use_starttls) if smtp_host else acc.smtp_use_starttls
    from_address = _ask("From-address",
                        default=acc.from_address or username) if smtp_host else None
    from_name = _ask("From-name",
                     default=acc.from_name or "") if smtp_host else ""

    new = ImapAccount(
        name=acc.name, label=label, host=host, port=port, ssl=ssl,
        username=username,
        folders=ImapFolders(inbox=inbox, sent=sent),
        enabled=enabled, poll_interval_seconds=poll,
        smtp_host=(smtp_host or None),
        smtp_port=smtp_port,
        smtp_use_starttls=smtp_starttls,
        from_address=from_address,
        from_name=(from_name or None),
    )
    if new_pw:
        set_password(new, new_pw)
    _save([new if a.name == acc.name else a for a in accounts])
    print(f"\n✓ account '{acc.name}' bijgewerkt.")


def cmd_remove(args: argparse.Namespace) -> None:
    accounts = _accounts()
    acc = _find(args.name)
    if acc is None:
        sys.exit(f"! geen account '{args.name}'")
    confirm = _ask(f"verwijder '{acc.name}' ({acc.username})? typ de naam ter bevestiging")
    if confirm != acc.name:
        sys.exit("! geannuleerd")
    delete_password(acc)
    _save([a for a in accounts if a.name != acc.name])
    print(f"✓ account '{acc.name}' verwijderd (yaml + Keychain).")


def cmd_test(args: argparse.Namespace) -> None:
    acc = _find(args.name)
    if acc is None:
        sys.exit(f"! geen account '{args.name}'")
    print(f"Verbinden met {acc.host}:{acc.port} als {acc.username} ...")
    try:
        info = _client(acc).test_connection()
    except Exception as exc:
        sys.exit(f"! login mislukt: {exc}")
    print(f"✓ login OK — {len(info['folders'])} folders gevonden.")


def cmd_folders(args: argparse.Namespace) -> None:
    acc = _find(args.name)
    if acc is None:
        sys.exit(f"! geen account '{args.name}'")
    print(f"Folders op {acc.name}:")
    for f in _client(acc).list_folders():
        marker = " ← inbox" if f == acc.folders.inbox else (" ← sent" if f == acc.folders.sent else "")
        print(f"  {f}{marker}")


def cmd_smtp_test(args: argparse.Namespace) -> None:
    """Verstuur een test-mail via SMTP naar het opgegeven adres."""
    acc = _find(args.name)
    if acc is None:
        sys.exit(f"! geen account '{args.name}'")
    if not acc.smtp_host:
        sys.exit(f"! account '{acc.name}' heeft geen SMTP geconfigureerd "
                 f"(run `imap_account.py edit {acc.name}` om SMTP toe te voegen).")
    from integrations.smtp_send import send_via_account
    to = args.to or acc.from_address or acc.username
    print(f"Stuur testmail via {acc.smtp_host}:{acc.smtp_port} → {to} ...")
    try:
        msgid = send_via_account(
            acc, to=to,
            subject="[PA-Agent] SMTP smoke-test",
            body=f"Dit is een SMTP-test vanaf {acc.name}.\n\nVerzonden door pa-agent.\n",
        )
    except Exception as exc:
        sys.exit(f"! SMTP send mislukt: {exc}")
    print(f"✓ verzonden (Message-ID={msgid or 'n/a'})")


def cmd_recent(args: argparse.Namespace) -> None:
    acc = _find(args.name)
    if acc is None:
        sys.exit(f"! geen account '{args.name}'")
    folder = args.folder
    limit = args.limit
    print(f"Laatste {limit} berichten in {folder or acc.folders.inbox} op {acc.name}:")
    print()
    for h in _client(acc).list_recent(folder=folder, limit=limit):
        marker = "  " if h.seen else "* "
        print(f"{marker}{h.date_iso[:16]:<17} {h.from_addr[:30]:<30} | {h.subject[:60]}")
    print()
    print("(* = ongelezen)")


# --- main -----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="IMAP-accountbeheer voor pa-agent.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("add").set_defaults(func=cmd_add)

    p_edit = sub.add_parser("edit"); p_edit.add_argument("name"); p_edit.set_defaults(func=cmd_edit)
    p_remove = sub.add_parser("remove"); p_remove.add_argument("name"); p_remove.set_defaults(func=cmd_remove)
    p_test = sub.add_parser("test"); p_test.add_argument("name"); p_test.set_defaults(func=cmd_test)
    p_folders = sub.add_parser("folders"); p_folders.add_argument("name"); p_folders.set_defaults(func=cmd_folders)

    p_recent = sub.add_parser("recent")
    p_recent.add_argument("name")
    p_recent.add_argument("folder", nargs="?", default=None)
    p_recent.add_argument("limit", nargs="?", type=int, default=10)
    p_recent.set_defaults(func=cmd_recent)

    p_smtp = sub.add_parser("smtp-test", help="stuur een testmail via SMTP")
    p_smtp.add_argument("name")
    p_smtp.add_argument("to", nargs="?", default=None,
                        help="bestemmingsadres (default = from_address)")
    p_smtp.set_defaults(func=cmd_smtp_test)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
