"""VIP-suggester: stel op basis van comm-historie kandidaten voor de
vip_contacts.yaml voor.

Score-logica:
- Bilateral score = min(in_count, out_count) — een hoge waarde betekent
  echte tweezijdige conversatie, niet alleen inkomende newsletters.
- Recency bonus = +X als laatste contact < 30 dagen.
- Volume normalized op log-schaal zodat één super-active sender niet
  alles domineert.

Filters:
- Skip eigen domeinen (settings.own_email_domains).
- Skip noreply/notifications/automated patterns.
- Min 5 messages totaal — anders geen signal.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from math import log
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from core.perms import open_secure

TZ = ZoneInfo("Europe/Amsterdam")

_NOREPLY_RE = re.compile(
    r"(?i)(?:^|[._+\-])(no-?reply|donotreply|notifications?|"
    r"alerts?|info|support|system|mailer|postmaster|"
    r"news|updates?|newsletter|marketing|admin|automated)(?:$|[._+\-@])"
)

# Publieke email-providers — wel valide klant-correspondenten op
# persoon-niveau, maar nooit als 'organisatie' (te veel verschillende
# users in zo'n bucket).
_PUBLIC_PROVIDERS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
    "hotmail.nl", "live.com", "live.nl", "yahoo.com", "yahoo.nl",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "ziggo.nl", "kpnmail.nl", "kpn.nl", "xs4all.nl", "planet.nl",
    "telfortglasvezel.nl", "tele2.nl", "online.nl", "casema.nl",
    "home.nl", "quicknet.nl", "upcmail.nl", "chello.nl", "vodafone.nl",
}


def suggest_vips(
    db_path: Path, *,
    own_domains: tuple[str, ...] = (),
    existing_emails: set[str] | None = None,
    existing_domains: set[str] | None = None,
    top_persons: int = 30,
    top_orgs: int = 15,
    days_back: int = 365,
    auto_detect_own: bool = True,
) -> dict[str, Any]:
    """Pak uit comm_items de top-N persons + top-N orgs als VIP-kandidaten.

    Eigen-domain detectie: kijk welke domeinen the user regelmatig zelf
    gebruikt als from-adres (direction=out). Die zijn per definitie z'n
    eigen — ook als settings.own_email_domains leeg is."""
    existing_emails = existing_emails or set()
    existing_domains = existing_domains or set()
    own = {d.lower() for d in own_domains}

    if auto_detect_own:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            for row in conn.execute("""
                SELECT LOWER(substr(from_addr, instr(from_addr, '@')+1)) as domain,
                       COUNT(*) n
                  FROM comm_items
                 WHERE direction='out' AND from_addr LIKE '%@%'
                   AND source IN ('gmail','imap')
                 GROUP BY domain
                HAVING n >= 5
            """).fetchall():
                d = row[0].strip()
                if d:
                    own.add(d)

    now = datetime.now(TZ)
    now_unix = int(now.timestamp())
    since = now_unix - days_back * 86400
    recent_cut = now_unix - 30 * 86400

    person_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"in": 0, "out": 0, "first": None, "last": None}
    )
    domain_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"in": 0, "out": 0, "first": None, "last": None,
                 "emails": set()}
    )

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute("""
            SELECT direction, from_addr, to_addrs, occurred_at
              FROM comm_items
             WHERE occurred_at >= ? AND source IN ('gmail','imap')
        """, (since,)):
            if r["direction"] == "in":
                addr = (r["from_addr"] or "").strip().lower()
            else:
                try:
                    arr = yaml.safe_load(r["to_addrs"]) or []
                    addr = (arr[0] if arr else "").strip().lower()
                except Exception:
                    addr = ""
            if not addr or "@" not in addr:
                continue
            email = _extract_email(addr)
            if not email:
                continue
            domain = email.split("@", 1)[-1]
            if domain in own:
                continue
            if _NOREPLY_RE.search(email):
                # skip noreply OOK voor org-aggregatie — die mails zijn
                # nooit VIP-relevant
                continue

            ts = int(r["occurred_at"] or 0)

            # Person bucket
            ps = person_stats[email]
            ps[r["direction"]] += 1
            ps["first"] = ts if ps["first"] is None else min(ps["first"], ts)
            ps["last"] = ts if ps["last"] is None else max(ps["last"], ts)

            # Domain bucket
            ds = domain_stats[domain]
            ds[r["direction"]] += 1
            ds["emails"].add(email)
            ds["first"] = ts if ds["first"] is None else min(ds["first"], ts)
            ds["last"] = ts if ds["last"] is None else max(ds["last"], ts)

    def _score(s: dict[str, Any]) -> float:
        total = s["in"] + s["out"]
        if total < 5:
            return 0.0
        bilateral = min(s["in"], s["out"])
        # Log volume + bilateral-bonus + recency bonus
        score = log(1 + total) + 2 * log(1 + bilateral)
        if s["last"] and s["last"] >= recent_cut:
            score += 1.5
        return score

    persons_ranked = []
    for email, s in person_stats.items():
        if email in existing_emails:
            continue
        sc = _score(s)
        if sc <= 0:
            continue
        persons_ranked.append({
            "email": email,
            "domain": email.split("@", 1)[-1],
            "in": s["in"], "out": s["out"], "total": s["in"] + s["out"],
            "bilateral": min(s["in"], s["out"]),
            "first": datetime.fromtimestamp(s["first"], TZ).date().isoformat() if s["first"] else "",
            "last": datetime.fromtimestamp(s["last"], TZ).date().isoformat() if s["last"] else "",
            "days_since": (now_unix - s["last"]) // 86400 if s["last"] else None,
            "score": round(sc, 2),
        })
    persons_ranked.sort(key=lambda r: r["score"], reverse=True)

    orgs_ranked = []
    for domain, s in domain_stats.items():
        if domain in existing_domains:
            continue
        if domain in _PUBLIC_PROVIDERS:
            continue  # gmail/outlook/etc — geen organisatie
        sc = _score(s)
        if sc <= 0:
            continue
        orgs_ranked.append({
            "domain": domain,
            "in": s["in"], "out": s["out"], "total": s["in"] + s["out"],
            "bilateral": min(s["in"], s["out"]),
            "uniq_emails": len(s["emails"]),
            "sample_emails": sorted(s["emails"])[:3],
            "first": datetime.fromtimestamp(s["first"], TZ).date().isoformat() if s["first"] else "",
            "last": datetime.fromtimestamp(s["last"], TZ).date().isoformat() if s["last"] else "",
            "days_since": (now_unix - s["last"]) // 86400 if s["last"] else None,
            "score": round(sc, 2),
        })
    orgs_ranked.sort(key=lambda r: r["score"], reverse=True)

    return {
        "persons": persons_ranked[:top_persons],
        "orgs": orgs_ranked[:top_orgs],
    }


def _extract_email(s: str) -> str | None:
    m = re.search(r"<([^<>@\s]+@[^<>@\s]+)>", s)
    if m:
        return m.group(1).lower()
    m = re.search(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", s)
    return m.group(1).lower() if m else None


def load_existing_vips(vip_path: Path) -> tuple[set[str], set[str]]:
    """Lees bestaande emails + domains uit yaml zodat suggester die overslaat."""
    if not vip_path.exists():
        return set(), set()
    try:
        cfg = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set(), set()
    emails: set[str] = set()
    for p in cfg.get("people") or []:
        if isinstance(p, dict):
            for e in (p.get("emails") or []):
                emails.add(str(e).lower())
    domains: set[str] = set()
    for o in cfg.get("organizations") or []:
        if isinstance(o, dict):
            for d in (o.get("domains") or []):
                domains.add(str(d).lower().lstrip("@"))
    return emails, domains


def append_to_yaml(
    vip_path: Path,
    *,
    new_persons: list[dict[str, Any]],
    new_orgs: list[dict[str, Any]],
) -> tuple[int, int]:
    """Voeg toe aan vip_contacts.yaml. Returns (persons_added, orgs_added).

    Gebruikt PyYAML round-trip — verliest comments. Maakt eerst een .bak
    voor de zekerheid.
    """
    if not new_persons and not new_orgs:
        return (0, 0)

    existing: dict[str, Any] = {}
    if vip_path.exists():
        # Backup — .bak inherits the original mode if we'd just copy or
        # write_text, so use open_secure for atomic 0600 birth
        # (SECURITY_REVIEW_2 MEDIUM-1).
        bak_path = vip_path.with_suffix(vip_path.suffix + ".bak")
        with open_secure(bak_path, "w") as fh:
            fh.write(vip_path.read_text(encoding="utf-8"))
        try:
            existing = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            existing = {}

    people = existing.get("people") or []
    orgs = existing.get("organizations") or []

    # Dedup safety: skip emails/domains die al in yaml zaten.
    existing_emails: set[str] = set()
    for p in people:
        if isinstance(p, dict):
            for e in (p.get("emails") or []):
                existing_emails.add(str(e).lower())
    existing_domains: set[str] = set()
    for o in orgs:
        if isinstance(o, dict):
            for d in (o.get("domains") or []):
                existing_domains.add(str(d).lower().lstrip("@"))

    added_p = 0
    for np in new_persons:
        if np["email"].lower() in existing_emails:
            continue
        people.append({
            "name": np["name"],
            "emails": [np["email"]],
            "tier": np["tier"],
            "relationship": np.get("relationship") or "",
        })
        existing_emails.add(np["email"].lower())
        added_p += 1

    added_o = 0
    for no in new_orgs:
        if no["domain"].lower() in existing_domains:
            continue
        orgs.append({
            "name": no["name"],
            "domains": [no["domain"]],
            "tier": no["tier"],
        })
        existing_domains.add(no["domain"].lower())
        added_o += 1

    existing["people"] = people
    existing["organizations"] = orgs

    # open_secure → born 0600, no race window for the umask-0o022 default.
    with open_secure(vip_path, "w") as fh:
        yaml.safe_dump(existing, fh, allow_unicode=True, sort_keys=False,
                        default_flow_style=False)
    return (added_p, added_o)
