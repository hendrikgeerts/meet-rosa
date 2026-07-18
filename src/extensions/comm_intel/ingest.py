"""IngestWorker: eigen thread die periodiek alle sources poll't, lokaal
samenvat en naar `comm_items` schrijft.

Loopt los van de scheduler-thread zodat de Ollama-calls (5-30s elk) de
briefings/reminders niet blokkeren. Per source-account wordt high-water-mark
bijgehouden in `comm_ingest_state`. Eerste run = backfill 3 dagen
(configureerbaar later).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from extensions.comm_intel.schema import (
    insert_item,
    item_exists,
    load_state,
    upsert_state,
)
from extensions.comm_intel.summarize import summarize
from extensions.open_loops.detect import sync_for_comm_item
from extensions.travel_alerts.parser import (
    is_location_message,
    parse_location_body,
)
from extensions.travel_alerts.schema import insert_location
from models.ollama import OllamaClient

log = logging.getLogger(__name__)


class IngestWorker(threading.Thread):
    def __init__(
        self,
        *,
        db_path: Path,
        sources: list[Any],          # CommSource-shaped (duck-typed)
        ollama: OllamaClient,
        stop_event: threading.Event,
        poll_interval_seconds: int = 300,
        backfill_days: int = 3,
        per_poll_cap: int = 30,
        summarize_enabled: bool = True,
        embedding_enabled: bool = True,
        own_email_domains: tuple[str, ...] = (),
        on_item_added: Callable[[dict[str, Any]], None] | None = None,
        location_min_interval_seconds: int = 3600,
        sales_auto_touchpoint_enabled: bool = True,
    ) -> None:
        super().__init__(name="comm-ingest", daemon=True)
        self._db_path = db_path
        self._sources = sources
        self._ollama = ollama
        self._stop_event = stop_event
        self._poll_interval = poll_interval_seconds
        self._backfill_days = backfill_days
        self._per_poll_cap = per_poll_cap
        self._summarize_enabled = summarize_enabled
        self._sales_auto_touchpoint_enabled = sales_auto_touchpoint_enabled
        self._embedding_enabled = embedding_enabled
        self._own_email_domains = own_email_domains
        self._on_item_added = on_item_added
        self._location_min_interval = location_min_interval_seconds

    def run(self) -> None:
        log.info(
            "comm-ingest started: %d sources, poll=%ss, backfill=%dd, cap=%d/tick",
            len(self._sources), self._poll_interval, self._backfill_days, self._per_poll_cap,
        )
        # Wait briefly so Gmail/Slack auth has settled before first poll.
        self._stop_event.wait(timeout=15)
        while not self._stop_event.is_set():
            for source in self._sources:
                if self._stop_event.is_set():
                    break
                try:
                    n = self._ingest_one(source)
                    if n:
                        log.info("comm-ingest %s/%s: +%d items",
                                 source.source, source.account, n)
                except Exception:
                    log.exception("comm-ingest %s/%s failed",
                                  getattr(source, "source", "?"),
                                  getattr(source, "account", "?"))
            self._stop_event.wait(timeout=self._poll_interval)
        log.info("comm-ingest stopped")

    def _ingest_one(self, source: Any) -> int:
        folder = getattr(source, "folder", "") or ""
        with _conn(self._db_path) as conn:
            state = load_state(conn, source=source.source, account=source.account, folder=folder)

        if state and state.get("last_occurred_at"):
            since_unix = int(state["last_occurred_at"]) - 60
            last_id = state.get("last_external_id")
        else:
            import time as _time
            since_unix = int(_time.time()) - self._backfill_days * 86400
            last_id = None

        new_count = 0
        max_occurred = state.get("last_occurred_at") if state else None
        max_id = last_id
        seen_now = 0

        for item in source.fetch_new(
            last_external_id=last_id, since_unix=since_unix, limit=self._per_poll_cap,
        ):
            if self._stop_event.is_set():
                break
            seen_now += 1
            with _conn(self._db_path) as conn:
                if item_exists(conn, source=item.source, account=item.account,
                               external_id=item.external_id):
                    continue

            # PA-LOC short-circuit: phone-locatie email van iOS Shortcut.
            # Skip Llama-summarize + open-loop creatie (geen actie nodig);
            # parse coords + opslag in current_location, label intent='fyi'.
            is_location = is_location_message(item.subject)
            if is_location:
                summary_text = "(phone-locatie update)"
                intent = "fyi"
                sentiment = "neutral"
                coords = parse_location_body(item.body_full or "")
                if coords:
                    lat, lon, acc = coords
                    with _conn(self._db_path) as conn:
                        try:
                            insert_location(
                                conn, lat=lat, lon=lon, accuracy_m=acc,
                                source="ios_shortcut",
                                received_at=item.occurred_at,
                                min_interval_seconds=self._location_min_interval,
                            )
                        except Exception:
                            log.exception("travel-alerts: location insert failed")
                    # Auto-archive de PA-LOC mail zodat the user's inbox
                    # niet volloopt — coords zijn al verwerkt, mail heeft
                    # geen interactieve waarde meer. Mail blijft in All
                    # Mail voor audit. Source-class implementeert het
                    # (GmailSource: removeLabelIds=INBOX; IMAP: no-op).
                    if hasattr(source, "archive"):
                        try:
                            source.archive(item.external_id)
                        except Exception:
                            log.exception("PA-LOC auto-archive failed for %s",
                                          item.external_id)
                else:
                    log.warning("PA-LOC mail kon niet geparsed worden: %s",
                                (item.body_full or "")[:200])
            elif self._summarize_enabled:
                s = summarize(item, self._ollama,
                              own_email_domains=self._own_email_domains)
                summary_text, intent, sentiment = s.summary, s.intent, s.sentiment
            else:
                summary_text = None
                intent = None
                sentiment = None

            with _conn(self._db_path) as conn:
                rid = insert_item(conn, item, summary=summary_text,
                                  intent=intent, sentiment=sentiment)
                if rid and not is_location and self._embedding_enabled:
                    # Embedding voor RAG-zoektocht (comm_semantic_search).
                    # Best-effort — als embedding faalt (Ollama down) gaat
                    # de ingest gewoon door; historical_index.py kan later
                    # bijwerken. Kan uit via embedding_enabled=False zodat
                    # batch-indexer niet competeert om Ollama-queue.
                    try:
                        from extensions.comm_intel.embeddings import (
                            _open_with_vec,
                            embed,
                            upsert_embedding,
                        )
                        text = (item.subject or "") + "\n\n" + (item.body_full or "")[:4000]
                        if text.strip():
                            vec = embed(text, kind="document")
                            if vec is not None:
                                with _open_with_vec(self._db_path) as vc:
                                    upsert_embedding(vc, rid, vec)
                    except Exception:
                        log.exception("embedding insert failed for item %d", rid)
                    # Open-loops integration: detect.sync_for_comm_item
                    # doet zowel TRACK (open nieuwe loop bij actie-signaal)
                    # als CLOSE (sluit matching loops in zelfde thread) —
                    # bidirectional voor in én out. PA-LOC mails skippen
                    # we hier (al gedaan via is_location).
                    try:
                        sync_for_comm_item(conn, item, intent=intent,
                                            ollama=self._ollama)
                    except Exception:
                        log.exception("open-loop sync failed for %s/%s/%s",
                                      item.source, item.account, item.external_id)
                    # Sales auto-touchpoint: detecteer of dit item een
                    # interactie met een sales_account is. Eigen DB-tx
                    # binnen de helper zodat ingest-tx hier kort blijft.
                    try:
                        from extensions.sales.auto_touchpoint import (
                            maybe_log_touchpoint,
                        )
                        maybe_log_touchpoint(
                            self._db_path, item,
                            enabled=self._sales_auto_touchpoint_enabled,
                        )
                    except Exception:
                        log.exception(
                            "sales auto-touchpoint failed for %s/%s/%s",
                            item.source, item.account, item.external_id,
                        )
            if rid:
                new_count += 1
                if max_occurred is None or item.occurred_at > max_occurred:
                    max_occurred = item.occurred_at
                    max_id = item.external_id
                # Post-insert hook: laat extensies (scheduler_assist) reageren
                # op de net opgeslagen item zonder dat ingest van die
                # extensies hoeft te weten. Hook krijgt een dict met de
                # meest gebruikte velden + de DB-id.
                if self._on_item_added and not is_location:
                    try:
                        self._on_item_added({
                            "id": rid,
                            "source": item.source,
                            "account": item.account,
                            "external_id": item.external_id,
                            "direction": item.direction,
                            "from_addr": item.from_addr,
                            "to_addrs": item.to_addrs,
                            "subject": item.subject,
                            "body_full": item.body_full,
                            "thread_ref": item.thread_ref,
                            "intent": intent,
                            "sentiment": sentiment,
                            "summary": summary_text,
                        })
                    except Exception:
                        log.exception("on_item_added hook failed for %s/%s/%s",
                                      item.source, item.account, item.external_id)

            if seen_now >= self._per_poll_cap:
                break

        # Always update last_polled_at; last_external_id/last_occurred_at only
        # if we actually advanced.
        with _conn(self._db_path) as conn:
            upsert_state(
                conn, source=source.source, account=source.account, folder=folder,
                last_external_id=max_id, last_occurred_at=max_occurred,
            )
        return new_count


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def build_sources(
    *,
    gmail_client: Any | None = None,
    imap_yaml: Path | None = None,
    slack_yaml: Path | None = None,
) -> list[Any]:
    """Construct one CommSource per Gmail / IMAP-folder / Slack-workspace."""
    sources: list[Any] = []

    if gmail_client is not None:
        from extensions.comm_intel.sources.gmail_source import GmailSource
        sources.append(GmailSource(gmail_client))

    if imap_yaml is not None:
        from extensions.comm_intel.sources.imap_source import ImapSource
        from integrations.imap import all_enabled as imap_all_enabled
        for acc, pw in imap_all_enabled(imap_yaml):
            sources.append(ImapSource(acc, pw, acc.folders.inbox, "in"))
            sources.append(ImapSource(acc, pw, acc.folders.sent, "out"))

    if slack_yaml is not None:
        from extensions.comm_intel.sources.slack_source import SlackSource
        from integrations.slack import all_enabled as slack_all_enabled
        for ws, token in slack_all_enabled(slack_yaml):
            sources.append(SlackSource(ws, token))

    return sources
