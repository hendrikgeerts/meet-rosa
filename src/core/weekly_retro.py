"""Weekly retrospective — zaterdag 09:00 reflectie op de afgelopen week.

Niet hetzelfde als weekend_prep (zondag, vooruit-kijkend). Deze kijkt
ACHTERUIT: comm-volume, patronen, delegations, sales-snapshot, ge-
closede decisions/loops. Doel: één compact iMessage waar the user op een
zaterdag-ochtend zijn week begrijpt zonder doorklikken.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from datetime import timedelta
from pathlib import Path
from typing import Any

from core.timezone import now_local
from privacy.gateway import Gateway

log = logging.getLogger(__name__)


WEEKLY_RETRO_PROMPT = """You are Rosa, the user's personal assistant. You write his Saturday-morning retrospective — short, bulleted, no bold formatting, English.

Tone: gentle reflection, not anxiety. He's having coffee on a Saturday; this is "here's what your week looked like, what stood out, where you're hanging." Not a to-do list.

- Opening: one short line — "Week wrap — N mails, N meetings, X closed" using counters from context.
- 📊 Volume snapshot: 1-2 lines from `comm_volume.mails_in`, `comm_volume.mails_out`, `comm_volume.slack_in`, `comm_volume.meetings_held`. Mention if there's a striking imbalance ("80% of replies on Monday — Tuesday was quiet").
- 🎯 Closed this week: from `closed_count.loops` (open_loops resolved this week) + `closed_count.decisions` if non-zero. One line celebrating progress.
- ⏳ Still hanging: from `still_open` — top 3 oldest open inbound loops (mail/Slack/Plaud-action). Format: "  {age}d — {title} ({source})".
- 🤝 Delegations status: from `delegations_summary` — N waiting on others, M overdue (followup_at < now). If overdue >0: highlight as "you might want to follow up on these".
- 💼 Sales pulse: from `sales_summary` — N hot, M nurturing, X closed-won/lost this week. Skip if all zeros.
- 🔍 Patterns surfaced: from `patterns` (max 2 items) — Llama-detected behavioral or volume trends.
- Close: "Have a good weekend. 🌿" or short variant. No motivational fluff.
- Skip any section that's empty. Don't say "no items" — just leave it out."""


def collect_weekly_retro_context(
    *,
    db_path: Path,
) -> dict[str, Any]:
    now = now_local()
    week_start = now - timedelta(days=7)
    week_start_ts = int(week_start.timestamp())
    now_ts = int(now.timestamp())

    # Comm volume per week
    comm_volume = _collect_comm_volume(db_path, week_start_ts, now_ts)

    # Closed loops + decisions
    closed_count = _collect_closed_counts(db_path, week_start_ts, now_ts)

    # Top-3 stale open loops
    still_open = _collect_still_open(db_path, limit=3)

    # Delegations summary
    delegations_summary = _collect_delegations_summary(db_path, now_ts)

    # Sales pulse-lite (deze week)
    sales_summary = _collect_sales_summary(db_path, week_start_ts, now_ts)

    # Patterns recent
    patterns = _collect_recent_patterns(db_path, week_start_ts)

    return {
        "now": now.isoformat(),
        "week_start": week_start.isoformat(),
        "comm_volume": comm_volume,
        "closed_count": closed_count,
        "still_open": still_open,
        "delegations_summary": delegations_summary,
        "sales_summary": sales_summary,
        "patterns": patterns,
    }


def _collect_comm_volume(db_path: Path, start_ts: int, end_ts: int) -> dict[str, int]:
    out = {
        "mails_in": 0, "mails_out": 0,
        "slack_in": 0, "slack_out": 0,
        "meetings_held": 0,
    }
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT "
                "SUM(CASE WHEN source IN ('gmail','imap') AND direction='in' THEN 1 ELSE 0 END) AS mi, "
                "SUM(CASE WHEN source IN ('gmail','imap') AND direction='out' THEN 1 ELSE 0 END) AS mo, "
                "SUM(CASE WHEN source='slack' AND direction='in' THEN 1 ELSE 0 END) AS si, "
                "SUM(CASE WHEN source='slack' AND direction='out' THEN 1 ELSE 0 END) AS so "
                "FROM comm_items WHERE occurred_at >= ? AND occurred_at < ?",
                (start_ts, end_ts),
            ).fetchone()
            if row:
                out["mails_in"] = int(row["mi"] or 0)
                out["mails_out"] = int(row["mo"] or 0)
                out["slack_in"] = int(row["si"] or 0)
                out["slack_out"] = int(row["so"] or 0)
            mrow = conn.execute(
                "SELECT COUNT(*) FROM plaud_transcripts "
                "WHERE recorded_at >= ? AND recorded_at < ?",
                (start_ts, end_ts),
            ).fetchone()
            if mrow:
                out["meetings_held"] = int(mrow[0])
    except sqlite3.OperationalError:
        pass
    return out


def _collect_closed_counts(db_path: Path, start_ts: int, end_ts: int) -> dict[str, int]:
    out = {"loops": 0, "decisions": 0}
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM open_loops "
                "WHERE status='done' AND resolved_at >= ? AND resolved_at < ?",
                (start_ts, end_ts),
            ).fetchone()
            if row:
                out["loops"] = int(row[0] or 0)
            try:
                drow = conn.execute(
                    "SELECT COUNT(*) FROM decisions "
                    "WHERE decided_at >= ? AND decided_at < ?",
                    (start_ts, end_ts),
                ).fetchone()
                if drow:
                    out["decisions"] = int(drow[0] or 0)
            except sqlite3.OperationalError:
                pass
    except sqlite3.OperationalError:
        pass
    return out


def _collect_still_open(db_path: Path, *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from extensions.open_loops.schema import list_open
        now_ts = int(_time.time())
        with sqlite3.connect(db_path) as conn:
            for kind in ("incoming_question", "incoming_task",
                         "meeting_action_self"):
                for r in list_open(conn, kind=kind, limit=10):
                    age_days = (now_ts - int(r.get("created_at") or now_ts)) // 86400
                    out.append({
                        "id": r["id"], "kind": r["kind"],
                        "title": r.get("action_summary") or r.get("title"),
                        "source": r.get("source"),
                        "age_days": age_days,
                    })
        out.sort(key=lambda r: r["age_days"], reverse=True)
    except Exception:
        log.exception("retro: still_open fetch failed")
    return out[:limit]


def _collect_delegations_summary(db_path: Path, now_ts: int) -> dict[str, int]:
    out = {"waiting_total": 0, "overdue": 0}
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM open_loops WHERE status='open' "
                "AND kind IN ('outgoing_request','meeting_action_other')"
            ).fetchone()
            if row:
                out["waiting_total"] = int(row[0] or 0)
            orow = conn.execute(
                "SELECT COUNT(*) FROM open_loops WHERE status='open' "
                "AND kind IN ('outgoing_request','meeting_action_other') "
                "AND followup_at IS NOT NULL AND followup_at <= ?",
                (now_ts,),
            ).fetchone()
            if orow:
                out["overdue"] = int(orow[0] or 0)
    except sqlite3.OperationalError:
        pass
    return out


def _collect_sales_summary(db_path: Path, start_ts: int, end_ts: int) -> dict[str, int]:
    out = {"hot": 0, "nurturing": 0, "won_this_week": 0, "lost_this_week": 0}
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status='warm' THEN 1 ELSE 0 END) AS hot, "
                "SUM(CASE WHEN status='nurturing' THEN 1 ELSE 0 END) AS nu, "
                "SUM(CASE WHEN status='won' AND won_at >= ? AND won_at < ? THEN 1 ELSE 0 END) AS w, "
                "SUM(CASE WHEN status='lost' AND lost_at >= ? AND lost_at < ? THEN 1 ELSE 0 END) AS l "
                "FROM sales_accounts",
                (start_ts, end_ts, start_ts, end_ts),
            ).fetchone()
            if row:
                out["hot"] = int(row["hot"] or 0)
                out["nurturing"] = int(row["nu"] or 0)
                out["won_this_week"] = int(row["w"] or 0)
                out["lost_this_week"] = int(row["l"] or 0)
    except sqlite3.OperationalError:
        pass
    return out


def _collect_recent_patterns(db_path: Path, start_ts: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, body FROM patterns "
                "WHERE detected_at >= ? "
                "ORDER BY detected_at DESC LIMIT 2",
                (start_ts,),
            ).fetchall()
            out = [dict(r) for r in rows]
    except sqlite3.OperationalError:
        pass
    return out


def generate_weekly_retro(
    *,
    gateway: Gateway,
    db_path: Path,
    settings: Any | None = None,
) -> str:
    context = collect_weekly_retro_context(db_path=db_path)
    user_payload = (
        "Context (JSON):\n"
        + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de weekly retrospective."
    )
    system = WEEKLY_RETRO_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    response = gateway.complete(
        task="weekly_retro",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=900,
    )
    parts = [
        b.text for b in response.content
        if getattr(b, "type", None) == "text"
    ]
    return "".join(parts).strip() or "(weekly retro was leeg)"
