"""Wekelijkse uptime/downtime-digest naar the user.

Maandag-ochtend krijgt the user per iMessage een overzicht van vorige
week: per platform uptime %, totale downtime, aantal incidents, langste
incident, trend t.o.v. de week ervoor. Onderaan een per-incident-lijst
en eventueel een SLA-waarschuwing als uptime < drempel.

Bron: `uptime_events.kind = 'recovery'` met `detail = "downtime Ns"`.
Recovery events bevatten de canonical downtime-meting; we hoeven niet
zelf 'down'/'up' state-machines te draaien. Retention is 365 dagen
voor recovery-events, dus week-history werkt ruim.

Incidents die nog niet hersteld zijn op week-end vallen NIET in deze
week's rapport — die rollen door naar volgende week wanneer recovery
event komt. Cross-week incidents (down vóór week_start, recovery
binnen week) worden geclipt zodat we niet de hele incident-duur aan
deze week toerekenen.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

WEEK_SECONDS = 7 * 24 * 3600
_DOWNTIME_RE = re.compile(r"downtime\s+(\d+)\s*s")
NL_WEEKDAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
NL_MONTHS = [
    "", "jan", "feb", "mrt", "apr", "mei", "jun",
    "jul", "aug", "sep", "okt", "nov", "dec",
]

# M3 — cap voor per-incident lijst in de iMessage-render om
# onleesbaar-lange rapporten over jaarwindows te voorkomen.
DEFAULT_MAX_INCIDENT_LIST = 25


@dataclass
class Incident:
    target_name: str
    started_at: datetime     # local-tz
    duration_seconds: int    # may be clipped if cross-week
    raw_duration_seconds: int  # original from detail (uncropped)
    reason: str | None = None


@dataclass
class TargetStats:
    name: str
    uptime_pct: float
    downtime_seconds: int
    incident_count: int
    longest_incident: Incident | None
    incidents: list[Incident] = field(default_factory=list)
    prev_week_uptime_pct: float | None = None  # None als geen data

    @property
    def trend_diff(self) -> float | None:
        if self.prev_week_uptime_pct is None:
            return None
        return self.uptime_pct - self.prev_week_uptime_pct


def compute_window_stats(
    conn: sqlite3.Connection,
    target_name: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[float, int, list[Incident]]:
    """Bereken (uptime_pct, total_downtime_sec, incidents) voor één
    target over één window. Recovery-events binnen het window leveren
    de canonical incident-data. Cross-window incidents worden geclipt.
    """
    start_ts = int(window_start.timestamp())
    end_ts = int(window_end.timestamp())
    window_dur = end_ts - start_ts

    rows = conn.execute(
        """SELECT at, detail, error
           FROM uptime_events
           WHERE target_name = ? AND kind = 'recovery'
             AND at >= ? AND at < ?
           ORDER BY at ASC""",
        (target_name, start_ts, end_ts),
    ).fetchall()

    incidents: list[Incident] = []
    total_downtime = 0
    tz = window_start.tzinfo
    for row in rows:
        recovery_at = int(row[0])
        detail = str(row[1] or "")
        m = _DOWNTIME_RE.search(detail)
        if not m:
            continue
        raw_dur = int(m.group(1))
        incident_start_ts = recovery_at - raw_dur
        # Clip naar window: als incident vóór window_start startte,
        # tellen we alleen het gedeelte ín het window.
        effective_dur = raw_dur
        if incident_start_ts < start_ts:
            effective_dur = recovery_at - start_ts
        total_downtime += effective_dur
        incidents.append(Incident(
            target_name=target_name,
            started_at=datetime.fromtimestamp(incident_start_ts, tz=tz),
            duration_seconds=effective_dur,
            raw_duration_seconds=raw_dur,
            reason=_extract_reason(conn, target_name, incident_start_ts),
        ))

    # Cap totaal — kan niet meer dan het hele window zijn
    total_downtime = min(total_downtime, window_dur)
    uptime_pct = max(0.0, (window_dur - total_downtime) / window_dur * 100.0)
    return uptime_pct, total_downtime, incidents


def _extract_reason(
    conn: sqlite3.Connection,
    target_name: str,
    incident_start_ts: int,
) -> str | None:
    """Pak een korte 'reason' uit het 'down'-event rond incident-start
    (HTTP-status of netwerk-fout). Best-effort; None bij geen match."""
    row = conn.execute(
        """SELECT status_code, error FROM uptime_events
           WHERE target_name = ? AND kind = 'down'
             AND at BETWEEN ? AND ?
           ORDER BY at ASC LIMIT 1""",
        (target_name, incident_start_ts - 90, incident_start_ts + 90),
    ).fetchone()
    if not row:
        return None
    status, error = row[0], row[1]
    if status:
        return f"HTTP {int(status)}"
    if error:
        s = str(error)
        # Korte categorisatie — niet de hele exception-string
        if "timed out" in s.lower() or "timeout" in s.lower():
            return "timeout"
        if "name or service not known" in s.lower() or "nodename" in s.lower():
            return "DNS"
        if "ssl" in s.lower() or "tls" in s.lower():
            return "TLS"
        return "netwerk"
    return None


def compute_weekly_stats(
    db_path: Path,
    target_names: list[str],
    week_start: datetime,
    week_end: datetime,
    *,
    include_trend: bool = True,
) -> list[TargetStats]:
    """Per target: huidige-window + vorige-window stats. Returnt in
    dezelfde volgorde als `target_names`.

    Ondanks de naam ("weekly") werkt deze functie voor élke window-
    lengte — de scheduled digest geeft 7 dagen, de on-demand
    `uptime_report` tool geeft alles van 1 tot 730 dagen. De trend
    vergelijkt altijd tegen een prev-window van DEZELFDE lengte,
    zodat de uptime-percentages eerlijk vergelijkbaar zijn.

    Zie ook `compute_stats_with_trend` — alias voor leesbaarheid in
    nieuwe code.
    """
    window_duration = week_end - week_start
    prev_start = week_start - window_duration
    prev_end = week_start

    out: list[TargetStats] = []
    with sqlite3.connect(db_path) as conn:
        for name in target_names:
            pct, downtime, incidents = compute_window_stats(
                conn, name, week_start, week_end,
            )
            longest = max(
                incidents, key=lambda i: i.duration_seconds, default=None,
            )
            prev_pct = None
            if include_trend:
                prev_pct, _, _ = compute_window_stats(
                    conn, name, prev_start, prev_end,
                )
            out.append(TargetStats(
                name=name,
                uptime_pct=pct,
                downtime_seconds=downtime,
                incident_count=len(incidents),
                longest_incident=longest,
                incidents=incidents,
                prev_week_uptime_pct=prev_pct,
            ))
    return out


# M2 — leesbaarheidsalias voor on-demand call sites. Identieke semantiek;
# bestaande callers (scheduled digest + tests) blijven `compute_weekly_stats`
# gebruiken zonder breaking change.
compute_stats_with_trend = compute_weekly_stats


# ---- formatting --------------------------------------------------------

def _fmt_duration(seconds: int) -> str:
    """6m 18s / 1u 4m / 0s — kort, leesbaar."""
    if seconds <= 0:
        return "0s"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}u")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not hours:  # bij uren laten we seconden weg
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


def _fmt_trend(diff: float | None) -> str:
    """↑0.02% / ↓0.18% / → (geen verandering / geen data)."""
    if diff is None:
        return "→"
    if abs(diff) < 0.005:
        return "→"
    arrow = "↑" if diff > 0 else "↓"
    return f"{arrow}{abs(diff):.2f}%"


def _fmt_dt(dt: datetime, *, include_year: bool = False) -> str:
    """'di 19 mei 09:11' — kort, lokaal, ondubbelzinnig binnen 1 jaar.

    Voor windows die meerdere jaren overspannen: `include_year=True`
    geeft 'di 19 mei 2025 09:11'.
    """
    weekday = NL_WEEKDAYS[dt.weekday()]
    month = NL_MONTHS[dt.month]
    if include_year:
        return f"{weekday} {dt.day} {month} {dt.year} {dt.strftime('%H:%M')}"
    return f"{weekday} {dt.day} {month} {dt.strftime('%H:%M')}"


def _fmt_incident_line(i: Incident, *, include_year: bool = False) -> str:
    """Eén regel voor de incident-lijst."""
    when = _fmt_dt(i.started_at, include_year=include_year)
    dur = _fmt_duration(i.duration_seconds)
    reason = f"  {i.reason}" if i.reason else ""
    return f"  {when}  {i.target_name}  {dur}{reason}"


def format_imessage_report(
    stats: list[TargetStats],
    *,
    week_start: datetime,
    week_end: datetime,
    threshold_pct: float = 99.0,
    include_per_incident_list: bool = True,
    header_label: str | None = None,
    max_incident_list: int = DEFAULT_MAX_INCIDENT_LIST,
) -> str:
    """Render iMessage-vriendelijk overzicht. Geen markdown — the user
    leest dit in Messages.app.

    `header_label`: override voor de header-tekst (zonder leading
    emoji). Default: 'week NN — DD MMM – DD MMM YYYY'. Voor
    arbitrary windows (on-demand reports) geef bv.
    'afgelopen 30 dagen — 1 mei – 30 mei 2026'.
    """
    lines: list[str] = []

    if header_label is None:
        week_label = f"{week_start.strftime('%d %b')} – {(week_end - timedelta(seconds=1)).strftime('%d %b %Y')}"
        iso_week = week_start.isocalendar().week
        header_label = f"week {iso_week} — {week_label}"
    header_emoji = "✅" if all(s.uptime_pct >= 100.0 for s in stats) else "📈"
    lines.append(f"{header_emoji} Uptime {header_label}")
    lines.append("")

    # Year-disambiguation: voor windows die jaargrens overspannen tonen
    # we expliciet het jaartal in elke datum-stempel (anders kun je
    # "di 19 mei" niet onderscheiden tussen 2025 en 2026).
    include_year = (week_start.year != (week_end - timedelta(seconds=1)).year)

    # Per-platform compact block
    for s in stats:
        trend = _fmt_trend(s.trend_diff)
        lines.append(f"{s.name}    {s.uptime_pct:.2f}%   {trend}")
        if s.incident_count == 0:
            lines.append("  geen downtime · 0 incidents")
        else:
            dur = _fmt_duration(s.downtime_seconds)
            incident_word = "incident" if s.incident_count == 1 else "incidents"
            lines.append(f"  downtime  {dur} · {s.incident_count} {incident_word}")
            if s.longest_incident is not None:
                inc = s.longest_incident
                long_dur = _fmt_duration(inc.duration_seconds)
                when = _fmt_dt(inc.started_at, include_year=include_year)
                reason = f" {inc.reason}" if inc.reason else ""
                lines.append(f"  longest   {long_dur} ({when}{reason})")
        lines.append("")

    # Per-incident detail-lijst (uitgebreid format the user vroeg).
    # M3 — bij lange windows zou ongelimiteerd renderen ondermijnt
    # iMessage-leesbaarheid. We tonen LONGEST eerst tot max_incident_list
    # bereikt is, plus een footer met hoeveel weggelaten zijn.
    all_incidents = [
        i for s in stats for i in s.incidents
    ]
    if include_per_incident_list and all_incidents:
        lines.append("Incidents:")
        if len(all_incidents) <= max_incident_list:
            # Past in cap → chronologisch (natuurlijke leesvolgorde)
            shown = sorted(all_incidents, key=lambda i: i.started_at)
            for i in shown:
                lines.append(_fmt_incident_line(i, include_year=include_year))
        else:
            # Te lang → top-N op duur (impact-georden), dan chronologisch
            # binnen die top-N voor leesbaarheid
            top = sorted(
                all_incidents,
                key=lambda i: i.duration_seconds, reverse=True,
            )[:max_incident_list]
            shown = sorted(top, key=lambda i: i.started_at)
            remaining = len(all_incidents) - len(shown)
            for i in shown:
                lines.append(_fmt_incident_line(i, include_year=include_year))
            lines.append(
                f"  … en {remaining} kortere incidents niet getoond"
            )
        lines.append("")

    # SLA-flag voor platforms onder de drempel
    below = [s for s in stats if s.uptime_pct < threshold_pct]
    if below:
        for s in below:
            lines.append(
                f"⚠️ {s.name} onder {threshold_pct:.1f}% SLA-target "
                f"({s.uptime_pct:.2f}% deze week)"
            )

    return "\n".join(lines).rstrip()


def previous_week_window(now: datetime) -> tuple[datetime, datetime]:
    """Voor een 'now' op maandag (rapport-firetijd) returnt
    (week_start, week_end) van de WEEK DIE NET AFGELOPEN IS — dwz
    vorige maandag 00:00 t/m deze maandag 00:00.

    Op andere dagen returnt de meest recente complete week."""
    days_since_monday = now.weekday()
    this_monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days_since_monday,
    )
    last_monday = this_monday - timedelta(days=7)
    return last_monday, this_monday
