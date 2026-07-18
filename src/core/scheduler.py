"""Background scheduler thread: fires reminders, sends morning briefings,
sends evening dayclose, scans the Plaud inbox. Runs alongside the main
iMessage poll loop."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from datetime import timedelta

from core.audit import prune_old as audit_prune_old
from core.briefings import (
    generate_briefing, next_fire_time, next_fire_time_with_catchup,
)
from core.config import Settings
from core.ceo_letter import generate_ceo_letter
from core.dayclose import generate_dayclose
from core.midday import generate_midday
from core.scheduler_state import get_last_fired, set_last_fired
from extensions.english_practice.reminder import generate_english_reminder
from extensions.expenses.scan import scan_inbox as scan_expenses_inbox
from extensions.market_intel.digest import generate_market_digest
from extensions.meeting_prep.check import tick as meeting_prep_tick
from extensions.patterns.detector import run_weekly_detection
from extensions.comm_intel.schema import prune_old_comm_items
from extensions.expenses.schema import prune_old_expenses
from extensions.travel_alerts.check import tick as travel_alerts_tick
from extensions.travel_alerts.schema import prune_old_locations
from extensions.sales.schema import prune_inactive_cold_accounts
from extensions.uptime.schema import prune_old_events as prune_old_uptime_events
from integrations.here_maps import HereMapsClient
from extensions import reminders
from integrations import plaud
from integrations.gcal import CalendarClient
from core.timezone import active_tz
from integrations.gmail import GmailClient
from models.ollama import OllamaClient
from privacy.gateway import Gateway

log = logging.getLogger(__name__)
# Fallback / display-default. Werkelijke TZ wordt per-call resolved via
# `_tz()` zodat een "rosa tz America/Los_Angeles" iMessage-commando
# meteen impact heeft op alle fire-times en rendering.
TZ = ZoneInfo("Europe/Amsterdam")


class Scheduler(threading.Thread):
    def __init__(
        self,
        *,
        settings: Settings,
        gateway: Gateway,
        gmail: GmailClient,
        calendar: CalendarClient,
        send_imessage: Callable[[str, str], None],
        stop_event: threading.Event,
        morning_extras_yaml: Path | None = None,
        ollama: OllamaClient | None = None,
        here: HereMapsClient | None = None,
        health_state: Any | None = None,        # core.health.HealthState
        vip_path: Path | None = None,           # voor birthdays in briefing
        okrs_path: Path | None = None,          # voor OKR-snapshot in briefing/dayclose
        uptime_config_path: Path | None = None,  # voor wekelijkse uptime-report
        gmail_address: str = "",                 # voor meeting-prep: skip self-as-attendee
        todoist_client: Any = None,              # integrations.todoist.TodoistClient — voor briefing/midday pulse
        todoist_project_id: str | None = None,
    ) -> None:
        super().__init__(name="pa-scheduler", daemon=True)
        self._settings = settings
        self._gateway = gateway
        self._gmail = gmail
        self._calendar = calendar
        self._send = send_imessage
        self._stop_event = stop_event
        self._morning_extras_yaml = morning_extras_yaml
        self._ollama = ollama
        self._here = here
        self._health_state = health_state
        self._vip_path = vip_path
        self._okrs_path = okrs_path
        self._uptime_config_path = uptime_config_path
        self._gmail_address = gmail_address
        self._todoist_client = todoist_client
        self._todoist_project_id = todoist_project_id
        # Retry-counter per job_name (briefing/midday/dayclose/ceo_letter)
        # voor _handle_job_failure
        self._retry_count: dict[str, int] = {}

        # Initial schedule-build + track de TZ-naam zodat _tick een
        # TZ-switch kan detecteren en de fire-tijden herberekenen.
        # Zonder dit blijven _next_* gebonden aan de oude TZ en arriveert
        # de "morning briefing" op de UTC-equivalent van 07:00 NL —
        # midden in de avond op PT/Asian timezones (review C1).
        self._last_tz_name: str = ""
        now = datetime.now(self._tz())
        self._next_travel_check = (
            now.timestamp() if settings.travel_alerts_enabled and here else None
        )
        self._next_meeting_prep_check = (
            now.timestamp() if settings.meeting_prep_enabled else None
        )
        self._next_expense_scan = (
            now.timestamp() if settings.expenses_enabled else None
        )
        self._next_plaud_scan = now.timestamp()
        self._next_audit_prune = now.timestamp()
        self._next_location_prune = now.timestamp()
        self._next_db_prune = now.timestamp()
        # Delegation-followup tick — één keer per 6u.
        self._next_delegation_followup = now.timestamp()
        # Niet TZ-afhankelijk maar wel periodic — init hier.
        self._next_pattern_detect = _next_weekly_fire(now, target_weekday=0,
                                                       hhmm="09:00")
        self._next_briefing = None
        self._next_dayclose = None
        self._next_midday = None
        self._next_market_digest = None
        self._next_ceo_letter = None
        self._next_english_practice = None
        self._next_uptime_report = None
        self._next_weekend_prep = None
        self._next_weekly_retro = None
        self._next_duplicate_scan = None
        self._next_sales_morning = None
        self._next_sales_midday = None
        self._next_sales_evening = None
        self._reset_user_schedule(now)

    def _reset_user_schedule(self, now: datetime) -> None:
        """(Re)bereken alle TZ-afhankelijke fire-tijden vanuit `now`.
        Wordt aangeroepen bij init én bij elke gedetecteerde TZ-switch
        (review C1). Catch-up logica respecteert last_fired zodat een
        TZ-switch midden in de werkdag niet plotseling een tweede
        briefing van vandaag triggert."""
        s = self._settings
        last_briefing = self._last_fired("briefing")
        last_midday = self._last_fired("midday")
        last_dayclose = self._last_fired("dayclose")
        last_english = self._last_fired("english_practice")

        self._next_briefing = (
            next_fire_time_with_catchup(
                now, s.briefing_weekday_time, s.briefing_weekend_time,
                last_fired=last_briefing,
            ) if s.briefing_enabled else None
        )
        self._next_dayclose = (
            next_fire_time_with_catchup(
                now, s.dayclose_time, s.dayclose_time,
                last_fired=last_dayclose,
            ) if s.dayclose_enabled else None
        )
        self._next_midday = (
            next_fire_time_with_catchup(
                now, s.midday_time, s.midday_time,
                last_fired=last_midday,
            ) if s.midday_enabled else None
        )
        self._next_market_digest = (
            _next_weekly_fire(now, s.market_intel_weekday, s.market_intel_time)
            if s.market_intel_enabled else None
        )
        self._next_ceo_letter = (
            _next_weekly_fire(now, s.ceo_letter_weekday, s.ceo_letter_time)
            if s.ceo_letter_enabled else None
        )
        self._next_uptime_report = (
            _next_weekly_fire(now, s.uptime_report_weekday, s.uptime_report_time)
            if s.uptime_report_enabled else None
        )
        self._next_weekend_prep = (
            _next_weekly_fire(now, s.weekend_prep_weekday, s.weekend_prep_time)
            if s.weekend_prep_enabled else None
        )
        self._next_weekly_retro = (
            _next_weekly_fire(now, s.weekly_retro_weekday, s.weekly_retro_time)
            if s.weekly_retro_enabled else None
        )
        # Duplicate-scan piggybackt op weekly_retro slot — één tick per week.
        self._next_duplicate_scan = (
            _next_weekly_fire(now, s.weekly_retro_weekday, s.weekly_retro_time)
            if s.weekly_retro_enabled else None
        )
        self._next_english_practice = (
            next_fire_time_with_catchup(
                now, s.english_practice_time, s.english_practice_weekend_time,
                last_fired=last_english,
            ) if s.english_practice_enabled else None
        )
        # Sales daily nudges (3x per dag). Voor de tijd-uitlijning hergebruiken
        # we next_fire_time met dezelfde HH:MM voor weekdays en weekends —
        # de weekend-skip wordt in de tick zelf afgehandeld.
        self._next_sales_morning = (
            next_fire_time(now, s.sales_nudge_morning_time,
                            s.sales_nudge_morning_time)
            if s.sales_nudge_enabled else None
        )
        self._next_sales_midday = (
            next_fire_time(now, s.sales_nudge_midday_time,
                            s.sales_nudge_midday_time)
            if s.sales_nudge_enabled else None
        )
        self._next_sales_evening = (
            next_fire_time(now, s.sales_nudge_evening_time,
                            s.sales_nudge_evening_time)
            if s.sales_nudge_enabled else None
        )
        self._last_tz_name = str(now.tzinfo)

    def _tz(self) -> ZoneInfo:
        """Active TZ (gecached, ~5s TTL). Vervalt terug naar
        settings.default_timezone als app_state.active_timezone leeg is."""
        return active_tz(
            db_path=self._settings.db_path,
            default=self._settings.default_timezone,
        )

    def run(self) -> None:
        log.info(
            "scheduler started (briefing_next=%s, midday_next=%s, dayclose_next=%s, "
            "market_digest_next=%s, ceo_letter_next=%s, english_practice_next=%s)",
            self._next_briefing.isoformat() if self._next_briefing else "disabled",
            self._next_midday.isoformat() if self._next_midday else "disabled",
            self._next_dayclose.isoformat() if self._next_dayclose else "disabled",
            self._next_market_digest.isoformat() if self._next_market_digest else "disabled",
            self._next_ceo_letter.isoformat() if self._next_ceo_letter else "disabled",
            self._next_english_practice.isoformat() if self._next_english_practice else "disabled",
        )
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler tick failed — continuing")
            self._stop_event.wait(timeout=10)  # wake every 10s
        log.info("scheduler stopped")

    # --- tick pieces ---

    def _tick(self) -> None:
        now = datetime.now(self._tz())
        # C1 fix: detect TZ-switch en herbereken alle user-facing
        # fire-tijden zodat een "rosa tz America/Los_Angeles" niet
        # leidt tot een briefing op 22:00 PT (= 07:00 NL morgen).
        current_tz_name = str(now.tzinfo)
        if current_tz_name != self._last_tz_name:
            if self._last_tz_name:  # niet bij eerste tick na init
                log.info(
                    "scheduler: TZ-switch detected (%s → %s) — recomputing "
                    "briefing/midday/dayclose/market_digest/ceo_letter/"
                    "english_practice fire-times",
                    self._last_tz_name, current_tz_name,
                )
            self._reset_user_schedule(now)
        self._fire_due_reminders()
        self._maybe_send_briefing(now)
        self._maybe_send_midday(now)
        self._maybe_send_dayclose(now)
        self._maybe_send_market_digest(now)
        self._maybe_send_ceo_letter(now)
        self._maybe_send_uptime_report(now)
        self._maybe_send_weekend_prep(now)
        self._maybe_send_weekly_retro(now)
        self._maybe_send_duplicate_scan(now)
        self._maybe_send_sales_morning_nudge(now)
        self._maybe_send_sales_midday_nudge(now)
        self._maybe_send_sales_evening_nudge(now)
        self._maybe_send_english_reminder(now)
        self._maybe_ping_delegations(now)
        self._maybe_check_travel(now)
        self._maybe_check_meeting_prep(now)
        self._maybe_scan_expenses(now)
        self._maybe_scan_plaud(now)
        self._maybe_prune_audit(now)
        self._maybe_prune_locations(now)
        self._maybe_prune_db_tables(now)
        self._maybe_run_pattern_detection(now)
        # Health-heartbeat — laatst zodat we alleen "tick succeeded" markeren
        # als alle stappen daarboven niet hebben geraised.
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()

    # Retry-state per job: bij generate-failure niet meteen naar
    # volgende slot schuiven, maar 5/15/30 min later opnieuw proberen.
    # Na 3 mislukte pogingen → notify the user via iMessage + skip naar
    # volgende geplande tijd.
    _RETRY_DELAYS_MIN = (5, 15, 30)

    def _handle_job_failure(
        self, *, job_name: str, label: str,
        failure: BaseException, next_normal_slot: datetime,
    ) -> datetime:
        """Bepaal next-fire na een failure. Returns next_normal_slot bij
        max retries (en stuurt een notificatie naar the user), anders
        een kort retry-window."""
        count = self._retry_count.get(job_name, 0)
        if count < len(self._RETRY_DELAYS_MIN):
            delay = self._RETRY_DELAYS_MIN[count]
            self._retry_count[job_name] = count + 1
            log.warning(
                "%s failed; retry %d/%d in %dmin (was: %s)",
                job_name, count + 1, len(self._RETRY_DELAYS_MIN),
                delay, failure,
            )
            return datetime.now(self._tz()) + timedelta(minutes=delay)
        # Max retries bereikt → notify + reset
        try:
            self._send(
                self._settings.primary_handle,
                f"⚠️ {label} kon niet worden gegenereerd (3 pogingen mislukt).\n"
                f"Reden: {type(failure).__name__}: {str(failure)[:200]}\n"
                f"Volgende geplande poging: "
                f"{next_normal_slot.strftime('%a %d %b %H:%M')}.",
            )
            log.info("%s: definitive-fail notification sent to the user",
                     job_name)
        except Exception:
            log.exception("could not send failure-notify for %s", job_name)
        self._retry_count[job_name] = 0
        return next_normal_slot

    def _handle_job_success(self, job_name: str) -> None:
        """Reset retry-counter na succesvolle run."""
        self._retry_count[job_name] = 0

    def _last_fired(self, job_name: str) -> datetime | None:
        try:
            with sqlite3.connect(self._settings.db_path) as conn:
                return get_last_fired(conn, job_name, tz=TZ)
        except Exception:
            log.exception("scheduler: read last_fired(%s) failed", job_name)
            return None

    def _record_fired(self, job_name: str, when: datetime,
                       *, notes: str | None = None) -> None:
        try:
            with sqlite3.connect(self._settings.db_path) as conn:
                set_last_fired(conn, job_name, when, notes=notes)
        except Exception:
            log.exception("scheduler: write last_fired(%s) failed", job_name)

    def _fire_due_reminders(self) -> None:
        with _conn(self._settings.db_path) as conn:
            due = reminders.due_now(conn)
            for r in due:
                body = f"⏰ Reminder: {r['body']}"
                try:
                    self._send(r["handle"], body)
                    reminders.mark_sent(conn, r["id"])
                    log.info("reminder #%d fired to %s", r["id"], r["handle"])
                except Exception:
                    log.exception("failed to send reminder #%d — will retry next tick", r["id"])

    def _maybe_send_briefing(self, now: datetime) -> None:
        if not self._settings.briefing_enabled or self._next_briefing is None:
            return
        if now < self._next_briefing:
            return

        log.info("sending daily briefing")
        # Heartbeat vóór de blocking generate_briefing — anders ziet de
        # health-monitor 3-5min stilte en SIGTERMt mid-briefing.
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = next_fire_time(
            datetime.now(self._tz()),
            self._settings.briefing_weekday_time,
            self._settings.briefing_weekend_time,
        )
        try:
            text = generate_briefing(
                gateway=self._gateway,
                gmail=self._gmail,
                calendar=self._calendar,
                db_path=self._settings.db_path,
                morning_extras_yaml=self._morning_extras_yaml,
                ollama=self._ollama,
                vip_path=self._vip_path,
                okrs_path=self._okrs_path,
                here=self._here,
                home_lat=self._settings.travel_alerts_home_lat,
                home_lon=self._settings.travel_alerts_home_lon,
                todoist_client=self._todoist_client,
                todoist_project_id=self._todoist_project_id,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("briefing sent (%d chars)", len(text))
            self._record_fired("briefing", datetime.now(self._tz()))
            self._handle_job_success("briefing")
            self._next_briefing = next_normal
        except Exception as exc:
            log.exception("briefing generation/send failed")
            self._next_briefing = self._handle_job_failure(
                job_name="briefing", label="Ochtendbriefing",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_send_midday(self, now: datetime) -> None:
        if not self._settings.midday_enabled or self._next_midday is None:
            return
        if now < self._next_midday:
            return

        log.info("sending midday heads-up")
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = next_fire_time(
            datetime.now(self._tz()),
            self._settings.midday_time,
            self._settings.midday_time,
        )
        try:
            text = generate_midday(
                gateway=self._gateway,
                gmail=self._gmail,
                calendar=self._calendar,
                db_path=self._settings.db_path,
                todoist_client=self._todoist_client,
                todoist_project_id=self._todoist_project_id,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("midday sent (%d chars)", len(text))
            self._record_fired("midday", datetime.now(self._tz()))
            self._handle_job_success("midday")
            self._next_midday = next_normal
        except Exception as exc:
            log.exception("midday generation/send failed")
            self._next_midday = self._handle_job_failure(
                job_name="midday", label="Midday heads-up",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_send_dayclose(self, now: datetime) -> None:
        if not self._settings.dayclose_enabled or self._next_dayclose is None:
            return
        if now < self._next_dayclose:
            return

        log.info("sending dayclose")
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = next_fire_time(
            datetime.now(self._tz()),
            self._settings.dayclose_time,
            self._settings.dayclose_time,
        )
        try:
            text = generate_dayclose(
                gateway=self._gateway,
                gmail=self._gmail,
                calendar=self._calendar,
                db_path=self._settings.db_path,
                okrs_path=self._okrs_path,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("dayclose sent (%d chars)", len(text))
            self._record_fired("dayclose", datetime.now(self._tz()))
            self._handle_job_success("dayclose")
            self._next_dayclose = next_normal
        except Exception as exc:
            log.exception("dayclose generation/send failed")
            self._next_dayclose = self._handle_job_failure(
                job_name="dayclose", label="Dayclose",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_send_market_digest(self, now: datetime) -> None:
        if not self._settings.market_intel_enabled or self._next_market_digest is None:
            return
        if now < self._next_market_digest:
            return

        log.info("sending weekly market-intel digest")
        try:
            text = generate_market_digest(
                gateway=self._gateway, db_path=self._settings.db_path,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("market-digest sent (%d chars)", len(text))
        except Exception:
            log.exception("market-digest generation/send failed")
        finally:
            self._next_market_digest = _next_weekly_fire(
                datetime.now(self._tz()),
                self._settings.market_intel_weekday,
                self._settings.market_intel_time,
            )

    def _maybe_send_ceo_letter(self, now: datetime) -> None:
        if not self._settings.ceo_letter_enabled or self._next_ceo_letter is None:
            return
        if now < self._next_ceo_letter:
            return

        log.info("sending weekly CEO-letter")
        # Heartbeat: ceo_letter doet een Claude-call die enkele seconden
        # kan duren — voorkom dat de health-monitor mid-call SIGTERMt.
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = _next_weekly_fire(
            datetime.now(self._tz()),
            self._settings.ceo_letter_weekday,
            self._settings.ceo_letter_time,
        )
        try:
            text = generate_ceo_letter(
                gateway=self._gateway,
                gmail=self._gmail,
                calendar=self._calendar,
                db_path=self._settings.db_path,
                okrs_path=self._okrs_path,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("ceo-letter sent (%d chars)", len(text))
            self._record_fired("ceo_letter", datetime.now(self._tz()))
            self._handle_job_success("ceo_letter")
            self._next_ceo_letter = next_normal
        except Exception as exc:
            log.exception("ceo-letter generation/send failed")
            self._next_ceo_letter = self._handle_job_failure(
                job_name="ceo_letter", label="Wekelijkse CEO-letter",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_send_uptime_report(self, now: datetime) -> None:
        """Wekelijkse uptime/downtime-digest naar the user. Default
        maandag 09:00 lokaal — overzicht van net afgelopen ma-zo."""
        if (not self._settings.uptime_report_enabled
                or self._next_uptime_report is None):
            return
        if now < self._next_uptime_report:
            return
        if self._uptime_config_path is None or not self._uptime_config_path.exists():
            log.info("uptime-report: skipped (no uptime config)")
            self._next_uptime_report = _next_weekly_fire(
                datetime.now(self._tz()),
                self._settings.uptime_report_weekday,
                self._settings.uptime_report_time,
            )
            return

        log.info("sending weekly uptime-report")
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = _next_weekly_fire(
            datetime.now(self._tz()),
            self._settings.uptime_report_weekday,
            self._settings.uptime_report_time,
        )
        try:
            from extensions.uptime.checker import load_targets
            from extensions.uptime.weekly_report import (
                compute_weekly_stats, format_imessage_report,
                previous_week_window,
            )
            targets = load_targets(self._uptime_config_path)
            target_names = [t["name"] for t in targets]
            if not target_names:
                log.info("uptime-report: no targets configured — skipped")
                self._next_uptime_report = next_normal
                return
            week_start, week_end = previous_week_window(now)
            stats = compute_weekly_stats(
                self._settings.db_path, target_names,
                week_start, week_end,
            )
            text = format_imessage_report(
                stats,
                week_start=week_start,
                week_end=week_end,
                threshold_pct=self._settings.uptime_report_threshold_pct,
                include_per_incident_list=self._settings.uptime_report_include_incidents,
            )
            self._send(self._settings.primary_handle, text)
            log.info("uptime-report sent (%d chars, %d targets)",
                     len(text), len(stats))
            self._record_fired("uptime_report", datetime.now(self._tz()))
            self._handle_job_success("uptime_report")
            self._next_uptime_report = next_normal
        except Exception as exc:
            log.exception("uptime-report generation/send failed")
            self._next_uptime_report = self._handle_job_failure(
                job_name="uptime_report", label="Wekelijkse uptime-report",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_send_weekend_prep(self, now: datetime) -> None:
        """Zondagavond voorbereiding op de komende week. Top-3
        prioriteiten + hangende items + maandag-eerste-meeting."""
        if (not self._settings.weekend_prep_enabled
                or self._next_weekend_prep is None):
            return
        if now < self._next_weekend_prep:
            return

        log.info("sending weekend-prep")
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = _next_weekly_fire(
            datetime.now(self._tz()),
            self._settings.weekend_prep_weekday,
            self._settings.weekend_prep_time,
        )
        try:
            from core.weekend_prep import generate_weekend_prep
            text = generate_weekend_prep(
                gateway=self._gateway,
                calendar=self._calendar,
                db_path=self._settings.db_path,
                todoist_client=self._todoist_client,
                todoist_project_id=self._todoist_project_id,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("weekend-prep sent (%d chars)", len(text))
            self._record_fired("weekend_prep", datetime.now(self._tz()))
            self._handle_job_success("weekend_prep")
            self._next_weekend_prep = next_normal
        except Exception as exc:
            log.exception("weekend-prep generation/send failed")
            self._next_weekend_prep = self._handle_job_failure(
                job_name="weekend_prep", label="Weekend-prep",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_send_duplicate_scan(self, now: datetime) -> None:
        """Weekelijkse duplicate-scan over pending reminders + open
        Todoist. Skipt de send als er 0 hits zijn — the user wil
        geen 'geen duplicaten'-noise."""
        if self._next_duplicate_scan is None or now < self._next_duplicate_scan:
            return
        # Volgende slot: één week verder.
        next_normal = _next_weekly_fire(
            datetime.now(self._tz()),
            self._settings.weekly_retro_weekday,
            self._settings.weekly_retro_time,
        )
        try:
            from core.duplicate_scan import collect_duplicate_pairs
            pairs = collect_duplicate_pairs(
                db_path=self._settings.db_path,
                todoist_client=self._todoist_client,
                todoist_project_id=self._todoist_project_id,
            )
            if not pairs:
                log.info("duplicate-scan: 0 hits — skip send")
                self._next_duplicate_scan = next_normal
                return
            lines: list[str] = [
                f"🔎 Duplicate scan — found {len(pairs)} possible duplicate pair(s):",
            ]
            for i, p in enumerate(pairs[:10], 1):
                k = p["keeper"]
                d = p["duplicate"]
                lines.append(
                    f"  {i}. keep {k['source']}#{k['id']}: '{k['body'][:60]}'"
                )
                lines.append(
                    f"     drop {d['source']}#{d['id']}: '{d['body'][:60]}'"
                )
            lines.append(
                "Reply with the numbers to close (e.g. '1,3,4'), or 'all', "
                "or 'skip'. Rosa will cancel the duplicates on approval."
            )
            self._send(self._settings.primary_handle, "\n".join(lines))
            log.info("duplicate-scan: sent %d pairs", len(pairs))
        except Exception:
            log.exception("duplicate-scan tick failed")
        self._next_duplicate_scan = next_normal

    def _maybe_send_weekly_retro(self, now: datetime) -> None:
        """Zaterdag 09:00 reflectie op de afgelopen week — volume,
        closed items, delegations-status, sales-pulse, patterns."""
        if (not self._settings.weekly_retro_enabled
                or self._next_weekly_retro is None):
            return
        if now < self._next_weekly_retro:
            return

        log.info("sending weekly retro")
        if self._health_state is not None:
            self._health_state.record_scheduler_tick()
        next_normal = _next_weekly_fire(
            datetime.now(self._tz()),
            self._settings.weekly_retro_weekday,
            self._settings.weekly_retro_time,
        )
        try:
            from core.weekly_retro import generate_weekly_retro
            text = generate_weekly_retro(
                gateway=self._gateway,
                db_path=self._settings.db_path,
                settings=self._settings,
            )
            self._send(self._settings.primary_handle, text)
            log.info("weekly retro sent (%d chars)", len(text))
            self._record_fired("weekly_retro", datetime.now(self._tz()))
            self._handle_job_success("weekly_retro")
            self._next_weekly_retro = next_normal
        except Exception as exc:
            log.exception("weekly retro generation/send failed")
            self._next_weekly_retro = self._handle_job_failure(
                job_name="weekly_retro", label="Weekly retro",
                failure=exc, next_normal_slot=next_normal,
            )

    def _maybe_ping_delegations(self, now: datetime) -> None:
        """Eén keer per 6u: vind delegations (outgoing_request /
        meeting_action_other) waarvan followup_at gepasseerd is en
        Rosa nog niet gepingd heeft. Stuur the user één samengevatte
        iMessage met de top-5 zodat 'ie kan beslissen: nu vragen of
        nog een week wachten."""
        if now.timestamp() < self._next_delegation_followup:
            return
        # Volgende tick over 6u.
        self._next_delegation_followup = now.timestamp() + 6 * 3600

        try:
            import sqlite3 as _sql
            from extensions.open_loops.schema import (
                delegations_due_for_followup, mark_followup_pinged,
            )
            with _sql.connect(
                self._settings.db_path, isolation_level=None,
            ) as conn:
                due = delegations_due_for_followup(
                    conn, now_ts=int(now.timestamp()), limit=5,
                )
                if not due:
                    return
                lines: list[str] = [
                    "🤝 Delegations — these are at the 7d follow-up mark:",
                ]
                for d in due:
                    who = d.get("who") or "?"
                    title = (d.get("action_summary")
                             or d.get("title") or "")[:80]
                    days_ago = (
                        int(now.timestamp()) - int(d.get("created_at") or 0)
                    ) // 86400
                    lines.append(
                        f"  #{d['id']} {who} — {title} ({days_ago}d ago)"
                    )
                lines.append(
                    "Reply 'remind X again in N days' to defer, or "
                    "close them via close_loop."
                )
                text = "\n".join(lines)
                self._send(self._settings.primary_handle, text)
                mark_followup_pinged(conn, [int(d["id"]) for d in due])
                log.info(
                    "delegation-followup: pinged %d items", len(due),
                )
        except Exception:
            log.exception("delegation-followup tick failed")

    def _maybe_send_english_reminder(self, now: datetime) -> None:
        """Quick daily nudge if cards are due. Generates locally (no LLM), so
        no retry-with-backoff is needed — DB unavailability means we just
        bump to the next slot."""
        if (not self._settings.english_practice_enabled
                or self._next_english_practice is None):
            return
        if now < self._next_english_practice:
            return

        next_normal = next_fire_time(
            datetime.now(self._tz()),
            self._settings.english_practice_time,
            self._settings.english_practice_weekend_time,
        )
        # Skip weekend if configured — bump straight to next non-weekend slot.
        if self._settings.english_practice_skip_weekend:
            while next_normal.weekday() >= 5:
                next_normal = next_fire_time(
                    next_normal, self._settings.english_practice_time,
                    self._settings.english_practice_weekend_time,
                )
            if now.weekday() >= 5:
                # Today is weekend — don't send, just bump.
                log.info("english_practice: skipping weekend nudge")
                self._next_english_practice = next_normal
                return

        try:
            text = generate_english_reminder(self._settings.db_path)
        except Exception:
            log.exception("english_practice: reminder generation failed")
            self._next_english_practice = next_normal
            return

        if text is None:
            log.info("english_practice: nothing due — skipping nudge")
            self._next_english_practice = next_normal
            return

        try:
            self._send(self._settings.primary_handle, text)
            log.info("english_practice reminder sent (%d chars)", len(text))
            self._record_fired("english_practice", datetime.now(self._tz()))
        except Exception:
            log.exception("english_practice: iMessage send failed")
        self._next_english_practice = next_normal

    def _send_sales_nudge(
        self, now: datetime, *, slot: str,
        next_attr: str, time_attr: str, builder: Any,
    ) -> None:
        """Gedeelde implementatie voor de drie nudges.
        `slot` is voor log-prefix; `next_attr` is bv. '_next_sales_morning';
        `time_attr` is bv. 'sales_nudge_morning_time'; `builder` returnt str."""
        s = self._settings
        if not s.sales_nudge_enabled:
            return
        next_fire = getattr(self, next_attr)
        if next_fire is None or now < next_fire:
            return

        next_normal = next_fire_time(
            datetime.now(self._tz()),
            getattr(s, time_attr), getattr(s, time_attr),
        )
        # Weekend-skip: bump door tot eerstvolgende ma indien zo geconfigd
        if s.sales_nudge_skip_weekends:
            while next_normal.weekday() >= 5:
                next_normal = next_fire_time(
                    next_normal, getattr(s, time_attr),
                    getattr(s, time_attr),
                )
            if now.weekday() >= 5:
                log.info("sales-nudge %s: skipping weekend", slot)
                setattr(self, next_attr, next_normal)
                return

        try:
            text = builder(
                self._settings.db_path,
                target_count=s.sales_nudge_target_count,
            )
        except Exception:
            log.exception("sales-nudge %s: build failed", slot)
            setattr(self, next_attr, next_normal)
            return

        try:
            self._send(s.primary_handle, text)
            log.info("sales-nudge %s sent (%d chars)", slot, len(text))
        except Exception:
            log.exception("sales-nudge %s: iMessage send failed", slot)
        setattr(self, next_attr, next_normal)

    def _maybe_send_sales_morning_nudge(self, now: datetime) -> None:
        from extensions.sales.nudges import build_morning_nudge
        self._send_sales_nudge(
            now, slot="morning",
            next_attr="_next_sales_morning",
            time_attr="sales_nudge_morning_time",
            builder=build_morning_nudge,
        )

    def _maybe_send_sales_midday_nudge(self, now: datetime) -> None:
        from extensions.sales.nudges import build_midday_nudge
        self._send_sales_nudge(
            now, slot="midday",
            next_attr="_next_sales_midday",
            time_attr="sales_nudge_midday_time",
            builder=build_midday_nudge,
        )

    def _maybe_send_sales_evening_nudge(self, now: datetime) -> None:
        from extensions.sales.nudges import build_evening_nudge
        self._send_sales_nudge(
            now, slot="evening",
            next_attr="_next_sales_evening",
            time_attr="sales_nudge_evening_time",
            builder=build_evening_nudge,
        )

    def _maybe_check_travel(self, now: datetime) -> None:
        if self._next_travel_check is None or self._here is None:
            return
        if now.timestamp() < self._next_travel_check:
            return
        try:
            n = travel_alerts_tick(
                db_path=self._settings.db_path,
                calendar=self._calendar,
                here=self._here,
                send_imessage=self._send,
                primary_handle=self._settings.primary_handle,
                horizon_minutes=self._settings.travel_alerts_horizon_minutes,
                plan_minutes=self._settings.travel_alerts_plan_minutes,
                buffer_minutes=self._settings.travel_alerts_buffer_minutes,
                home_lat=self._settings.travel_alerts_home_lat,
                home_lon=self._settings.travel_alerts_home_lon,
            )
            if n:
                log.info("travel-alerts: %d alert(s) sent", n)
        except Exception:
            log.exception("travel-alerts tick failed")
        self._next_travel_check = now.timestamp() + self._settings.travel_alerts_check_interval_seconds

    def _maybe_check_meeting_prep(self, now: datetime) -> None:
        if self._next_meeting_prep_check is None:
            return
        if now.timestamp() < self._next_meeting_prep_check:
            return
        try:
            if self._vip_path is not None:
                n = meeting_prep_tick(
                    db_path=self._settings.db_path,
                    calendar=self._calendar,
                    gateway=self._gateway,
                    vip_path=self._vip_path,
                    send_imessage=self._send,
                    primary_handle=self._settings.primary_handle,
                    gmail_address=self._gmail_address,
                    minutes_before=self._settings.meeting_prep_minutes_before,
                    skip_internal_only=self._settings.meeting_prep_skip_internal_only,
                    settings=self._settings,
                )
                if n:
                    log.info("meeting-prep: %d brief(s) sent", n)
        except Exception:
            log.exception("meeting-prep tick failed")
        self._next_meeting_prep_check = (
            now.timestamp() + self._settings.meeting_prep_check_interval_seconds
        )

    def _maybe_scan_expenses(self, now: datetime) -> None:
        if self._next_expense_scan is None:
            return
        if now.timestamp() < self._next_expense_scan:
            return
        try:
            n = scan_expenses_inbox(
                self._settings.expenses_inbox_dir,
                self._settings.db_path,
                gateway=self._gateway,
            )
            if n:
                log.info("expenses: %d new receipt(s) processed", n)
        except Exception:
            log.exception("expenses scan tick failed")
        self._next_expense_scan = (
            now.timestamp() + self._settings.expenses_check_interval_seconds
        )

    def _maybe_run_pattern_detection(self, now: datetime) -> None:
        if now < self._next_pattern_detect:
            return
        try:
            patterns = run_weekly_detection(
                self._settings.db_path, ollama=self._ollama,
                settings=self._settings,
            )
            if patterns:
                log.info("patterns: %d signal(s) detected this week",
                         len(patterns))
        except Exception:
            log.exception("pattern detection tick failed")
        self._next_pattern_detect = _next_weekly_fire(
            datetime.now(self._tz()), target_weekday=0, hhmm="09:00",
        )

    def _maybe_prune_audit(self, now: datetime) -> None:
        if now.timestamp() < self._next_audit_prune:
            return
        try:
            # MED-4: split retention — egress metadata blijft langer (90d
            # default) dan shadow-payloads (14d default). Twee aparte
            # prunes met aparte vensters.
            removed_egress = audit_prune_old(
                self._settings.audit_dir,
                max_age_days=self._settings.audit_retention_days,
                prefix="egress",
            )
            removed_payloads = audit_prune_old(
                self._settings.audit_dir,
                max_age_days=self._settings.payloads_retention_days,
                prefix="payloads",
            )
            # Admin-action stream blijft langer dan egress — admin-acties
            # zijn relevant voor jaar-audits (A.12.4.3 + A.18.1.3).
            removed_admin = audit_prune_old(
                self._settings.audit_dir,
                max_age_days=self._settings.admin_retention_days,
                prefix="admin",
            )
            if removed_egress:
                log.info("audit: pruned %d egress file(s) older than %d days",
                         removed_egress, self._settings.audit_retention_days)
            if removed_payloads:
                log.info("audit: pruned %d payload file(s) older than %d days",
                         removed_payloads, self._settings.payloads_retention_days)
            if removed_admin:
                log.info("audit: pruned %d admin file(s) older than %d days",
                         removed_admin, self._settings.admin_retention_days)
        except Exception:
            log.exception("audit-prune tick failed")
        # Volgende run over 24u
        self._next_audit_prune = now.timestamp() + 86400

    def _maybe_prune_locations(self, now: datetime) -> None:
        """ISO MED-2: drop GPS-history rows older than the configured
        retention window. Cheap SQL DELETE; runs once per 24h."""
        if now.timestamp() < self._next_location_prune:
            return
        try:
            with sqlite3.connect(self._settings.db_path) as conn:
                removed = prune_old_locations(
                    conn, days=self._settings.location_retention_days,
                )
            if removed:
                log.info("travel-alerts: pruned %d location row(s) older than %d days",
                         removed, self._settings.location_retention_days)
        except Exception:
            log.exception("location-prune tick failed")
        self._next_location_prune = now.timestamp() + 86400

    def _maybe_prune_db_tables(self, now: datetime) -> None:
        """ISO A.18.1.3: per-table retention prune for comm_items +
        expenses + uptime_events. All default to long retentions
        (365d / 7 years / 90d-up + 365d-alert) so this is rarely
        deletion-heavy; runs once per 24h."""
        if now.timestamp() < self._next_db_prune:
            return
        try:
            with sqlite3.connect(self._settings.db_path) as conn:
                comm_removed = prune_old_comm_items(
                    conn, days=self._settings.comm_items_retention_days,
                )
                exp_removed = prune_old_expenses(
                    conn, days=self._settings.expenses_retention_days,
                )
                up_removed, alert_removed = prune_old_uptime_events(
                    conn,
                    days_up=self._settings.uptime_events_retention_days_up,
                    days_alert=self._settings.uptime_events_retention_days_alert,
                )
                # M3 review-fix: AVG-retentie van inactieve sales-accounts.
                # Hard-delete `koud` zonder touchpoints in N dagen.
                sales_removed, sales_ids = prune_inactive_cold_accounts(
                    conn,
                    days_since_last_touch=self._settings.sales_retention_cold_days,
                )
                if sales_removed:
                    try:
                        from core.audit import log_admin_action
                        log_admin_action(
                            action="sales_account_retention_prune",
                            actor="scheduler",
                            from_value={"ids": sales_ids,
                                          "count": sales_removed},
                            reason=(
                                f"koud + geen touchpoints in "
                                f"{self._settings.sales_retention_cold_days}d"
                            ),
                        )
                    except Exception:
                        log.exception("sales retention audit failed")
                # Audit DB-2 (28/6): iMessage-conversaties + processed-
                # message-bodies retention (GDPR art 5(1)(e)).
                from core.db import prune_conversation_history
                turns_removed, processed_removed = prune_conversation_history(
                    conn,
                    turns_days=self._settings.conversation_turns_retention_days,
                    processed_days=self._settings.processed_messages_retention_days,
                )
                if turns_removed:
                    log.info(
                        "conversation_turns: pruned %d rows older than %d days",
                        turns_removed,
                        self._settings.conversation_turns_retention_days,
                    )
                if processed_removed:
                    log.info(
                        "processed_messages: pruned %d rows older than %d days",
                        processed_removed,
                        self._settings.processed_messages_retention_days,
                    )
            if comm_removed:
                log.info("comm-intel: pruned %d items older than %d days",
                         comm_removed, self._settings.comm_items_retention_days)
            if exp_removed:
                log.info("expenses: pruned %d items older than %d days",
                         exp_removed, self._settings.expenses_retention_days)
            if up_removed or alert_removed:
                log.info(
                    "uptime: pruned %d up/down + %d alert events",
                    up_removed, alert_removed,
                )
            if sales_removed:
                log.info(
                    "sales: pruned %d cold accounts inactive >%d days",
                    sales_removed,
                    self._settings.sales_retention_cold_days,
                )
        except Exception:
            log.exception("db-prune tick failed")
        self._next_db_prune = now.timestamp() + 86400

    def _maybe_scan_plaud(self, now: datetime) -> None:
        if now.timestamp() < self._next_plaud_scan:
            return
        try:
            added = plaud.scan_inbox(self._settings.plaud_inbox_dir, self._settings.db_path)
            if added:
                log.info("plaud inbox: ingested %d new transcript(s)", added)
        except Exception:
            log.exception("plaud inbox scan failed")
        # Also analyze pending transcripts (cheap if none); produces meeting
        # records + open_loops for action items.
        if self._ollama is not None:
            try:
                from extensions.plaud_intel.analyze import analyze_pending
                n = analyze_pending(
                    self._settings.db_path, self._ollama, limit=3,
                    user_name=self._settings.user_name,
                )
                if n:
                    log.info("plaud-analyze: %d meeting(s) processed", n)
            except Exception:
                log.exception("plaud-analyze tick failed")
        self._next_plaud_scan = now.timestamp() + 60  # every 60s


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _next_weekly_fire(now: datetime, target_weekday: int, hhmm: str) -> datetime:
    """Eerstvolgende weekly fire-moment > now. weekday: 0=ma .. 6=zo.
    Als vandaag de target-day is en het tijdstip nog komt → vandaag;
    anders volgende week dezelfde dag."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=hh, minute=mm, second=0, microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate
