"""Top-3 selectie-algoritme voor de daily briefing.

Prioriteit:
  1. Open offertes >5d zonder beweging (alle targets, hoogste urgentie)
  2. Triggers gisteren+vandaag binnengekomen, nog niet geserveerd
  3. Cadence-vervallen accounts in nurturing/kansrijk
  4. Eén koud slot met een trigger gekoppeld
  5. Diversificatie: max 2 uit dezelfde target

Deterministisch — zelfde DB-state + zelfde tijd geeft zelfde top-3.
Geen randomisatie (the user wil "vandaag dezelfde 3 als wat Rosa
vanochtend mailde").
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import _row_to_dict


@dataclass(frozen=True)
class TopAccount:
    account: dict[str, Any]
    reason_code: str       # urgent_offerte / trigger_today / cadence_overdue / cold_with_trigger
    reason_text: str       # leesbare uitleg voor briefing
    suggestion: str        # concrete actie-suggestie
    related_trigger_id: int | None = None


def select_top_n(
    db_path: Path, *, n: int = 3, max_per_target: int = 2,
    now_unix: int | None = None,
) -> list[TopAccount]:
    now = int(now_unix) if now_unix else int(time.time())
    candidates: list[TopAccount] = []
    seen_ids: set[int] = set()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Auto-unsnooze before selecting
        from .storage import unsnooze_expired
        with sqlite3.connect(db_path, isolation_level=None) as wconn:
            unsnooze_expired(wconn)

        # 1. Open offertes met laatste touch >5d geleden
        five_days_ago = now - 5 * 86400
        rows = conn.execute(
            "SELECT * FROM sales_accounts "
            "WHERE status = 'offerte' "
            "  AND (last_touch_at IS NULL OR last_touch_at < ?) "
            "ORDER BY COALESCE(last_touch_at, 0) ASC LIMIT ?",
            (five_days_ago, n * 2),
        ).fetchall()
        for r in rows:
            acc = _row_to_dict(r)
            days = _days_since(acc.get("last_touch_at"), now)
            candidates.append(TopAccount(
                account=acc,
                reason_code="urgent_offerte",
                reason_text=f"Offerte open, {days}d zonder beweging",
                suggestion="Bel of mail om peilen — offerte zit te lang op tafel",
            ))
            seen_ids.add(acc["id"])

        # 2. Onbenutte triggers van laatste 48u, gekoppeld aan account
        recent_cutoff = now - 2 * 86400
        rows = conn.execute(
            "SELECT t.*, a.id AS aid, a.naam AS anaam, a.target AS atarget, "
            "       a.status AS astatus, a.last_touch_at AS alast "
            "FROM sales_triggers t "
            "JOIN sales_accounts a ON t.account_id = a.id "
            "WHERE t.consumed_at IS NULL AND t.occurred_at >= ? "
            "ORDER BY t.occurred_at DESC LIMIT ?",
            (recent_cutoff, n * 2),
        ).fetchall()
        for r in rows:
            if r["aid"] in seen_ids:
                continue
            acc = _account_summary_from_join(r)
            candidates.append(TopAccount(
                account=acc,
                reason_code="trigger_today",
                reason_text=f"Nieuw signaal: {r['title'] or r['source']}",
                suggestion="Reach out met verwijzing naar dit signaal — context is vers",
                related_trigger_id=int(r["id"]),
            ))
            seen_ids.add(acc["id"])

        # 3. Cadence-vervallen kansrijk/nurturing
        rows = conn.execute(
            "SELECT * FROM sales_accounts "
            "WHERE status IN ('kansrijk','nurturing') "
            "  AND next_touch_at IS NOT NULL AND next_touch_at < ? "
            "ORDER BY "
            "  CASE status WHEN 'kansrijk' THEN 0 ELSE 1 END, "
            "  next_touch_at ASC LIMIT ?",
            (now, n * 2),
        ).fetchall()
        for r in rows:
            if r["id"] in seen_ids:
                continue
            acc = _row_to_dict(r)
            days = _days_overdue(acc.get("next_touch_at"), now)
            candidates.append(TopAccount(
                account=acc,
                reason_code="cadence_overdue",
                reason_text=(
                    f"{acc['status'].capitalize()} — cadence "
                    f"{days}d voorbij" if days > 0
                    else f"{acc['status'].capitalize()} — cadence vandaag"
                ),
                suggestion=_default_suggestion_for(acc),
            ))
            seen_ids.add(acc["id"])

        # 4. Koude accounts opvullen — tot we n slots hebben gevuld.
        # Eerst koude met VERVALLEN next_touch (cadence verlopen) en
        # daarna koude die net nooit getouched (oudste eerst). Voor de
        # bulk-import situatie (200 vers geïmporteerd, next_touch in
        # toekomst) is dit het pad dat de nudges-suggesties voedt.
        slots_needed = n - len(candidates)
        if slots_needed > 0:
            rows = conn.execute(
                "SELECT * FROM sales_accounts "
                "WHERE status = 'koud' "
                "ORDER BY "
                "  CASE WHEN next_touch_at IS NOT NULL AND next_touch_at < ? "
                "       THEN 0 ELSE 1 END, "
                "  COALESCE(estimated_value_eur, 0) DESC, "
                "  COALESCE(last_touch_at, created_at) ASC "
                "LIMIT ?",
                (now, slots_needed * 3),
            ).fetchall()
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                acc = _row_to_dict(r)
                overdue = (
                    acc.get("next_touch_at") is not None
                    and acc["next_touch_at"] < now
                )
                candidates.append(TopAccount(
                    account=acc,
                    reason_code="cold_outreach",
                    reason_text=(
                        "Koud account — cadence vervallen, opnieuw aan tafel"
                        if overdue else
                        "Koud account — tijd voor eerste touch"
                    ),
                    suggestion=_default_suggestion_for(acc),
                ))
                seen_ids.add(acc["id"])

    # Diversificatie: max_per_target
    diversified: list[TopAccount] = []
    target_count: dict[str, int] = {}
    for cand in candidates:
        t = cand.account["target"]
        if target_count.get(t, 0) >= max_per_target:
            continue
        diversified.append(cand)
        target_count[t] = target_count.get(t, 0) + 1
        if len(diversified) >= n:
            break

    # Fallback: als diversificatie te streng is en we hebben <n, vul aan
    if len(diversified) < n:
        for cand in candidates:
            if cand not in diversified:
                diversified.append(cand)
                if len(diversified) >= n:
                    break
    return diversified[:n]


def mark_triggers_consumed(db_path: Path, trigger_ids: list[int]) -> None:
    """Markeer dat een trigger in de briefing van vandaag is verschenen
    zodat hij morgen niet opnieuw als 'nieuw' komt."""
    if not trigger_ids:
        return
    placeholders = ",".join("?" for _ in trigger_ids)
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.execute(
            f"UPDATE sales_triggers SET consumed_at = strftime('%s','now') "
            f"WHERE id IN ({placeholders})",
            trigger_ids,
        )


# ---- helpers ---------------------------------------------------------

def _days_since(ts: int | None, now_unix: int) -> int:
    if not ts:
        return 999
    return max(0, (now_unix - int(ts)) // 86400)


def _days_overdue(ts: int | None, now_unix: int) -> int:
    if not ts:
        return 0
    return max(0, (now_unix - int(ts)) // 86400)


def _default_suggestion_for(acc: dict[str, Any]) -> str:
    target = acc.get("target", "")
    prospect_type = acc.get("prospect_type", "")
    base_by_target = {
        "adl_video": "LinkedIn-post over recent ADL-project + tag contactpersoon",
        "dst_connect": "Vraag of ze nog AV-klanten hebben die CMS zoeken — YourProduct kort intro",
        "ds_templates": "Demo-link of API-changelog delen — geeft aanleiding tot gesprek",
        "multi": "Cross-sell touch — koppel aan recent project",
    }
    by_type = {
        "av_reseller": "Stuur partner-update of laatste co-marketing case",
        "cms_vendor": "Korte mail over API-roadmap of integratie-mogelijkheid",
        "end_customer": "Persoonlijke check-in + casus uit hun sector",
    }
    if prospect_type and prospect_type in by_type:
        return by_type[prospect_type]
    return base_by_target.get(target, "Korte touch — vraag hoe het loopt, deel iets relevants")


def _account_summary_from_join(r: sqlite3.Row) -> dict[str, Any]:
    """Voor de trigger-join die niet de volle account-row teruggeeft."""
    return {
        "id": r["aid"],
        "naam": r["anaam"],
        "target": r["atarget"],
        "status": r["astatus"],
        "last_touch_at": r["alast"],
    }
