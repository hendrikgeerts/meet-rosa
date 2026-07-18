"""Dagelijkse dagafsluiting (default 20:00 Europe/Amsterdam).

Spiegelt `briefings.py` qua structuur — context-verzameling lokaal,
content-generatie via `gateway.complete()` (privacy-laag actief).

Wat er vandaag gebeurde wordt afgeleid uit:
  - Agenda-events met start_time vandaag (al gepasseerd of bezig)
  - Reminders die vandaag zijn afgevuurd (sent_at op vandaag)
  - iMessage-gespreksturns van vandaag (uit conversation_turns)
  - Plaud-transcripts vandaag ingegest

Wat morgen op de agenda staat:
  - Agenda-events met start_time morgen
  - Reminders ingepland voor morgen

Comm-intel data (mail/slack samenvattingen) zal automatisch meegenomen
worden zodra die extensie draait — collect_dayclose_context vraagt dan
extra summary-rijen op uit dezelfde db.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.decisions.schema import recent_decisions as _recent_decisions
from extensions.okrs.loader import load_okrs, to_briefing_snapshot
from extensions.open_loops.schema import list_open
from extensions.patterns.schema import mark_surfaced, pending_patterns
from integrations.gcal import CalendarClient
from integrations.gmail import GmailClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
from core.timezone import now_local
TZ = ZoneInfo("Europe/Amsterdam")


DAYCLOSE_PROMPT = """You are Rosa, the user's personal assistent. You write his end-of-day wrap at 8 pm. He reads this as iMessage:
- Short and bulleted, no bold formatting.
- Start with one short opening line ("Day's wrap" or "Evening the user —").
- ✅ What happened today: calendar events (with time), reminders that fired, transcripts that were processed, brief mention of iMessage conversations.
- ⚠️ Open loops: items from mail/Slack that the user still needs to respond to. Per item: gebruik `action` als die er is (1-zin Llama-extract — concrete vraag), anders title. Format: "  who — action  (Nd, due X)". Toon max 5, oldest first. Skip de sectie als open_loops_inbound leeg is. Bij `overdue: true`: prefix met 🔴.
- ⏳ Stale waiting (uit `stale_outgoing_requests`): items waar the user X dagen geleden iemand iets heeft gevraagd en nog geen antwoord heeft. Per item: gebruik `action` (concrete-actie-zin) als die er is, anders title. Format: "  who — action  (Xd geleden)". Suggest 1 follow-up reminder per stale item. Skip de sectie als leeg.
- 📌 For tomorrow: calendar events with time, reminders scheduled, deadlines.
- 🧠 Worth remembering: 1-2 things from today's conversations/mail worth noting (new contacts, status changes). Skip if nothing special.
- 📓 Decisions logged today: from `decisions_today` — list each with title + 1-line context. Skip if empty.
- 🎯 OKR pulse: if okrs is non-empty AND today's events / decisions / closed loops moved a key result, mention it ("→ moved 'kr1: enterprise klanten' from 2 to 3"). Otherwise skip the section. Don't repeat OKR list — only progress signals.
- 🔍 Patterns: list each item from `patterns_pending` as one short line ("Inkomend mailvolume +85% deze week — wellicht een nieuwsletter-storm"). Use the pattern's title verbatim als kernzin, body als 1-zin context. Skip if list empty.
- 🌟 Open config-wishes: from `config_wishes_open` — the user's structurele preferences die nog niet zijn afgehandeld. Per item: id + title (max 5). Format: "  #12 Voortaan briefings in NL". Skip de sectie als list leeg. Doel: voorkomen dat preferences ondersneeuwen.
- ❓ Unrecorded wishes today: from `unrecorded_wish_candidates`. Items that sounded like a structural wish in chat but were never persisted via add_config_wish. Per item, one line with the excerpt and ask if it really was a wish. Format: "  Earlier you said: '{content_excerpt}' — did you mean that as a wish? Say yes and I'll save it." Skip the section if the list is empty. Do NOT call add_config_wish yourself — wait for confirmation. Safety net for the missed-tool-call bug.
- 🔴 VIP-alerts: from `vip_alerts` (max 3) — strategische klanten die te lang stil zijn of waar het volume drastisch gedaald is. Per item: name + days_silent + flag-reden. Format: "  [tier] {name} — {days}d stil" (alert) of "  [tier] {name} — volume -{X}% (was ~{baseline}/wk)" (trend-shift). Skip de sectie als list leeg.
- Close with "Sleep well. 🌙" or a short variant.
- If the day was quiet: say so briefly too — no filler."""


def collect_dayclose_context(
    *,
    gmail: GmailClient,
    calendar: CalendarClient,
    db_path: Path,
    okrs_path: Path | None = None,
) -> dict[str, Any]:
    now = now_local()
    start_of_today = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
    start_of_tomorrow = start_of_today + timedelta(days=1)
    end_of_tomorrow = start_of_tomorrow + timedelta(days=1)

    # Today (passed events)
    try:
        events_today = calendar.list_events(
            time_min=start_of_today, time_max=now, max_results=50,
        )
    except Exception:
        log.exception("dayclose: calendar today fetch failed")
        events_today = []

    # Tomorrow
    try:
        events_tomorrow = calendar.list_events(
            time_min=start_of_tomorrow, time_max=end_of_tomorrow, max_results=50,
        )
    except Exception:
        log.exception("dayclose: calendar tomorrow fetch failed")
        events_tomorrow = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Reminders that fired today
        rows = conn.execute(
            "SELECT id, remind_at, body FROM reminders "
            "WHERE sent_at IS NOT NULL "
            "AND sent_at >= ? AND sent_at < ? "
            "ORDER BY remind_at ASC",
            (int(start_of_today.timestamp()), int(start_of_tomorrow.timestamp())),
        ).fetchall()
        reminders_fired = [
            {"id": r["id"], "at": datetime.fromtimestamp(r["remind_at"], now.tzinfo).isoformat(), "body": r["body"]}
            for r in rows
        ]

        # Reminders scheduled for tomorrow
        rows = conn.execute(
            "SELECT id, remind_at, body FROM reminders "
            "WHERE sent_at IS NULL AND cancelled_at IS NULL "
            "AND remind_at >= ? AND remind_at < ? "
            "ORDER BY remind_at ASC",
            (int(start_of_tomorrow.timestamp()), int(end_of_tomorrow.timestamp())),
        ).fetchall()
        reminders_tomorrow = [
            {"id": r["id"], "at": datetime.fromtimestamp(r["remind_at"], now.tzinfo).isoformat(), "body": r["body"]}
            for r in rows
        ]

        # iMessage conversation turns from today (compact)
        rows = conn.execute(
            "SELECT role, content, created_at FROM conversation_turns "
            "WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC",
            (int(start_of_today.timestamp()), int(start_of_tomorrow.timestamp())),
        ).fetchall()
        imessage_turns = [
            {"role": r["role"], "content": r["content"][:300]}
            for r in rows
        ]

        # Plaud transcripts ingested today (titles only — body is local)
        try:
            rows = conn.execute(
                "SELECT id, title, recorded_at FROM plaud_transcripts "
                "WHERE ingested_at >= ? AND ingested_at < ? "
                "ORDER BY recorded_at DESC",
                (int(start_of_today.timestamp()), int(start_of_tomorrow.timestamp())),
            ).fetchall()
            plaud_today = [{"id": r["id"], "title": r["title"]} for r in rows]
        except sqlite3.OperationalError:
            plaud_today = []

        # Decisions logged today
        try:
            decisions_today = _recent_decisions(conn, days=1, limit=10)
        except sqlite3.OperationalError:
            decisions_today = []

        # Open loops — split by direction so the prompt can render them in
        # twee aparte secties (the user moet reageren vs. wacht op antwoord).
        try:
            loops_inbound = [
                _loop_to_dict(l, now) for l in list_open(
                    conn, days_back=30, limit=5,
                ) if l["kind"] in ("incoming_question", "incoming_task",
                                    "meeting_action_self")
            ]
            loops_waiting = [
                _loop_to_dict(l, now) for l in list_open(
                    conn, kind="outgoing_request", days_back=30, limit=5,
                )
            ]
            # Stale: the user vroeg iemand iets >7 dagen geleden, geen antwoord.
            # Subset van loops_waiting met age_days >= 7 — voor follow-up suggestie.
            stale_outgoing = [
                l for l in loops_waiting if l.get("age_days", 0) >= 7
            ]
        except sqlite3.OperationalError:
            loops_inbound = []
            loops_waiting = []
            stale_outgoing = []

    okrs_snapshot: list[dict[str, Any]] = []
    if okrs_path is not None:
        try:
            okrs_snapshot = to_briefing_snapshot(load_okrs(okrs_path))
        except Exception:
            log.exception("dayclose: okrs load failed")

    patterns_to_show: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            patterns_to_show = pending_patterns(conn, limit=2)
            if patterns_to_show:
                mark_surfaced(conn, [p["id"] for p in patterns_to_show])
    except sqlite3.OperationalError:
        # patterns-table bestaat niet (eerste boot na migratie zonder
        # init_patterns_schema). Dayclose blijft werken.
        pass

    # Open config-wishes: laat the user niet vergeten wat hij heeft gevraagd
    # te onthouden. Skipt sectie wanneer leeg.
    open_wishes: list[dict[str, Any]] = []
    try:
        from extensions.config_wishes.schema import list_wishes as _list_wishes
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            open_w = _list_wishes(conn, status="open", limit=5)
            wip_w = _list_wishes(conn, status="wip", limit=5)
            open_wishes = (open_w + wip_w)[:5]
    except sqlite3.OperationalError:
        pass

    # Wish-audit: scan vandaag's chat-history op patronen die wel een
    # wish lijken te uiten maar geen add_config_wish-call hebben
    # getriggered. Vangnet voor de "Genoteerd!"-zonder-persistence bug.
    unrecorded_wishes: list[dict[str, Any]] = []
    try:
        from extensions.config_wishes.audit import (
            find_unrecorded_wish_candidates,
        )
        unrecorded_wishes = find_unrecorded_wish_candidates(
            db_path, since=start_of_today, until=now, limit=3,
        )
    except Exception:
        log.exception("dayclose: wish-audit failed")

    # VIP relationship-alerts: top-3 stilste/dalende klanten zodat ze
    # niet uit beeld raken. Vraagt vip_contacts.yaml + comm_items.
    vip_alerts: list[dict[str, Any]] = []
    try:
        from web.vip_aggregator import build_vip_snapshot
        vip_path = db_path.parent.parent / "config" / "vip_contacts.yaml"
        vip_snap = build_vip_snapshot(db_path, vip_path)
        if vip_snap.get("has_config"):
            alerts_only = [v for v in vip_snap["vips"]
                           if v["flag"] in ("alert", "warn")]
            vip_alerts = alerts_only[:3]
    except Exception:
        log.exception("dayclose: vip snapshot failed")

    return {
        "now": now.isoformat(),
        "weekday": now.strftime("%A"),
        "date": now.date().isoformat(),
        "events_today_passed": events_today,
        "events_tomorrow": events_tomorrow,
        "reminders_fired_today": reminders_fired,
        "reminders_tomorrow": reminders_tomorrow,
        "imessage_turns_today": imessage_turns,
        "plaud_transcripts_today": plaud_today,
        "open_loops_inbound": loops_inbound,
        "open_loops_waiting": loops_waiting,
        "stale_outgoing_requests": stale_outgoing,
        "decisions_today": decisions_today,
        "okrs": okrs_snapshot,
        "patterns_pending": patterns_to_show,
        "config_wishes_open": open_wishes,
        "unrecorded_wish_candidates": unrecorded_wishes,
        "vip_alerts": vip_alerts,
    }


def _loop_to_dict(row: Any, now: datetime) -> dict[str, Any]:
    # action_summary kolom kan ontbreken in oude rows of niet-gemigreerde DBs.
    action = None
    try:
        action = row["action_summary"]
    except (KeyError, IndexError):
        pass
    out = {
        "kind": row["kind"],
        "who": row["who"],
        "title": row["title"],
        "action": action,        # 1-zin Llama-extract — toon dit ipv title
        "age_days": (int(now.timestamp()) - row["created_at"]) // 86400,
    }
    # Deadline-info als de row een due_at heeft (uit deadline-parser).
    try:
        due = row["due_at"]
        if due:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _Zi
            tz = _Zi("Europe/Amsterdam")
            out["due"] = _dt.fromtimestamp(int(due), tz).strftime("%Y-%m-%d %H:%M")
            out["overdue"] = int(due) < int(now.timestamp())
    except (KeyError, IndexError):
        pass
    return out


def generate_dayclose(
    *,
    gateway: Gateway,
    gmail: GmailClient,
    calendar: CalendarClient,
    db_path: Path,
    okrs_path: Path | None = None,
    settings: Any | None = None,
) -> str:
    context = collect_dayclose_context(
        gmail=gmail, calendar=calendar, db_path=db_path, okrs_path=okrs_path,
    )
    user_payload = (
        "Context (JSON):\n" + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        + "\n\nSchrijf de dagafsluiting."
    )
    system = DAYCLOSE_PROMPT
    if settings is not None:
        from core.prompt_builder import render_system_prompt
        system = render_system_prompt(system, settings)
    # force_label='internal' (2/7): zie briefings.py — voorkom classifier-
    # keyword-trigger → lokaal Llama → scheduler-block → health-SIGTERM.
    response = gateway.complete(
        task="dayclose",
        system=system,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=1024,
        force_label="internal",
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip() or "(dagafsluiting was leeg)"
