"""On-demand uptime/downtime rapport — Claude tool zodat the user in
iMessage kan vragen "uptime laatste maand" / "rapport afgelopen 7
weken" / "hoe stond CMS in mei?" enzovoort.

Hergebruikt `compute_weekly_stats` + `format_imessage_report` uit
weekly_report.py. Verschil met de scheduled weekly digest: window is
parameterized en de header-label is aangepast.

Retention-grenzen (uit `schema.py.prune_old_events`):
- 'recovery' events: 365d (canonical downtime-bron)
- 'down' / 'up': 90d (gebruikt voor reason-extractie)

Voor windows > 365 dagen waarschuwt de tool dat data oudere events
kan missen — niet faalt, maar the user weet het.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.timezone import current_tz

from .weekly_report import compute_weekly_stats, format_imessage_report

log = logging.getLogger(__name__)

# Hard caps om misbruik / per-ongeluk-grote-windows te voorkomen
MAX_DAYS = 730  # 2 jaar — boven de recovery-retention
MIN_DAYS = 1
WARN_DAYS = 365  # boven dit punt: warning toevoegen


_ISO_DATE = "%Y-%m-%d"


def _parse_window(
    args: dict[str, Any], now: datetime,
) -> tuple[datetime, datetime, str]:
    """Parse `days` of `start_date`/`end_date` uit args naar
    (window_start, window_end, header_label). Raise ValueError bij
    ongeldige input."""
    days = args.get("days")
    start_date = args.get("start_date")
    end_date = args.get("end_date")

    if days is not None and (start_date or end_date):
        raise ValueError(
            "geef 'days' OF ('start_date' en 'end_date'), niet beide"
        )

    if days is not None:
        try:
            n_days = int(days)
        except (TypeError, ValueError):
            raise ValueError(f"'days' moet integer zijn, kreeg {days!r}")
        if n_days < MIN_DAYS:
            raise ValueError(f"'days' minimaal {MIN_DAYS}")
        if n_days > MAX_DAYS:
            raise ValueError(f"'days' maximaal {MAX_DAYS}")
        window_end = now.replace(microsecond=0)
        window_start = window_end - timedelta(days=n_days)
        # Vriendelijke label-zinnen voor common periods
        if n_days == 7:
            label_period = "afgelopen 7 dagen"
        elif n_days == 30:
            label_period = "afgelopen 30 dagen"
        elif n_days == 90:
            label_period = "afgelopen 90 dagen"
        elif n_days % 7 == 0:
            label_period = f"afgelopen {n_days // 7} weken"
        else:
            label_period = f"afgelopen {n_days} dagen"
        date_range = (
            f"{window_start.strftime('%d %b')} – "
            f"{window_end.strftime('%d %b %Y')}"
        )
        return window_start, window_end, f"{label_period} — {date_range}"

    if not (start_date and end_date):
        raise ValueError(
            "geef 'days' of beide 'start_date' + 'end_date' (YYYY-MM-DD)"
        )

    try:
        start_d = datetime.strptime(str(start_date), _ISO_DATE)
        end_d = datetime.strptime(str(end_date), _ISO_DATE)
    except ValueError as e:
        raise ValueError(f"datum-formaat moet YYYY-MM-DD zijn: {e}")
    if end_d <= start_d:
        raise ValueError("end_date moet ná start_date liggen")
    if (end_d - start_d).days > MAX_DAYS:
        raise ValueError(f"window groter dan {MAX_DAYS} dagen niet ondersteund")
    tz = now.tzinfo
    window_start = start_d.replace(tzinfo=tz)
    window_end = end_d.replace(tzinfo=tz)
    date_range = (
        f"{window_start.strftime('%d %b')} – "
        f"{window_end.strftime('%d %b %Y')}"
    )
    return window_start, window_end, date_range


def _target_names(db_path: Path) -> list[str]:
    """Pak target-namen uit uptime_checks (worker schrijft hier voor
    elke target in config). Sorteer op naam voor stabiele output."""
    with sqlite3.connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT name FROM uptime_checks ORDER BY name"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(r[0]) for r in rows]


def uptime_report_handler(
    db_path: Path, args: dict[str, Any],
) -> dict[str, Any]:
    """Tool-handler: bereken uptime-stats over een window en render
    naar iMessage-tekst die Claude in zijn antwoord kan tonen."""
    now = datetime.now(current_tz())
    try:
        window_start, window_end, header_label = _parse_window(args, now)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    targets = _target_names(db_path)
    if not targets:
        return {
            "ok": False,
            "error": "geen uptime-targets geconfigureerd "
                     "(config/uptime.yaml ontbreekt of leeg)",
        }

    target_filter = args.get("target")
    if target_filter:
        targets = [t for t in targets if t == target_filter]
        if not targets:
            return {
                "ok": False,
                "error": f"target {target_filter!r} niet bekend in uptime_checks",
            }

    include_incidents = bool(args.get("include_incidents", True))
    # M1 — defense-in-depth tegen garbage input van Claude. JSON-schema
    # restricts type, maar directe callers (tests, scripts) kunnen
    # alles meegeven. Fallback naar default i.p.v. crash.
    try:
        threshold = float(args.get("threshold_pct", 99.0))
    except (TypeError, ValueError):
        threshold = 99.0

    try:
        stats = compute_weekly_stats(
            db_path, targets, window_start, window_end,
            include_trend=bool(args.get("include_trend", True)),
        )
    except Exception as e:
        log.exception("uptime_report compute failed")
        return {"ok": False, "error": f"compute failed: {e}"}

    text = format_imessage_report(
        stats,
        week_start=window_start,
        week_end=window_end,
        threshold_pct=threshold,
        include_per_incident_list=include_incidents,
        header_label=header_label,
    )

    # Warning bij grote windows — recovery-retention is 365d
    days = (window_end - window_start).days
    if days > WARN_DAYS:
        text += (
            f"\n\n⚠️ Window van {days} dagen > {WARN_DAYS} dagen recovery-"
            "retention. Oudere incidents zijn mogelijk uit de history "
            "geprund en niet meegenomen."
        )

    return {
        "ok": True,
        "report": text,
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "days": days,
        },
        "targets": [
            {
                "name": s.name,
                "uptime_pct": round(s.uptime_pct, 4),
                "downtime_seconds": s.downtime_seconds,
                "incident_count": s.incident_count,
            }
            for s in stats
        ],
    }


# --- registratie ---------------------------------------------------------

UPTIME_HANDLERS = {
    "uptime_report": uptime_report_handler,
}

UPTIME_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "uptime_report",
        "description": (
            "On-demand uptime/downtime rapport over een tijdsperiode. "
            "Gebruik wanneer the user vraagt 'rapport afgelopen X', 'uptime "
            "laatste maand/week/kwartaal', 'hoe stond platform X in mei', "
            "of vergelijkbaar. Returnt een geformatteerd iMessage-rapport in "
            "het 'report' veld dat je LETTERLIJK aan the user kan tonen. "
            "Geef 'days' voor relatieve windows (7, 30, 49 voor 7 weken, "
            "365 voor jaar) OF expliciete 'start_date' + 'end_date' in "
            "YYYY-MM-DD format. Optionele 'target' filtert op één platform. "
            "Recovery-events zijn 365d retained — bij langere windows komt "
            "een waarschuwing onderaan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": MIN_DAYS,
                    "maximum": MAX_DAYS,
                    "description": (
                        "Aantal dagen terug vanaf nu. Voor 'afgelopen "
                        "week' = 7, 'afgelopen maand' ≈ 30, 'afgelopen 7 "
                        "weken' = 49, 'afgelopen kwartaal' ≈ 90."
                    ),
                },
                "start_date": {
                    "type": "string",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                    "description": "ISO datum YYYY-MM-DD (alleen samen met end_date).",
                },
                "end_date": {
                    "type": "string",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                    "description": "ISO datum YYYY-MM-DD (alleen samen met start_date).",
                },
                "target": {
                    "type": "string",
                    "description": "Optioneel: filter op één platform (exacte naam uit uptime config).",
                },
                "include_incidents": {
                    "type": "boolean",
                    "description": "Per-incident lijst onderaan. Default true.",
                },
                "include_trend": {
                    "type": "boolean",
                    "description": (
                        "Trend t.o.v. de voorgaande window van dezelfde "
                        "lengte. Default true."
                    ),
                },
                "threshold_pct": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 100.0,
                    "description": "SLA-drempel voor ⚠️-flag. Default 99.0.",
                },
            },
            "additionalProperties": False,
        },
    },
]
