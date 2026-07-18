"""Wekelijkse detector voor gedrags-trends.

Vijf detectoren werken puur over de bestaande SQLite-data — geen Claude
nodig, alleen aggregaties over comm_items / decisions / open_loops. De
output zijn `patterns`-rijen die dayclose surfaces. Bewust geen embeddings
of LLM-call: de signalen zijn statistisch en moeten goedkoop kunnen draaien.

Gerunnen vanuit scheduler tick — wekelijks (Monday 09:00 default). Werkt
idempotent dankzij UNIQUE(week_start, kind).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.llm_helpers import llm_short_text
from extensions.patterns.schema import insert_or_replace_pattern

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")


_NARRATIVE_PROMPT = """Je krijgt een gedetecteerd gedragspatroon uit the user's werkdata. Schrijf 1 zin (max 25 woorden) wat de meest waarschijnlijke oorzaak is en wat hij zou kunnen doen. Praktisch, geen jargon. Engels.

Geen disclaimers, geen 'mogelijk', geen herhaling van de cijfers. Alleen de inzicht-zin."""


def run_weekly_detection(
    db_path: Path, *, today: date | None = None,
    ollama: Any | None = None,
    settings: Any | None = None,
) -> list[dict[str, Any]]:
    """Voer alle detectoren uit voor de afgelopen volledige week.
    Returns list van patterns die nieuw waren of veranderden."""
    today = today or date.today()
    # "Vorige week" = week die afgelopen zondag eindigde.
    monday = today - timedelta(days=today.weekday())  # this week's Monday
    last_week_start = monday - timedelta(days=7)      # last week's Monday
    last_week_end = monday                             # exclusive

    week_start_ts = _to_ts(last_week_start)
    week_end_ts = _to_ts(last_week_end)
    # 4-week trailing baseline (excl deze week)
    baseline_start_ts = _to_ts(last_week_start - timedelta(weeks=4))

    out: list[dict[str, Any]] = []
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        for fn in (
            _detect_comm_volume_spike,
            _detect_decisions_slowing,
            _detect_stale_outgoing_rising,
            _detect_meeting_overload,
            _detect_focus_blocks_shrinking,
        ):
            try:
                p = fn(conn, week_start_ts=week_start_ts,
                        week_end_ts=week_end_ts,
                        baseline_start_ts=baseline_start_ts)
                if p is not None:
                    # Optional: enrich body met Llama-narrative.
                    if ollama is not None:
                        system_prompt = _NARRATIVE_PROMPT
                        if settings is not None:
                            from core.prompt_builder import render_system_prompt
                            system_prompt = render_system_prompt(
                                system_prompt, settings,
                            )
                        narrative = llm_short_text(
                            ollama, system=system_prompt,
                            user=f"Pattern: {p['title']}\nContext: {p['body']}",
                        )
                        if narrative:
                            p["body"] = (p["body"] + "\n\nInsight: " + narrative)
                    insert_or_replace_pattern(conn, week_start=week_start_ts,
                                                **p)
                    out.append({"week_start": week_start_ts, **p})
            except Exception:
                log.exception("pattern detector %s failed", fn.__name__)
    return out


def _to_ts(d: date) -> int:
    return int(datetime.combine(d, time(0, 0), tzinfo=TZ).timestamp())


# --- Individual detectors -------------------------------------------------

def _detect_comm_volume_spike(
    conn: sqlite3.Connection, *,
    week_start_ts: int, week_end_ts: int, baseline_start_ts: int,
) -> dict[str, Any] | None:
    """Inkomende comm_items deze week vs 4-week trailing avg.
    Trigger als deze week >= 1.5x baseline én >= 50 items totaal."""
    try:
        this_week = conn.execute(
            "SELECT COUNT(*) FROM comm_items WHERE direction='in' "
            "AND occurred_at >= ? AND occurred_at < ?",
            (week_start_ts, week_end_ts),
        ).fetchone()[0]
        baseline_total = conn.execute(
            "SELECT COUNT(*) FROM comm_items WHERE direction='in' "
            "AND occurred_at >= ? AND occurred_at < ?",
            (baseline_start_ts, week_start_ts),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    baseline_avg = baseline_total / 4 if baseline_total else 0
    if baseline_avg < 10 or this_week < 50:
        return None
    if this_week < baseline_avg * 1.5:
        return None
    pct = int(round((this_week / baseline_avg - 1) * 100))
    severity = "alert" if pct >= 100 else "watch"
    return {
        "kind": "comm_volume_spike",
        "severity": severity,
        "title": f"Inkomend mailvolume +{pct}% vs 4-week-gem",
        "body": (f"Deze week {this_week} inkomende items vs gemiddeld "
                  f"{baseline_avg:.0f}/wk. Mogelijk nieuwsletter-storm of "
                  "klant-escalatie — even scrollen in /audit waard."),
        "metric_value": float(this_week),
        "baseline_value": float(baseline_avg),
    }


def _detect_decisions_slowing(
    conn: sqlite3.Connection, *,
    week_start_ts: int, week_end_ts: int, baseline_start_ts: int,
) -> dict[str, Any] | None:
    """Beslissingen vertragen — 0-1 deze week vs trailing avg >= 3/wk."""
    try:
        this_week = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE decided_at >= ? AND decided_at < ?",
            (week_start_ts, week_end_ts),
        ).fetchone()[0]
        baseline_total = conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE decided_at >= ? AND decided_at < ?",
            (baseline_start_ts, week_start_ts),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    baseline_avg = baseline_total / 4
    if baseline_avg < 3 or this_week > 1:
        return None
    return {
        "kind": "decisions_slowing",
        "severity": "watch",
        "title": f"Beslissingen vertragen ({this_week} deze week, gem {baseline_avg:.1f})",
        "body": ("Vorige weken loste je gemiddeld {b:.1f} beslissingen per "
                  "week in. Deze week {n}. Iets blokkeert óf je hebt geen "
                  "moment voor strategische keuzes gemaakt — check open "
                  "loops met kind='outgoing_request'.").format(
            b=baseline_avg, n=this_week),
        "metric_value": float(this_week),
        "baseline_value": float(baseline_avg),
    }


def _detect_stale_outgoing_rising(
    conn: sqlite3.Connection, *,
    week_start_ts: int, week_end_ts: int, baseline_start_ts: int,
) -> dict[str, Any] | None:
    """Open outgoing_request loops > 7 dagen oud, count nu vs baseline.
    Niet historisch — actuele snapshot."""
    try:
        cutoff = week_end_ts - 7 * 86400
        stale_now = conn.execute(
            "SELECT COUNT(*) FROM open_loops "
            "WHERE status='open' AND kind='outgoing_request' "
            "AND created_at < ?",
            (cutoff,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    if stale_now < 4:
        return None
    severity = "alert" if stale_now >= 8 else "watch"
    return {
        "kind": "stale_outgoing_rising",
        "severity": severity,
        "title": f"{stale_now} oude verzoeken wachten op antwoord (>7d)",
        "body": ("Lijstje wat je hebt uitstaan zonder reactie groeit. "
                  "Either follow-uppen of accepteren dat het niet komt — "
                  "check loops_open(kind='outgoing_request')."),
        "metric_value": float(stale_now),
        "baseline_value": None,
    }


def _detect_meeting_overload(
    conn: sqlite3.Connection, *,
    week_start_ts: int, week_end_ts: int, baseline_start_ts: int,
) -> dict[str, Any] | None:
    """Heuristiek via comm_items als proxy voor meeting-density: telt
    het aantal Plaud-transcripts (meetings die werden opgenomen) deze
    week. >=5 transcripts/week = veel meetings.
    Echte agenda-data zit niet in lokale DB (alleen Google API)."""
    try:
        meetings = conn.execute(
            "SELECT COUNT(*) FROM plaud_transcripts "
            "WHERE recorded_at >= ? AND recorded_at < ?",
            (week_start_ts, week_end_ts),
        ).fetchone()[0]
        baseline_total = conn.execute(
            "SELECT COUNT(*) FROM plaud_transcripts "
            "WHERE recorded_at >= ? AND recorded_at < ?",
            (baseline_start_ts, week_start_ts),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    baseline_avg = baseline_total / 4
    if meetings < 5 or meetings < baseline_avg * 1.5:
        return None
    return {
        "kind": "meeting_overload",
        "severity": "watch",
        "title": f"{meetings} meetings deze week (gem {baseline_avg:.1f})",
        "body": ("Veel gesprekken; dit eet focus-tijd. Prik volgende week "
                  "bewust 1-2 deep-work blokken in de agenda voordat ze "
                  "vol lopen."),
        "metric_value": float(meetings),
        "baseline_value": float(baseline_avg),
    }


def _detect_focus_blocks_shrinking(
    conn: sqlite3.Connection, *,
    week_start_ts: int, week_end_ts: int, baseline_start_ts: int,
) -> dict[str, Any] | None:
    """Snoezig signaal: response-time op inkomende vragen wordt langzamer
    (proxy voor 'minder ruimte om dingen op te pakken'). Vergelijk
    median(resolved_at - created_at) van open_loops resolved deze week
    vs trailing baseline. Trigger als deze week >= 2x baseline."""
    try:
        this_resp = conn.execute(
            "SELECT resolved_at - created_at FROM open_loops "
            "WHERE resolved_at IS NOT NULL AND status='done' "
            "AND resolved_at >= ? AND resolved_at < ? "
            "AND kind IN ('incoming_question','incoming_task')",
            (week_start_ts, week_end_ts),
        ).fetchall()
        baseline_resp = conn.execute(
            "SELECT resolved_at - created_at FROM open_loops "
            "WHERE resolved_at IS NOT NULL AND status='done' "
            "AND resolved_at >= ? AND resolved_at < ? "
            "AND kind IN ('incoming_question','incoming_task')",
            (baseline_start_ts, week_start_ts),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if len(this_resp) < 5 or len(baseline_resp) < 10:
        return None
    median_this = _median([r[0] for r in this_resp])
    median_base = _median([r[0] for r in baseline_resp])
    if median_base <= 0 or median_this < median_base * 2:
        return None
    return {
        "kind": "focus_blocks_shrinking",
        "severity": "watch",
        "title": (f"Antwoord-tijd verdubbelt: ~{median_this // 3600}u nu "
                   f"vs ~{median_base // 3600}u eerder"),
        "body": ("Het kost je nu meer dan twee keer zo lang om vragen af "
                  "te handelen vergeleken met de afgelopen weken. Mogelijk "
                  "te veel context-switching — overweeg batching of focus-"
                  "blok in de ochtend."),
        "metric_value": float(median_this),
        "baseline_value": float(median_base),
    }


def _median(values: list[int | float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2
