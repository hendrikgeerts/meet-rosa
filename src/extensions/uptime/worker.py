"""UptimeWorker — background thread die per-target HTTP-checks doet
en bij threshold-failures de alert-pipeline triggert.

Threading-model: één thread, sequential checks over alle targets. Voor
2-5 targets met 60s interval is dat ruim voldoende — geen async/HTTP-pool
nodig. Wakker elke 5s, check welke targets due zijn.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from extensions.uptime.alerts import dispatch_alert
from extensions.uptime.checker import check, load_targets
from extensions.uptime.schema import (
    CheckResult, get_target_state, init_uptime_schema, insert_event,
    mark_escalated,
    record_alert_sent, record_check, remove_target, upsert_target,
)

log = logging.getLogger(__name__)


class UptimeWorker(threading.Thread):
    """Sequential HTTP-checks in een eigen thread."""

    def __init__(
        self,
        *,
        db_path: Path,
        config_path: Path,
        stop_event: threading.Event,
        send_imessage: Callable[[str, str], None],
        primary_handle: str,
        tts_synthesize: Callable[..., Path] | None = None,
        tts_voice: str = "Ava (Enhanced)",
        send_imessage_audio: Callable[[str, Path], None] | None = None,
        ntfy_topic: str | None = None,
        ntfy_server: str = "https://ntfy.sh",
        tick_seconds: int = 5,
        escalate_after_seconds: int = 600,   # 10 min — Ntfy Critical voor DND-doorbraak
    ) -> None:
        super().__init__(name="uptime-worker", daemon=True)
        self._db_path = db_path
        self._config_path = config_path
        self._stop_event = stop_event
        self._send = send_imessage
        self._handle = primary_handle
        self._tts = tts_synthesize
        self._tts_voice = tts_voice
        self._send_audio = send_imessage_audio
        self._ntfy_topic = ntfy_topic
        self._ntfy_server = ntfy_server
        self._tick = tick_seconds
        self._escalate_after_seconds_default = int(escalate_after_seconds)
        # In-memory next-due timestamps per target zodat we niet bij elke
        # tick een DB-query nodig hebben. Wordt gehydrateerd uit
        # last_check_at + interval bij start.
        self._next_due: dict[str, float] = {}
        self._targets: list[dict[str, Any]] = []

    def run(self) -> None:
        init_uptime_schema(self._db_path)
        self._reload_config()
        log.info(
            "uptime-worker started: %d targets, tick=%ds",
            len(self._targets), self._tick,
        )

        while not self._stop_event.is_set():
            try:
                self._tick_once()
            except Exception:
                log.exception("uptime tick failed — continuing")
            self._stop_event.wait(timeout=self._tick)
        log.info("uptime-worker stopped")

    def _reload_config(self) -> None:
        """Read config + sync uptime_checks table to match."""
        new_targets = load_targets(self._config_path)
        new_names = {t["name"] for t in new_targets}
        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            # Drop targets that disappeared from config
            existing = conn.execute(
                "SELECT name FROM uptime_checks"
            ).fetchall()
            for (name,) in existing:
                if name not in new_names:
                    remove_target(conn, name=name)
            # Upsert current ones
            for t in new_targets:
                upsert_target(conn, name=t["name"], url=t["url"])
        self._targets = new_targets
        now = time.time()
        # R3+M5: jitter zodat post-restart niet alle targets simultaan
        # afvuren. Random offset binnen het check_interval houdt
        # gemiddelde frequentie correct, voorkomt thundering-herd.
        self._next_due = {
            t["name"]: now + random.uniform(0, t["check_interval_seconds"])
            for t in self._targets
        }

    def _tick_once(self) -> None:
        now = time.time()
        for target in self._targets:
            name = target["name"]
            if now < self._next_due.get(name, 0):
                continue
            retry_after = self._check_one(target, now=int(now))
            # R1: als HTTP 429/503 een Retry-After header gaf, respect
            # die in plaats van te hammeren op vaste interval. Cap is al
            # toegepast in checker (max 3600s).
            interval = target["check_interval_seconds"]
            if retry_after is not None and retry_after > interval:
                self._next_due[name] = now + retry_after
            else:
                self._next_due[name] = now + interval

    def _check_one(self, target: dict[str, Any], *, now: int) -> int | None:
        """Run één check + interpret + alert if needed.

        Atomicity-fix (post-review): DB-state-mutaties gebeuren binnen
        de `with sqlite3.connect()` block, maar `dispatch_alert` (die 2-10s
        kan duren door iMessage/TTS/ntfy) wordt BUITEN de connection
        uitgevoerd. Voorkomt event-zonder-last_alert_at gat als de daemon
        midden in een send crasht.

        Returns Retry-After seconds als HTTP 429/503 dat aangaf (anders
        None) — caller gebruikt het voor next_due-scheduling.
        """
        result = check(
            name=target["name"], url=target["url"],
            expected_status=target["expected_status"],
            timeout_seconds=target["timeout_seconds"],
            expect_text=target.get("expect_text"),
        )

        # Phase 1: read state + decide what to send (binnen DB-conn).
        pending_alert: tuple[str, bool, int] | None = None  # (kind, re_alert, duration_s)
        pending_recovery_duration: int | None = None

        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            state = get_target_state(conn, name=result.name) or {}
            prev_failures = int(state.get("consecutive_failures") or 0)
            was_down = bool(state.get("is_down"))
            down_since = state.get("down_since")
            silence_until = state.get("silence_until") or 0

            if result.ok:
                new_failures = 0
                new_is_down = False
                new_down_since = None
                if was_down and down_since:
                    duration_seconds = now - int(down_since)
                    insert_event(
                        conn, target_name=result.name, kind="recovery",
                        status_code=result.status_code,
                        latency_ms=result.latency_ms,
                        detail=f"downtime {duration_seconds}s",
                    )
                    if now >= silence_until:
                        pending_recovery_duration = duration_seconds
                else:
                    insert_event(
                        conn, target_name=result.name, kind="up",
                        status_code=result.status_code,
                        latency_ms=result.latency_ms,
                    )
            else:
                new_failures = prev_failures + 1
                new_is_down = new_failures >= target["fail_threshold"]
                new_down_since = down_since or now
                insert_event(
                    conn, target_name=result.name, kind="down",
                    status_code=result.status_code,
                    latency_ms=result.latency_ms,
                    error=result.error,
                )
                if new_is_down and now >= silence_until:
                    last_alert = state.get("last_alert_at") or 0
                    just_triggered = not was_down
                    re_alert_due = (
                        was_down
                        and (now - int(last_alert)) >= target["re_alert_interval_seconds"]
                    )
                    duration_now = now - new_down_since
                    if just_triggered:
                        insert_event(
                            conn, target_name=result.name, kind="alert",
                            status_code=result.status_code,
                            latency_ms=result.latency_ms,
                            error=result.error,
                        )
                        pending_alert = ("alert", False, duration_now)
                    elif re_alert_due:
                        insert_event(
                            conn, target_name=result.name, kind="realert",
                            status_code=result.status_code,
                            latency_ms=result.latency_ms,
                            error=result.error,
                        )
                        pending_alert = ("realert", True, duration_now)

            record_check(
                conn, result=result,
                consecutive_failures=new_failures,
                is_down=new_is_down, down_since=new_down_since,
            )
            # Markeer alert-sent VOOR de daadwerkelijke send — als de
            # send crasht is alleen het iMessage gemist; bij crash daarna
            # wordt geen re-alert direct gefired (we eten een verkeerde
            # bool, beter dan dubbel-pingen).
            if pending_alert is not None:
                record_alert_sent(conn, name=result.name, at=now)

        # Phase 2: dispatch alerts BUITEN de DB-connection. iMessage/
        # TTS/ntfy kunnen seconden duren — geen reden om andere DB-
        # writers (comm-intel ingest, scheduler) zo lang te blokkeren.
        if pending_alert is not None:
            kind, re_alert, dur = pending_alert
            self._send_down_alert(
                target, result=result, re_alert=re_alert,
                duration_seconds=dur,
            )
        if pending_recovery_duration is not None:
            self._send_recovery(target, duration_seconds=pending_recovery_duration)

        return result.retry_after

    def _send_down_alert(
        self, target: dict[str, Any], *, result: CheckResult,
        re_alert: bool, duration_seconds: int,
    ) -> None:
        channels = set(target.get("alert_channels") or ["imessage"])

        # Escalation-laag: bij langdurige downtime de Ntfy-channel
        # automatisch aanzetten (mits er een topic geconfigureerd is).
        # Per-target override `escalate_after_seconds`; anders fallback
        # naar settings-default. 0 of None = uit.
        #
        # M1 — alleen EENMAAL per incident escaleren. Re-alerts om de
        # 15 min zouden anders een Critical-Alert-storm geven en iOS
        # zou ze als spam herkennen. `escalated_at` op uptime_checks
        # markeert dat we al gepushed hebben; record_check reset het
        # naar NULL bij recovery zodat een toekomstige outage
        # opnieuw kan escaleren.
        escalate_after = target.get("escalate_after_seconds")
        if escalate_after is None:
            escalate_after = self._escalate_after_seconds_default

        escalated = False
        if (
            escalate_after and escalate_after > 0
            and duration_seconds >= int(escalate_after)
            and self._ntfy_topic
        ):
            with sqlite3.connect(self._db_path, isolation_level=None) as conn:
                state = get_target_state(conn, name=target["name"])
                already_escalated = bool(state and state.get("escalated_at"))
            if not already_escalated:
                channels = channels | {"ntfy"}
                escalated = True

        body = dispatch_alert(
            target=target, result=result,
            duration_seconds=duration_seconds, re_alert=re_alert,
            kind="down",
            channels=channels,
            send_imessage=self._send,
            primary_handle=self._handle,
            tts_synthesize=self._tts,
            tts_voice=self._tts_voice,
            send_imessage_audio=self._send_audio,
            ntfy_topic=self._ntfy_topic,
            ntfy_server=self._ntfy_server,
        )
        if escalated:
            try:
                with sqlite3.connect(
                    self._db_path, isolation_level=None,
                ) as conn:
                    mark_escalated(conn, name=target["name"])
            except Exception:
                log.exception(
                    "uptime: kon escalated_at niet markeren voor %s",
                    target["name"],
                )
        log.warning(
            "uptime: ALERT %s (%s, %ds%s) — %s",
            target["name"], "re-alert" if re_alert else "first",
            duration_seconds,
            " ESCALATED→ntfy" if escalated else "",
            (body or "(silenced)")[:100],
        )

    def _send_recovery(
        self, target: dict[str, Any], *, duration_seconds: int,
    ) -> None:
        channels = set(target.get("alert_channels") or ["imessage"])
        # Recovery via imessage + ntfy; geen voice (terug-online is goed
        # nieuws, geen wakker-maken nodig).
        recovery_channels = channels & {"imessage", "ntfy"}
        dispatch_alert(
            target=target, result=None,
            duration_seconds=duration_seconds, re_alert=False,
            kind="recovery",
            channels=recovery_channels,
            send_imessage=self._send,
            primary_handle=self._handle,
            tts_synthesize=None,
            tts_voice=self._tts_voice,
            send_imessage_audio=None,
            ntfy_topic=self._ntfy_topic,
            ntfy_server=self._ntfy_server,
        )
        log.info(
            "uptime: RECOVERY %s — was down for %ds",
            target["name"], duration_seconds,
        )
