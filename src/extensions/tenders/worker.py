"""TenderNed-polling worker. Eigen thread, default 30 min tick.

Strategie:
  1. Pak laatste 100 publicatie-summaries van /publicaties?size=100
  2. Voor elke niet-eerder-geziene publicatie_id:
     - Detail ophalen (CPV-codes + trefwoorden in detail-endpoint)
     - Matcher laten beoordelen
     - Insert in tenders-tabel (matched + unmatched, voor dedupe)
     - Bij matched + niet-rectificatie + sluitingsdatum nog niet voorbij:
       iMessage-alert sturen, kenmerk loggen als alerted
  3. Daily prune van oude unmatched-rijen

Heartbeat naar HealthState bij elke tick zodat we niet ge-SIGTERMed worden
tijdens een trage feed-call.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

# TenderNed levert sluitingsdatums + publicatiedatums naive — interpretatie
# moet ALTIJD Europe/Amsterdam zijn, niet the user's active TZ (review H1).
# Anders skipt skip_expired verkeerd wanneer hij in een andere TZ zit.
TENDERNED_TZ = ZoneInfo("Europe/Amsterdam")

from .alerts import format_alert
from .feed import (
    TenderNedError, TenderNedRateLimited,
    fetch_publication_detail, fetch_recent_summaries, overview_url,
)
from .matcher import DEFAULT_FILTER, TenderFilter, match
from .schema import (
    kenmerk_already_alerted, prune_old_unmatched, tender_exists,
)

log = logging.getLogger(__name__)


class TenderWorker(threading.Thread):
    def __init__(
        self,
        *,
        db_path: Path,
        stop_event: threading.Event,
        send_imessage: Callable[[str, str], None],
        primary_handle: str,
        tender_filter: TenderFilter = DEFAULT_FILTER,
        poll_interval_seconds: int = 1800,    # 30 min
        page_size: int = 100,
        skip_expired: bool = True,
        skip_rectifications_after_first: bool = True,
        prune_unmatched_days: int = 90,
        max_publication_age_hours: int = 24,
    ) -> None:
        super().__init__(name="pa-tenders", daemon=True)
        self._db_path = db_path
        self._stop = stop_event
        self._send = send_imessage
        self._handle = primary_handle
        self._filter = tender_filter
        self._interval = max(60, int(poll_interval_seconds))
        self._page_size = max(10, min(int(page_size), 500))
        self._skip_expired = skip_expired
        self._skip_rect = skip_rectifications_after_first
        self._prune_days = max(7, int(prune_unmatched_days))
        # H3 — first-run/backfill bombardement-bescherming: publicaties
        # ouder dan deze drempel triggeren geen alert, maar worden wel
        # opgeslagen voor dedupe + `tenders_search`. Dit voorkomt 5-10
        # alerts ineens bij eerste boot of bij filter-uitbreiding +
        # backfill.
        self._max_pub_age_hours = max(1, int(max_publication_age_hours))
        # Startup jitter — voorkom dat we precies op het hele uur klappen
        # tegen de TenderNed-CDN tijdens een herstart-stormpje.
        self._first_delay = random.uniform(60, 240)

    def run(self) -> None:
        log.info(
            "tender-worker started: poll=%ds size=%d skip_expired=%s "
            "skip_rect=%s",
            self._interval, self._page_size, self._skip_expired, self._skip_rect,
        )
        if self._first_delay > 0 and not self._stop.is_set():
            self._stop.wait(self._first_delay)
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception:
                log.exception("tender-worker tick failed (continuing)")
            now = time.time()
            if now - last_prune > 86400:  # daily
                try:
                    self._prune()
                except Exception:
                    log.exception("tender prune failed")
                last_prune = now
            self._stop.wait(self._interval)

    # ------------------------------------------------------------------

    def _tick_once(self) -> None:
        try:
            summaries = fetch_recent_summaries(size=self._page_size)
        except TenderNedRateLimited as e:
            # M1 — TenderNed wil dat we wachten. Skip deze tick; volgende
            # poll na de normale interval. Worker doet nog geen
            # backoff-state-machine; bij aanhoudende 429s zou je dat
            # willen toevoegen.
            log.warning("tender feed rate-limited (Retry-After=%ds); "
                        "skipping tick", e.retry_after_seconds)
            return
        except TenderNedError as e:
            log.warning("tender feed fetch failed: %s", e)
            return
        if not summaries:
            log.debug("tender feed empty")
            return

        new_count = 0
        matched_count = 0
        alert_count = 0
        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            for summary in summaries:
                try:
                    pub_id = int(summary.get("publicatieId") or 0)
                except (TypeError, ValueError):
                    continue
                if pub_id <= 0:
                    continue
                if tender_exists(conn, pub_id):
                    continue

                # Niet eerder gezien → detail ophalen en classificeren
                try:
                    detail = fetch_publication_detail(pub_id)
                except TenderNedError as e:
                    log.warning("tender detail fetch failed for %d: %s",
                                pub_id, e)
                    continue

                result = match(detail, self._filter)
                self._insert(conn, detail, result)
                new_count += 1
                if not result.matched:
                    continue
                matched_count += 1

                # Alert-policy: skip rectificaties na eerste alert in keten,
                # en skip verlopen sluitingsdatums.
                if not self._should_alert(conn, detail):
                    continue

                text = format_alert(detail, result)
                try:
                    self._send(self._handle, text)
                except Exception:
                    log.exception("tender alert send failed for %d", pub_id)
                    continue

                conn.execute(
                    "UPDATE tenders SET alerted_at = strftime('%s','now') "
                    "WHERE publicatie_id = ?",
                    (pub_id,),
                )
                alert_count += 1

        if new_count or matched_count:
            log.info(
                "tender-tick: %d new, %d matched, %d alerted",
                new_count, matched_count, alert_count,
            )

    def _should_alert(
        self, conn: sqlite3.Connection, detail: dict[str, Any],
    ) -> bool:
        # H3: Backfill-bescherming. Skip alerts voor publicaties ouder
        # dan N uur — voorkomt 5-10 alerts ineens bij first-run/restart-
        # met-lege-DB. Item wordt wel opgeslagen voor `tenders_search`
        # historie.
        if _publication_age_hours(detail.get("publicatieDatum")) > self._max_pub_age_hours:
            return False

        # Verlopen sluitingsdatums → niet alerten (geen actie meer mogelijk).
        if self._skip_expired and _is_expired(detail.get("sluitingsDatum")):
            return False

        # Rectificatie / gunning: als kenmerk al een alert had → niet
        # opnieuw alerten (tenzij sluitingsdatum is verschoven).
        if self._skip_rect:
            try:
                kenmerk = int(detail.get("kenmerk") or 0)
            except (TypeError, ValueError):
                kenmerk = 0
            if kenmerk and kenmerk_already_alerted(conn, kenmerk):
                aank_code = detail.get("aankondigingCode") or {}
                code = aank_code.get("code") if isinstance(aank_code, dict) else None
                if code in ("REC", "GUN"):
                    return False

        return True

    def _insert(
        self, conn: sqlite3.Connection, detail: dict[str, Any],
        result: Any,
    ) -> None:
        aank = detail.get("aankondigingCode") or {}
        proc = detail.get("procedureCode") or {}
        type_op = detail.get("typeOpdrachtCode") or {}

        conn.execute(
            """INSERT OR IGNORE INTO tenders
               (publicatie_id, kenmerk, aanbesteding_naam, opdrachtgever_naam,
                opdracht_beschrijving, publicatie_datum, sluitings_datum,
                type_publicatie, aankondiging_code, procedure, type_opdracht,
                cpv_codes, nuts_codes, trefwoord1, trefwoord2, link,
                matched, matched_layers, matched_terms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(detail.get("publicatieId") or 0),
                int(detail.get("kenmerk") or 0),
                str(detail.get("aanbestedingNaam") or "")[:500],
                str(detail.get("opdrachtgeverNaam") or "")[:300],
                str(detail.get("opdrachtBeschrijving") or "")[:5000],
                str(detail.get("publicatieDatum") or ""),
                str(detail.get("sluitingsDatum") or ""),
                str(detail.get("typePublicatie") or "")[:200],
                str(aank.get("code") or "") if isinstance(aank, dict) else "",
                str(proc.get("omschrijving") or "") if isinstance(proc, dict) else "",
                str(type_op.get("code") or "") if isinstance(type_op, dict) else "",
                json.dumps(detail.get("cpvCodes") or [], ensure_ascii=False),
                json.dumps(detail.get("nutsCodes") or [], ensure_ascii=False),
                str(detail.get("trefwoord1") or ""),
                str(detail.get("trefwoord2") or ""),
                overview_url(int(detail.get("publicatieId") or 0)),
                1 if result.matched else 0,
                json.dumps(list(result.layers), ensure_ascii=False),
                json.dumps(list(result.terms), ensure_ascii=False),
            ),
        )

    def _prune(self) -> None:
        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            removed = prune_old_unmatched(conn, days=self._prune_days)
        if removed:
            log.info("tender-prune: removed %d unmatched rows older than %dd",
                     removed, self._prune_days)


def _parse_tenderned_dt(iso: str | None) -> datetime | None:
    """Parse een naive ISO-timestamp van TenderNed en plak Europe/Amsterdam
    erop. TenderNed is NL-overheidsdienst — alle datums zijn lokaal NL,
    ongeacht waar the user zich bevindt (review H1)."""
    if not iso:
        return None
    try:
        s = str(iso).split(".")[0]
        if "T" not in s:
            s = s + "T23:59:59"
        dt = datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TENDERNED_TZ)
    return dt


def _is_expired(iso: str | None) -> bool:
    dt = _parse_tenderned_dt(iso)
    if dt is None:
        return False
    return dt < datetime.now(TENDERNED_TZ)


def _publication_age_hours(iso: str | None) -> float:
    """Hoe lang geleden werd dit gepubliceerd? Wordt gebruikt door H3
    backfill-bescherming. Returnt 0.0 als parse faalt (= behandelen als
    nieuw, anders missen we items)."""
    dt = _parse_tenderned_dt(iso)
    if dt is None:
        return 0.0
    delta = datetime.now(TENDERNED_TZ) - dt
    return max(0.0, delta.total_seconds() / 3600.0)
