"""Replace email-prefix names in `config/vip_contacts.yaml` with the real
display-names that other people use when emailing those addresses.

Why: the /vip-suggester (mei 2026) populated `name` from email localpart,
so the daily briefing renders entries like `stephen.oneill — 342d stil`
instead of `Stephen O'Neill`. After redactor → Claude → reconstructor
round-trip the reader sees a handle, not a name.

The script reads display-names from `comm_items.from_addr` (which already
contains parsed "Display Name <email>" headers from past Gmail/IMAP ingest)
— no Gmail API calls needed.

Usage:
    python scripts/enrich_vip_names.py            # dry-run, prints diff
    python scripts/enrich_vip_names.py --apply    # writes YAML (with .bak)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from collections import Counter
from email.utils import parseaddr
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from core.config import load_settings  # noqa: E402
from core.perms import open_secure, secure_file  # noqa: E402

VIP_PATH = _REPO_ROOT / "config" / "vip_contacts.yaml"


def looks_like_email_handle(name: str) -> bool:
    """A 'name' that's really an email localpart — heuristic: lowercase,
    contains a dot or hyphen, no space, ≤30 chars. We deliberately err on the
    side of "enrich more" — you reviews the diff before write."""
    if not name or " " in name:
        return False
    if "@" in name:
        return True
    if len(name) > 30:
        return False
    if name != name.lower():
        return False
    return ("." in name) or ("-" in name) or name.isalpha()


def candidate_display_names(
    conn: sqlite3.Connection, email: str,
) -> list[tuple[str, int]]:
    """Return display-name candidates for an email, ranked by frequency."""
    rows = conn.execute(
        "SELECT from_addr, COUNT(*) AS c "
        "FROM comm_items WHERE from_addr LIKE ? "
        "GROUP BY from_addr ORDER BY c DESC",
        (f"%{email}%",),
    ).fetchall()
    counts: Counter[str] = Counter()
    for raw, c in rows:
        name, addr = parseaddr(raw)
        if not name or addr.lower() != email.lower():
            continue
        cleaned = name.strip().strip('"').strip("'").strip()
        # Strip trailing company/role tail: "Tim Graat | DOCUconcept",
        # "Bas Reijmers - GISB", "Joost Blonk (2.Orange)" → just the name.
        cleaned = re.split(r"\s*[|/]\s*", cleaned, maxsplit=1)[0]
        cleaned = re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)[0]
        cleaned = re.split(r"\s*\(", cleaned, maxsplit=1)[0]
        cleaned = cleaned.strip()
        if cleaned:
            counts[cleaned] += int(c)
    return counts.most_common()


def looks_like_real_name(s: str) -> bool:
    """Sanity-check the candidate before suggesting it.
    Accept: 'Stephen O'Neill', 'Jan Boer', 'J. Boer'. Reject: 'noreply',
    'support', single-word lowercase handles, role-style names."""
    if not s or len(s) < 2:
        return False
    if "@" in s:
        return False
    # Must contain at least one letter
    if not re.search(r"[A-Za-zÀ-ÿ]", s):
        return False
    role_words = {"noreply", "no-reply", "support", "info", "team", "billing",
                  "admin", "office", "contact", "notifications"}
    low = s.lower()
    for r in role_words:
        if r in low:
            return False
    # Single-word all-lowercase reject (likely the same email-handle problem
    # we're trying to fix)
    if s == low and " " not in s and "-" not in s and "." not in s:
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Write YAML (otherwise dry-run only).")
    ap.add_argument("--all", action="store_true",
                    help="Re-check every VIP, even those whose name already "
                         "looks like a real name. Default: only entries "
                         "that look like email-handles.")
    args = ap.parse_args()

    settings = load_settings()
    cfg = yaml.safe_load(VIP_PATH.read_text(encoding="utf-8"))
    people = cfg.get("people") or []

    proposals: list[tuple[int, str, str, list[tuple[str, int]]]] = []
    with sqlite3.connect(settings.db_path) as conn:
        for idx, p in enumerate(people):
            name = (p.get("name") or "").strip()
            emails = [e for e in (p.get("emails") or []) if e]
            if not emails:
                continue
            if not args.all and not looks_like_email_handle(name):
                continue
            # Try each email until we find a usable candidate
            best: str | None = None
            ranked: list[tuple[str, int]] = []
            for email in emails:
                cands = candidate_display_names(conn, email)
                ranked.extend(cands)
                for cand, _c in cands:
                    if looks_like_real_name(cand) and cand != name:
                        best = cand
                        break
                if best:
                    break
            if best:
                proposals.append((idx, name, best, ranked[:5]))

    if not proposals:
        print("No enrichments found. All VIP names already look human, or no "
              "display-names were found in comm_items.")
        return

    print(f"\nProposed renames ({len(proposals)}):\n")
    width = max(len(p[1]) for p in proposals)
    for idx, old, new, ranked in proposals:
        print(f"  [{idx:3d}]  {old:<{width}}  →  {new}")
        if len(ranked) > 1:
            others = ", ".join(f"{n} ({c})" for n, c in ranked[1:4])
            print(f"         alternates: {others}")

    if not args.apply:
        print("\n(dry-run — nothing written). Re-run with --apply to commit.")
        return

    # Backup first
    bak = VIP_PATH.with_suffix(".yaml.bak.enrich")
    shutil.copy2(VIP_PATH, bak)
    secure_file(bak)
    print(f"\nBackup written to {bak}")

    for idx, _old, new, _ranked in proposals:
        people[idx]["name"] = new

    cfg["people"] = people
    # open_secure → file is 0600 from creation; eliminates the
    # write_text + chmod race window where the file is briefly 0644.
    with open_secure(VIP_PATH, "w") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    print(f"Updated {VIP_PATH}")
    print(f"Applied {len(proposals)} name updates.")


if __name__ == "__main__":
    main()
