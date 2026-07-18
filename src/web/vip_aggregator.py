"""VIP-relationship-monitor — aggregator voor /vip dashboard.

Per VIP uit vip_contacts.yaml: dagen sinds laatste contact, volume deze
week, baseline (gem. afgelopen 4 weken), volume-trend %, en alert-flag.

Match-strategie:
- Persons: matchen via `emails` (exact from_addr/to_addrs) of via
  domain als de mail van dat domein komt EN de organisatie ook
  geconfigureerd is.
- Organizations: matchen via `domains` — alle mail van/naar `*@domain`.

Per VIP returnt de aggregator één rij met alle signalen. Sortering
in de template: tier A eerst, dan oplopend op days-silent.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

TZ = ZoneInfo("Europe/Amsterdam")


def build_vip_snapshot(
    db_path: Path, vip_path: Path,
) -> dict[str, Any]:
    """Bouw VIP-monitor data. Returns {'vips': [...], 'has_config': bool}."""
    if not vip_path.exists():
        return {"vips": [], "has_config": False, "reason": "vip_contacts.yaml ontbreekt"}

    try:
        cfg = yaml.safe_load(vip_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {"vips": [], "has_config": False, "reason": "yaml parse error"}

    people = cfg.get("people") or []
    orgs = cfg.get("organizations") or []
    if not people and not orgs:
        return {"vips": [], "has_config": False,
                "reason": "geen `people` of `organizations` gedefinieerd"}

    now = datetime.now(TZ)
    now_unix = int(now.timestamp())
    week_start = now_unix - 7 * 86400
    baseline_start = now_unix - 28 * 86400  # afgelopen 4 weken

    out: list[dict[str, Any]] = []

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row

        for p in people:
            if not isinstance(p, dict):
                continue
            name = p.get("name") or ""
            emails = [e.lower() for e in (p.get("emails") or []) if e]
            if not name or not emails:
                continue
            stats = _stats_for_addresses(
                conn, emails=emails, domains=[],
                now_unix=now_unix, week_start=week_start,
                baseline_start=baseline_start,
            )
            tier = (p.get("tier") or "C").upper()
            out.append({
                "kind": "person",
                "name": name,
                "tier": tier,
                "match_on": ", ".join(emails),
                "relationship": p.get("relationship") or "",
                **stats,
                "flag": _flag(stats, tier),
            })

        for o in orgs:
            if not isinstance(o, dict):
                continue
            name = o.get("name") or ""
            domains = [d.lower().lstrip("@") for d in (o.get("domains") or []) if d]
            if not name or not domains:
                continue
            stats = _stats_for_addresses(
                conn, emails=[], domains=domains,
                now_unix=now_unix, week_start=week_start,
                baseline_start=baseline_start,
            )
            tier = (o.get("tier") or "C").upper()
            out.append({
                "kind": "org",
                "name": name,
                "tier": tier,
                "match_on": ", ".join(f"@{d}" for d in domains),
                "relationship": o.get("relationship") or "",
                **stats,
                "flag": _flag(stats, tier),
            })

    # Sort: alert-flag eerst, dan tier A→C, dan oplopend op days_silent.
    flag_rank = {"alert": 0, "warn": 1, "ok": 2, "never": 3}
    tier_rank = {"A": 0, "B": 1, "C": 2}
    out.sort(key=lambda r: (
        flag_rank.get(r["flag"], 9),
        tier_rank.get(r["tier"], 9),
        9999 if r["days_silent"] is None else -r["days_silent"],
    ))

    return {"vips": out, "has_config": True}


def _stats_for_addresses(
    conn: sqlite3.Connection, *,
    emails: list[str], domains: list[str],
    now_unix: int, week_start: int, baseline_start: int,
) -> dict[str, Any]:
    """Query comm_items voor de gegeven email-set + domains."""
    # Bouw WHERE-clause: (from_addr in emails) OR (json_extract(to_addrs,'$[0]') in emails)
    #   OR (from_addr LIKE '%@dom' OR json_extract(to_addrs,'$[0]') LIKE '%@dom')
    clauses: list[str] = []
    params: list[Any] = []
    for e in emails:
        clauses.append("LOWER(from_addr) = ?")
        params.append(e)
        clauses.append("LOWER(json_extract(to_addrs,'$[0]')) = ?")
        params.append(e)
    for d in domains:
        like = f"%@{d}"
        clauses.append("LOWER(from_addr) LIKE ?")
        params.append(like)
        clauses.append("LOWER(json_extract(to_addrs,'$[0]')) LIKE ?")
        params.append(like)
    if not clauses:
        return {
            "last_contact_at": None, "days_silent": None,
            "week_count": 0, "baseline_per_week": 0.0,
            "trend_pct": 0,
        }
    where = " OR ".join(clauses)

    last = conn.execute(
        f"SELECT MAX(occurred_at) FROM comm_items WHERE {where}",
        params,
    ).fetchone()[0]
    days_silent = ((now_unix - last) // 86400) if last else None

    week_n = conn.execute(
        f"SELECT COUNT(*) FROM comm_items WHERE occurred_at >= ? AND ({where})",
        [week_start, *params],
    ).fetchone()[0]

    baseline_n = conn.execute(
        f"SELECT COUNT(*) FROM comm_items "
        f"WHERE occurred_at >= ? AND occurred_at < ? AND ({where})",
        [baseline_start, week_start, *params],
    ).fetchone()[0]
    baseline_per_week = baseline_n / 3.0 if baseline_n else 0.0  # 3 weken in 4-1

    trend_pct = 0
    if baseline_per_week > 0:
        trend_pct = int(((week_n - baseline_per_week) / baseline_per_week) * 100)

    return {
        "last_contact_at": last,
        "days_silent": days_silent,
        "week_count": week_n,
        "baseline_per_week": round(baseline_per_week, 1),
        "trend_pct": trend_pct,
    }


def _flag(stats: dict[str, Any], tier: str) -> str:
    """Bepaal alert-niveau o.b.v. tier + silence + trend."""
    days = stats["days_silent"]
    trend = stats["trend_pct"]
    baseline = stats["baseline_per_week"]
    if days is None:
        return "never"  # nooit contact gehad

    # Silence-drempel afhankelijk van tier
    silence_alert = {"A": 14, "B": 30, "C": 60}.get(tier, 60)
    silence_warn = {"A": 7, "B": 14, "C": 30}.get(tier, 30)
    if days >= silence_alert:
        return "alert"
    # Stijl-shift: was actief, nu drastisch minder
    if baseline >= 2.0 and trend <= -50:
        return "alert"
    if days >= silence_warn:
        return "warn"
    if baseline >= 2.0 and trend <= -25:
        return "warn"
    return "ok"
