"""MarketIntelWorker — eigen thread die periodiek fetched + scoort.

Sinds 24/4 gebruikt scoring Claude (via gateway.complete force_label='public')
ipv lokale Llama. Reden: Ollama saturated door comm-intel summarize backlog
op deze Intel CPU, scoring liep telkens in 240s urllib timeouts. Items zijn
publieke RSS-headlines dus geen privacy-issue om naar Claude te sturen.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from extensions.market_intel.fetch import fetch_and_store
from extensions.market_intel.score import score_pending
from extensions.market_intel.sources import ALL_SOURCES
from privacy.gateway import Gateway

log = logging.getLogger(__name__)


class MarketIntelWorker(threading.Thread):
    def __init__(
        self,
        *,
        db_path: Path,
        gateway: Gateway,
        stop_event: threading.Event,
        poll_interval_seconds: int = 7200,   # 2u
        score_batch: int = 30,
    ) -> None:
        super().__init__(name="market-intel", daemon=True)
        self._db_path = db_path
        self._gateway = gateway
        self._stop_event = stop_event
        self._poll_interval = poll_interval_seconds
        self._score_batch = score_batch

    def run(self) -> None:
        log.info(
            "market-intel started: %d sources, poll=%ss, score_batch=%d",
            len(ALL_SOURCES), self._poll_interval, self._score_batch,
        )
        # Korte initiële wachttijd zodat overige init geland is.
        self._stop_event.wait(timeout=20)
        while not self._stop_event.is_set():
            try:
                added = fetch_and_store(self._db_path, ALL_SOURCES)
                if added:
                    log.info("market-intel: +%d new items", added)
            except Exception:
                log.exception("market-intel fetch tick failed")

            if self._stop_event.is_set():
                break

            try:
                scored = score_pending(
                    self._db_path, self._gateway, limit=self._score_batch,
                )
                if scored:
                    log.info("market-intel: scored %d items", scored)
            except Exception:
                log.exception("market-intel scoring tick failed")

            self._stop_event.wait(timeout=self._poll_interval)
        log.info("market-intel stopped")
