"""Drie daily nudges voor the user's '3 bedrijven per dag'-doel.

Morgen   — geeft 3 concrete suggesties (top-3 selectie) zodat hij geen
            keuzevraagstuk heeft bij het begin van de dag.
Middag   — check-in op progressie: 0/1/2/3+ contacten gemaakt vandaag.
            Toont resterende suggesties als nog niet gehaald.
Avond    — dagevaluatie: wie er vandaag geraakt is + reflectie of doel
            is gehaald.

Telling: tellen we OUTBOUND touchpoints van vandaag (channel in
{email_out, linkedin, call, meeting, plaud}). Auto-gelogde email_in
(reply ontvangen) telt NIET als "ik heb iemand benaderd".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import core.timezone as _tz  # late-bound zodat test-monkey-patches doorwerken

from .briefing import compute_sales_pulse


_OUTBOUND_CHANNELS = ("email_out", "linkedin", "call", "meeting", "plaud")

# Target → label voor iMessage-output
_TARGET_LABELS = {
    "adl_video":    "ADL",
    "dst_connect":  "DST",
    "ds_templates": "DS",
    "multi":        "MULTI",
}


def _today_window(tz_name: str | None = None) -> tuple[int, int]:
    """Returnt (start_of_today_unix, start_of_tomorrow_unix) in lokale TZ."""
    now = _tz.now_local()
    start = datetime.combine(now.date(), time(0, 0, 0), tzinfo=now.tzinfo)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def count_outbound_today(db_path: Path) -> tuple[int, list[dict[str, Any]]]:
    """Hoeveel unieke accounts heeft the user vandaag benaderd?
    Returnt (account_count, lijst met namen+target+kanaal voor weergave)."""
    start, end = _today_window()
    placeholders = ",".join("?" for _ in _OUTBOUND_CHANNELS)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT DISTINCT a.id, a.naam, a.target, "
            f"       (SELECT channel FROM sales_touchpoints t2 "
            f"        WHERE t2.account_id = a.id "
            f"          AND t2.occurred_at >= ? AND t2.occurred_at < ? "
            f"          AND t2.channel IN ({placeholders}) "
            f"        ORDER BY t2.occurred_at DESC LIMIT 1) AS channel "
            f"FROM sales_accounts a "
            f"WHERE EXISTS ("
            f"  SELECT 1 FROM sales_touchpoints t "
            f"  WHERE t.account_id = a.id "
            f"    AND t.occurred_at >= ? AND t.occurred_at < ? "
            f"    AND t.channel IN ({placeholders}) "
            f") "
            f"ORDER BY a.naam",
            (start, end, *_OUTBOUND_CHANNELS,
             start, end, *_OUTBOUND_CHANNELS),
        ).fetchall()
    contacted = [
        {"id": r["id"], "naam": r["naam"], "target": r["target"],
         "channel": r["channel"]}
        for r in rows
    ]
    return len(contacted), contacted


def _target_label(t: str) -> str:
    return _TARGET_LABELS.get(t, t)


def _top_n_suggestions(db_path: Path, n: int) -> list[dict[str, Any]]:
    pulse, _ = compute_sales_pulse(db_path)
    return list(pulse.get("top_three", []))[:n]


def build_morning_nudge(db_path: Path, target_count: int = 3) -> str:
    """Eerste reminder: 3 concrete suggesties + doel-statement."""
    suggestions = _top_n_suggestions(db_path, n=target_count)
    lines = [f"🎯 Doel vandaag: {target_count} bedrijven benaderen."]
    if not suggestions:
        lines.append("")
        lines.append(
            "Geen kandidaten in pipeline. Voeg accounts toe of zet "
            "bestaande prospects op nurturing/kansrijk."
        )
        return "\n".join(lines)

    lines.append("Suggesties:")
    for i, s in enumerate(suggestions, 1):
        target = _target_label(s.get("target", ""))
        lines.append(f"{i}. {s.get('naam')} [{target}] — {s.get('reason', '')}")
        suggestion = s.get("suggestion")
        if suggestion:
            lines.append(f"   → {suggestion}")
    lines.append("")
    lines.append(
        "Log via iMessage: 'ik heb [bedrijf] gemaild/gebeld/gesproken'."
    )
    return "\n".join(lines)


def build_midday_nudge(db_path: Path, target_count: int = 3) -> str:
    """Check-in op progressie + suggesties voor wat nog te doen."""
    count, contacted = count_outbound_today(db_path)
    remaining = max(0, target_count - count)

    if count >= target_count:
        lines = [
            f"✅ Doel gehaald: {count}/{target_count} bedrijven benaderd vandaag.",
            "",
        ]
        for c in contacted[:target_count]:
            lines.append(
                f"• {c['naam']} [{_target_label(c['target'])}] — {c['channel']}"
            )
        if count > target_count:
            lines.append("")
            lines.append(
                f"(Bonus: +{count - target_count} extra contacten gemaakt.)"
            )
        return "\n".join(lines)

    head = (
        f"📈 Sales midday-check: {count}/{target_count} bedrijven benaderd."
    )
    lines = [head, ""]
    if contacted:
        lines.append("Vandaag al:")
        for c in contacted:
            lines.append(
                f"• {c['naam']} [{_target_label(c['target'])}] — {c['channel']}"
            )
        lines.append("")
    lines.append(
        f"Nog {remaining} te gaan voor je dagdoel. Suggesties:"
    )
    suggestions = _top_n_suggestions(db_path, n=remaining)
    if not suggestions:
        lines.append("(Geen open kandidaten meer — overweeg een koud "
                      "account te promoveren naar nurturing.)")
        return "\n".join(lines)
    contacted_ids = {c["id"] for c in contacted}
    shown = 0
    for s in suggestions:
        if s.get("id") in contacted_ids:
            continue
        target = _target_label(s.get("target", ""))
        lines.append(
            f"• {s.get('naam')} [{target}] — {s.get('reason', '')}"
        )
        sug = s.get("suggestion")
        if sug:
            lines.append(f"  → {sug}")
        shown += 1
        if shown >= remaining:
            break
    return "\n".join(lines)


def build_evening_nudge(db_path: Path, target_count: int = 3) -> str:
    """Dagevaluatie."""
    count, contacted = count_outbound_today(db_path)
    if count >= target_count:
        head = f"🌙 Dagsluiting sales: {count}/{target_count} ✅"
        tail = "Doel gehaald — morgen weer 3."
    elif count > 0:
        head = f"🌙 Dagsluiting sales: {count}/{target_count}"
        tail = (
            f"Net niet — {target_count - count} kort. Morgen weer een "
            "frisse start."
        )
    else:
        head = f"🌙 Dagsluiting sales: 0/{target_count}"
        tail = (
            "Niemand benaderd vandaag. Geen drama, maar morgen telt "
            "weer met 3. Top-3 staat klaar om 09:00."
        )

    lines = [head, ""]
    if contacted:
        lines.append("Vandaag geraakt:")
        for c in contacted:
            lines.append(
                f"• {c['naam']} [{_target_label(c['target'])}] — {c['channel']}"
            )
        lines.append("")
    lines.append(tail)
    return "\n".join(lines)
