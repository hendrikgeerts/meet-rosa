"""TodoistSyncWorker — eigen thread die elke N min push + pull doet."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from extensions.todoist_sync.sync import pull_completions, push_pending
from integrations.todoist import Project, TodoistClient

log = logging.getLogger(__name__)


class TodoistSyncWorker(threading.Thread):
    def __init__(
        self,
        *,
        db_path: Path,
        client: TodoistClient,
        project: Project,
        stop_event: threading.Event,
        sync_interval_seconds: int = 300,   # 5 min
        review_queue_loops: bool = True,
    ) -> None:
        super().__init__(name="todoist-sync", daemon=True)
        self._db_path = db_path
        self._client = client
        self._project = project
        self._stop_event = stop_event
        self._interval = sync_interval_seconds
        self._review_queue_loops = review_queue_loops

    def run(self) -> None:
        log.info(
            "todoist-sync started: project=%s (id=%s), interval=%ss",
            self._project.name, self._project.id, self._interval,
        )
        # Korte initiële wachttijd zodat overige init geland is.
        self._stop_event.wait(timeout=10)
        while not self._stop_event.is_set():
            try:
                pushed = push_pending(
                    self._db_path, self._client, self._project,
                    review_queue_loops=self._review_queue_loops,
                )
                if pushed:
                    log.info("todoist-sync: +%d task(s) pushed", pushed)
            except Exception:
                log.exception("todoist-sync push tick failed")

            if self._stop_event.is_set():
                break

            try:
                closed = pull_completions(self._db_path, self._client, self._project)
                if closed:
                    log.info("todoist-sync: %d completion(s) pulled", closed)
            except Exception:
                log.exception("todoist-sync pull tick failed")

            self._stop_event.wait(timeout=self._interval)
        log.info("todoist-sync stopped")
