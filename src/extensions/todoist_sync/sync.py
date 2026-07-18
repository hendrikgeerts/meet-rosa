"""Bidirectional sync logic.

push_pending() — alle nieuwe lokale items sinds vorige tick → Todoist
pull_completions() — checkt remote status per gelinkte task → markeert
                     local item als done bij remote completion
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from extensions.todoist_sync.schema import (
    get_link_by_local, get_link_by_remote, insert_link,
    mark_completed_remote, queue_enqueue_loop, touch_synced,
)
from integrations.todoist import (
    Project, TodoistClient, TodoistProjectFullError,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

# Loops met deze kinds vragen actie van the user → push naar Todoist.
# outgoing_request en meeting_action_other zijn delegate-tracking; die
# horen niet als taak op zijn lijst.
_PUSHABLE_LOOP_KINDS = (
    "incoming_question",
    "incoming_task",
    "meeting_action_self",
)


def push_pending(
    db_path: Path, client: TodoistClient, project: Project,
    *, max_items: int = 50, review_queue_loops: bool = True,
) -> int:
    """Vind reminders + actionable open_loops zonder todoist-link → push.
    Returns aantal nieuwe Todoist-tasks aangemaakt."""
    pushed = 0
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row

        # --- reminders ---
        reminder_rows = conn.execute(
            """
            SELECT r.id, r.body, r.remind_at, r.handle
              FROM reminders r
              LEFT JOIN todoist_links l
                ON l.local_kind='reminder' AND l.local_id=r.id
             WHERE r.sent_at IS NULL AND r.cancelled_at IS NULL
               AND l.id IS NULL
             ORDER BY r.id DESC
             LIMIT ?
            """, (max_items,),
        ).fetchall()

        for r in reminder_rows:
            due_iso = to_rfc3339(int(r["remind_at"]))
            content = with_date_prefix(r["body"], int(r["remind_at"]))
            try:
                task = client.create_task(
                    content=content,
                    project_id=project.id,
                    labels=["rosa-reminder"],
                    due_datetime=due_iso,
                )
            except TodoistProjectFullError:
                log.warning(
                    "todoist: project '%s' vol (max items reached). "
                    "Skip rest van deze tick. Vraag Rosa 'ruim m'n "
                    "Todoist op' om duplicaten/stale items te sluiten.",
                    project.name,
                )
                return pushed
            except Exception:
                log.exception("todoist push failed for reminder %s", r["id"])
                continue
            if insert_link(conn, kind="reminder", local_id=int(r["id"]),
                           todoist_id=task.id):
                pushed += 1
                log.info("todoist: pushed reminder #%d as task %s",
                         r["id"], task.id)

        # --- open_loops ---
        placeholders = ",".join("?" for _ in _PUSHABLE_LOOP_KINDS)
        loop_rows = conn.execute(
            f"""
            SELECT lo.id, lo.kind, lo.who, lo.title, lo.body_excerpt,
                   lo.context, lo.due_at, lo.source, lo.source_ref
              FROM open_loops lo
              LEFT JOIN todoist_links l
                ON l.local_kind='open_loop' AND l.local_id=lo.id
             WHERE lo.status='open' AND lo.kind IN ({placeholders})
               AND l.id IS NULL
             ORDER BY lo.id DESC
             LIMIT ?
            """, (*_PUSHABLE_LOOP_KINDS, max_items),
        ).fetchall()

        for r in loop_rows:
            label = _loop_label(dict(r))
            # SECURITY_REVIEW_2 MED-3: who:title leaked sender-names and
            # body_excerpt leaked mail-content to api.todoist.com. We now
            # send only the (Llama-summarised) title without the who-
            # prefix, and replace the description with a reference back
            # to the local dashboard. the user klikt door voor details.
            content = with_date_prefix(r["title"], r["due_at"])
            description = loop_description_for_todoist(int(r["id"]))
            due_iso = to_rfc3339(int(r["due_at"])) if r["due_at"] else None

            # Review-queue (sinds 28/6): geen automatische push meer voor
            # open_loops — alles landt in todoist_push_queue en wacht op
            # the user's expliciete approve/reject via Rosa. Voorkomt dat
            # het project ongemerkt volraakt. Reminders blijven WEL
            # automatisch syncen (hij vraagt ze immers zelf).
            if review_queue_loops:
                queue_enqueue_loop(
                    conn, loop_id=int(r["id"]), kind=str(r["kind"]),
                    label=label, title=str(r["title"] or ""),
                    due_at=int(r["due_at"]) if r["due_at"] else None,
                )
                continue

            try:
                task = client.create_task(
                    content=content,
                    project_id=project.id,
                    labels=[label],
                    due_datetime=due_iso,
                    description=description,
                )
            except TodoistProjectFullError:
                log.warning(
                    "todoist: project '%s' vol — open_loops sync gestopt "
                    "tot the user opruimt.", project.name,
                )
                return pushed
            except Exception:
                log.exception("todoist push failed for loop %s", r["id"])
                continue
            if insert_link(conn, kind="open_loop", local_id=int(r["id"]),
                           todoist_id=task.id):
                pushed += 1
                log.info("todoist: pushed loop #%d (%s) as task %s",
                         r["id"], r["kind"], task.id)
    return pushed


def loop_description_for_todoist(loop_id: int) -> str:
    """Description voor een open-loop op Todoist. Bevat alleen een
    referentie naar het lokale dashboard — geen body_excerpt, geen
    persoonsnaam, geen mail-content. the user klikt door om de echte
    inhoud te zien (en die staat 100% lokaal in `memory.db`).
    """
    return (
        f"Rosa loop #{loop_id} — open http://127.0.0.1:8080/loops "
        "voor afzender, body en context."
    )


def pull_completions(
    db_path: Path, client: TodoistClient, project: Project,
) -> int:
    """Fetch alle Rosa-project tasks; voor elke completed task die we
    in todoist_links kennen, markeer het lokale item als done.
    Returns aantal lokale items gemarkeerd."""
    try:
        tasks = client.list_tasks(project_id=project.id)
    except Exception:
        log.exception("todoist pull tasks failed")
        return 0

    # Todoist's `list_tasks` standaard filtert completed=False uit.
    # Voor pull-completions willen we juist completed weten — daarom
    # checken we welke gelinkte tasks NIET meer in de open-lijst staan.
    open_remote_ids = {t.id for t in tasks if not t.is_completed}

    closed_count = 0
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM todoist_links "
            "WHERE completed_at_remote IS NULL"
        ).fetchall()
        for link in rows:
            tid = link["todoist_id"]
            if tid in open_remote_ids:
                touch_synced(conn, todoist_id=tid)
                continue
            # Niet meer in de open-lijst van het project → completed,
            # of verwijderd, of verplaatst. Behandel als "done":
            # markeer lokaal én in todoist_links.
            mark_completed_remote(conn, todoist_id=tid)
            if link["local_kind"] == "reminder":
                conn.execute(
                    "UPDATE reminders SET cancelled_at=strftime('%s','now') "
                    "WHERE id=? AND sent_at IS NULL AND cancelled_at IS NULL",
                    (link["local_id"],),
                )
            else:
                conn.execute(
                    "UPDATE open_loops SET status='done', "
                    "resolved_at=strftime('%s','now'), "
                    "resolved_via='todoist' "
                    "WHERE id=? AND status='open'",
                    (link["local_id"],),
                )
            closed_count += 1
            log.info("todoist: pulled completion for %s #%s (task %s)",
                     link["local_kind"], link["local_id"], tid)
    return closed_count


def _loop_label(loop_row: dict[str, Any]) -> str:
    """Pak een Todoist-label op basis van de bron-source van de loop.
    Same heuristiek als midday._loop_to_dict."""
    source = loop_row.get("source") or "unknown"
    ref = loop_row.get("source_ref") or ""
    if source == "comm" and ref:
        prefix = ref.split(":", 1)[0]   # gmail / imap / slack
        if prefix == "slack":
            return "rosa-slack"
        return "rosa-mail"
    if source == "plaud":
        return "rosa-meeting"
    if loop_row.get("kind", "").startswith("meeting_action"):
        return "rosa-meeting"
    return "rosa-loop"


def to_rfc3339(unix_seconds: int) -> str:
    """Todoist verwacht datetime in ISO-8601 UTC (e.g. '2026-04-29T13:00:00Z')
    of met tz-offset. We sturen UTC-Z."""
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_NL_MONTHS_SHORT = ("jan", "feb", "mrt", "apr", "mei", "jun",
                     "jul", "aug", "sep", "okt", "nov", "dec")


def with_date_prefix(content: str, due_unix: int | None) -> str:
    """Prefix de Todoist-content met "DD mmm" zodat recurring reminders
    visueel onderscheidbaar zijn in de Todoist-UI. Voor reminders zonder
    due_at: geen prefix (geen datum bekend)."""
    if not due_unix:
        return content
    dt = datetime.fromtimestamp(int(due_unix), TZ)
    prefix = f"[{dt.day} {_NL_MONTHS_SHORT[dt.month - 1]}] "
    # Voorkom dubbele prefix als hij al geprefixt is (idempotent voor
    # re-sync via update-script)
    if content.startswith("[") and content[3:].startswith(" ") or \
       content.startswith("[") and "] " in content[:16]:
        return content
    return prefix + content
