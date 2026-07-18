"""Health-monitoring voor de pa-agent.

Drie functies in één module:
  HealthState  — gedeelde, thread-safe state die main-loop + scheduler
                 hun laatste-tick-timestamps in kunnen updaten.
  HealthMonitor — eigen thread die elke 60s checkt of alles nog leeft.
                 Bij blijvende failure: iMessage warning + SIGTERM zodat
                 launchd het proces opnieuw opstart (vs. silent dead).
  describe_health — snapshot die het 'rosa?' iMessage-shortcut gebruikt.

Wat we monitoren:
  * iMessage poll-loop tick-frequentie (te lang stil = hangend proces)
  * Scheduler tick-frequentie (idem)
  * File-descriptor usage (was de aanleiding om dit te bouwen)
  * Resident memory (best-effort via resource — niet exact maar trendbaar)
"""
from __future__ import annotations

import logging
import os
import resource
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class HealthState:
    """Thread-safe shared state. Update via record_*; lees via snapshot()."""
    started_at: float = field(default_factory=time.time)
    last_imessage_poll_at: float = field(default_factory=time.time)
    last_scheduler_tick_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_imessage_poll(self) -> None:
        with self._lock:
            self.last_imessage_poll_at = time.time()

    def record_scheduler_tick(self) -> None:
        with self._lock:
            self.last_scheduler_tick_at = time.time()

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                "started_at": self.started_at,
                "last_imessage_poll_at": self.last_imessage_poll_at,
                "last_scheduler_tick_at": self.last_scheduler_tick_at,
            }


# --- helpers --------------------------------------------------------------

def _open_fd_count() -> int:
    """Aantal open FDs van DEZE process — telt /dev/fd entries op macOS."""
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return -1


def _fd_soft_limit() -> int:
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return soft
    except Exception:
        return 0


def _rss_mb() -> float:
    """Rough resident memory in MB."""
    try:
        u = resource.getrusage(resource.RUSAGE_SELF)
        # Op macOS is ru_maxrss in bytes, op Linux in kilobytes.
        if os.uname().sysname == "Darwin":
            return u.ru_maxrss / (1024 * 1024)
        return u.ru_maxrss / 1024
    except Exception:
        return 0.0


# --- describe (used by 'rosa?' shortcut) ---------------------------------

def describe_health(state: HealthState) -> str:
    """Compacte one-liner-ish status voor iMessage-reply op 'rosa?'."""
    snap = state.snapshot()
    now = time.time()
    uptime_s = int(now - snap["started_at"])
    days, rem = divmod(uptime_s, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    uptime_str = (
        f"{days}d {hours}h" if days
        else (f"{hours}h {mins}m" if hours else f"{mins}m")
    )
    last_poll_s = int(now - snap["last_imessage_poll_at"])
    last_tick_s = int(now - snap["last_scheduler_tick_at"])
    fd_open = _open_fd_count()
    fd_soft = _fd_soft_limit()
    rss = _rss_mb()
    return (
        f"Rosa alive ✓\n"
        f"Uptime: {uptime_str}\n"
        f"Last iMessage poll: {last_poll_s}s ago\n"
        f"Last scheduler tick: {last_tick_s}s ago\n"
        f"FDs: {fd_open}/{fd_soft}\n"
        f"RAM (peak): {rss:.0f} MB"
    )


# --- monitor thread -------------------------------------------------------

class HealthMonitor(threading.Thread):
    """Achtergrond-thread die periodiek de gezondheid van de agent checkt
    en bij blijvende failure SIGTERM raised (launchd KeepAlive restart)."""

    def __init__(
        self,
        *,
        state: HealthState,
        stop_event: threading.Event,
        send_imessage: Callable[[str, str], None],
        primary_handle: str,
        check_interval_seconds: int = 60,
        log_interval_seconds: int = 900,                  # 15 min
        imessage_poll_max_silence_seconds: int = 60,
        scheduler_tick_max_silence_seconds: int = 120,
        fd_warn_ratio: float = 0.80,
        rss_warn_mb: float = 1500.0,
        consecutive_failures_before_kill: int = 3,
    ) -> None:
        super().__init__(name="pa-health", daemon=True)
        self._state = state
        self._stop_event = stop_event
        self._send = send_imessage
        self._handle = primary_handle
        self._check_interval = check_interval_seconds
        self._log_interval = log_interval_seconds
        self._imessage_max_silence = imessage_poll_max_silence_seconds
        self._scheduler_max_silence = scheduler_tick_max_silence_seconds
        self._fd_warn_ratio = fd_warn_ratio
        self._rss_warn_mb = rss_warn_mb
        self._kill_threshold = consecutive_failures_before_kill
        self._consecutive_failures = 0
        self._last_log_ts = 0.0
        self._notified_warnings: set[str] = set()

    def run(self) -> None:
        log.info(
            "health-monitor started (check=%ss, kill_after=%d failures)",
            self._check_interval, self._kill_threshold,
        )
        # Korte initiële wachttijd zodat de eerste poll / scheduler-tick
        # gegarandeerd is gebeurd vóór onze eerste check.
        self._stop_event.wait(timeout=20)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("health-monitor tick failed")
            self._stop_event.wait(timeout=self._check_interval)
        log.info("health-monitor stopped")

    def _tick(self) -> None:
        now = time.time()
        snap = self._state.snapshot()

        problems: list[str] = []
        # 1. iMessage poll
        poll_silence = now - snap["last_imessage_poll_at"]
        if poll_silence > self._imessage_max_silence:
            problems.append(f"iMessage poll silent voor {int(poll_silence)}s")
        # 2. Scheduler tick
        sched_silence = now - snap["last_scheduler_tick_at"]
        if sched_silence > self._scheduler_max_silence:
            problems.append(f"scheduler tick silent voor {int(sched_silence)}s")
        # 3. File descriptors
        fd_open = _open_fd_count()
        fd_soft = _fd_soft_limit()
        if fd_open > 0 and fd_soft > 0:
            if (fd_open / fd_soft) >= self._fd_warn_ratio:
                problems.append(f"FDs {fd_open}/{fd_soft} (>{int(self._fd_warn_ratio*100)}%)")
        # 4. Memory (warning only, geen kill — gewoon flag)
        rss = _rss_mb()
        memory_warning = rss > self._rss_warn_mb

        # Periodic log line — zichtbare metrics ook als alles oké is.
        if (now - self._last_log_ts) >= self._log_interval:
            log.info(
                "health: uptime=%ds fd=%d/%d rss=%.0fMB "
                "imessage_poll_age=%ds scheduler_tick_age=%ds",
                int(now - snap["started_at"]), fd_open, fd_soft, rss,
                int(poll_silence), int(sched_silence),
            )
            self._last_log_ts = now

        # Memory-warning is informatief — 1× sturen per tier, geen kill.
        if memory_warning and "rss" not in self._notified_warnings:
            self._notified_warnings.add("rss")
            self._notify(f"⚠️ Rosa: RAM gebruik {rss:.0f}MB > {self._rss_warn_mb:.0f}MB threshold")

        if problems:
            self._consecutive_failures += 1
            log.warning("health: failure #%d — %s",
                        self._consecutive_failures, "; ".join(problems))
            if self._consecutive_failures == 1:
                # Eerste failure: the user even waarschuwen.
                self._notify(
                    "⚠️ Rosa healthcheck waarschuwing:\n"
                    + "\n".join(f"• {p}" for p in problems)
                    + "\n(monitor blijft kijken; restart na 3× achter elkaar)"
                )
            if self._consecutive_failures >= self._kill_threshold:
                self._notify(
                    f"🛑 Rosa healthcheck failure {self._consecutive_failures}× "
                    f"achter elkaar — restart via SIGTERM."
                )
                log.critical("health: kill-threshold bereikt → SIGTERM")
                # SIGTERM triggert de signal-handler die stop_event.set();
                # main-run() retourneert, launchd KeepAlive start opnieuw.
                os.kill(os.getpid(), signal.SIGTERM)
        else:
            if self._consecutive_failures > 0:
                log.info("health: hersteld na %d failure(s)",
                         self._consecutive_failures)
                self._consecutive_failures = 0

    def _notify(self, body: str) -> None:
        try:
            self._send(self._handle, body)
        except Exception:
            log.exception("health: iMessage notify mislukt — body=%s", body[:120])
