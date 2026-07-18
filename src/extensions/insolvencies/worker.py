"""Polling-thread voor faillissementsdossier.nl RSS-feed.

Default 30 min interval, jitter bij startup, daily prune. Per nieuwe
publicatie:
  - parse + match
  - insert in `insolvencies`
  - bij match + recent + nog niet eerder gealert: iMessage-alert

Backfill-bescherming: items waarvan pub_date > 7 dagen geleden alerten
niet (lager risico dan tenders-vraag van 24u — faillissementsdata
is per definitie minder vluchtig en je wilt een gemiste alert van
gisteren wél nog).
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from .alerts import format_alert
from .feed import (
    FaillissementsFeedError,
    InsolvencyItem,
    fetch_and_parse,
)
from .matcher import DEFAULT_FILTER, InsolvencyFilter, match
from .schema import insolvency_exists, prune_old_unmatched

log = logging.getLogger(__name__)


class InsolvenciesWorker(threading.Thread):
    def __init__(
        self,
        *,
        db_path: Path,
        stop_event: threading.Event,
        send_imessage: Callable[[str, str], None],
        primary_handle: str,
        insolvency_filter: InsolvencyFilter = DEFAULT_FILTER,
        poll_interval_seconds: int = 1800,
        max_publication_age_days: int = 7,
        prune_unmatched_days: int = 90,
    ) -> None:
        super().__init__(name="pa-insolvencies", daemon=True)
        self._db_path = db_path
        self._stop = stop_event
        self._send = send_imessage
        self._handle = primary_handle
        self._filter = insolvency_filter
        self._interval = max(60, int(poll_interval_seconds))
        self._max_age_days = max(1, int(max_publication_age_days))
        self._prune_days = max(7, int(prune_unmatched_days))
        self._first_delay = random.uniform(60, 240)

    def run(self) -> None:
        log.info(
            "insolvencies-worker started: poll=%ds max_age=%dd",
            self._interval, self._max_age_days,
        )
        if self._first_delay > 0 and not self._stop.is_set():
            self._stop.wait(self._first_delay)
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception:
                log.exception("insolvencies tick failed (continuing)")
            now = time.time()
            if now - last_prune > 86400:
                try:
                    self._prune()
                except Exception:
                    log.exception("insolvencies prune failed")
                last_prune = now
            self._stop.wait(self._interval)

    # ------------------------------------------------------------------

    def _tick_once(self) -> None:
        try:
            items = fetch_and_parse()
        except FaillissementsFeedError as e:
            log.warning("insolvencies feed fetch failed: %s", e)
            return
        if not items:
            return

        # Fase 1: classificeer + insert in DB. Geen iMessage-IO hier
        # zodat de writer-lock zo kort mogelijk wordt vastgehouden (H1).
        new_count = 0
        matched_count = 0
        to_alert: list[tuple[InsolvencyItem, object]] = []
        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            for item in items:
                if insolvency_exists(conn, item.link):
                    continue
                result = match(item, self._filter, watchlist_conn=conn)
                self._insert(conn, item, result)
                new_count += 1
                if not result.matched:
                    continue
                matched_count += 1
                if self._should_alert(item):
                    to_alert.append((item, result))

        # Fase 2: iMessage-send buiten de DB-connectie. Per succesvolle
        # send een korte tx voor alerted_at — bij crash midden in deze
        # lus krijgt the user bij de volgende tick hooguit een dubbele
        # alert (insolvencies blijft gemarkeerd matched=1, alerted_at is
        # NULL → kandidaat voor next tick), wat veiliger is dan "send
        # gemist".
        alert_count = 0
        for item, result in to_alert:
            text = format_alert(item, result)
            try:
                self._send(self._handle, text)
            except Exception:
                log.exception("insolvencies alert send failed for %s",
                              item.link)
                continue
            try:
                with sqlite3.connect(
                    self._db_path, isolation_level=None,
                ) as conn:
                    conn.execute(
                        "UPDATE insolvencies SET alerted_at = "
                        "strftime('%s','now') WHERE link = ?",
                        (item.link,),
                    )
                alert_count += 1
            except sqlite3.Error:
                log.exception(
                    "insolvencies could not mark alerted_at for %s",
                    item.link,
                )

        if new_count or matched_count:
            log.info(
                "insolvencies-tick: %d new, %d matched, %d alerted",
                new_count, matched_count, alert_count,
            )

    def _should_alert(self, item: InsolvencyItem) -> bool:
        """Backfill-bescherming: alerteer alleen voor publicaties uit de
        afgelopen `max_age_days`. Items komen wel in de DB voor history."""
        age_days = _publication_age_days(item.pub_date)
        return age_days <= self._max_age_days

    def _insert(
        self, conn: sqlite3.Connection, item: InsolvencyItem, result,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO insolvencies
               (link, naam, kvk, plaats, provincie, rechtbank, curator,
                insolventie_nr, status, hoofd_activiteit, raw_description,
                pub_date, pub_at_unix, matched, matched_layers, matched_terms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.link, item.naam[:300], item.kvk, item.plaats,
                item.provincie, item.rechtbank, item.curator,
                item.insolventie_nr, item.status,
                (item.hoofd_activiteit or "")[:1000],
                item.description_raw[:4000],
                item.pub_date,
                item.pub_at_unix,
                1 if result.matched else 0,
                json.dumps(list(result.layers), ensure_ascii=False),
                json.dumps(list(result.terms), ensure_ascii=False),
            ),
        )

    def _prune(self) -> None:
        with sqlite3.connect(self._db_path, isolation_level=None) as conn:
            removed = prune_old_unmatched(conn, days=self._prune_days)
        if removed:
            log.info(
                "insolvencies-prune: removed %d unmatched rows >%dd",
                removed, self._prune_days,
            )


def _publication_age_days(pub_date: str | None) -> float:
    """RFC2822 (RSS pubDate) → leeftijd in dagen. Onbekend = 0 (= behandelen
    als nieuw, anders missen we items als parsing van een nieuwe variant
    faalt)."""
    if not pub_date:
        return 0.0
    try:
        dt = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return 0.0
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    return max(0.0, delta.total_seconds() / 86400.0)
