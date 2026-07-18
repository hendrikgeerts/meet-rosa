"""Rosa-tools voor directe Todoist-toegang.

De `TodoistSyncWorker` pushed lokale reminders eenrichtingsverkeer naar
Todoist. Deze tools geven Rosa daarbovenop READ + COMPLETE + UPDATE op
de Todoist-kant zelf — the user kan nu vragen "wat staat er in m'n
Todoist", "mark X als done", "verschuif Y naar vrijdag".

Privacy: alle tool-results gaan via de orchestrator door
`<untrusted_aggregated_data>`-wrap + redactor; egress naar
api.todoist.com is al actief vanwege de sync-worker (geen nieuwe
sub-processor).
"""
from __future__ import annotations

import logging
import socket
import urllib.error
from datetime import datetime, timedelta
from typing import Any

from core.query_safety import QUERY_SCHEMA, validate_query
from extensions.todoist_sync.cleanup import (
    find_duplicates, find_stale, get_proposal,
    register_duplicate_proposal, register_stale_proposal,
)
from extensions.todoist_sync.schema import (
    queue_get, queue_list_pending, queue_mark_approved, queue_mark_rejected,
)
from extensions.todoist_sync.sync import (
    loop_description_for_todoist, to_rfc3339, with_date_prefix,
)
from integrations.todoist import Task, TodoistClient

log = logging.getLogger(__name__)


def _list_tasks_safely(
    client: TodoistClient, project_id: str | None,
) -> tuple[list[Task] | None, dict[str, Any] | None]:
    """Wrap client.list_tasks zodat een Todoist-timeout/error de
    Claude tool-call loop niet 15s blokkeert + niet als generic
    Exception terug komt. Returns (tasks, error_dict)."""
    try:
        return client.list_tasks(project_id=project_id), None
    except (urllib.error.URLError, socket.timeout) as exc:
        log.warning("todoist list_tasks network failure: %s", exc)
        return None, {"error": "todoist temporarily unreachable, try again later"}
    except urllib.error.HTTPError as exc:
        log.warning("todoist list_tasks HTTP %s", exc.code)
        return None, {"error": f"todoist HTTP {exc.code}"}


def _today_iso() -> str:
    from core.timezone import current_tz
    return datetime.now(current_tz()).date().isoformat()


def _task_due_iso(t: Task) -> str | None:
    """Pak de YYYY-MM-DD vorm in the user's active TZ — werkt voor due_date
    en due_datetime. Review 27/6 M3: due_datetime van Todoist staat in
    UTC-Z; zonder TZ-conversie geeft 23:30 NL (=22:30Z) per ongeluk
    een verkeerde dag in winter."""
    if t.due_date:
        return t.due_date
    if t.due_datetime:
        return _datetime_to_local_date(t.due_datetime)
    return None


def _datetime_to_local_date(iso: str) -> str:
    """Parse ISO-8601 (Z of +hh:mm of naive) en geef YYYY-MM-DD in
    the user's active TZ. Faalt-safe: bij parse-error fallback op
    string-prefix zodat list_tasks niet uit elkaar valt op één
    rare due_datetime."""
    from core.timezone import current_tz
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return iso[:10]
        return dt.astimezone(current_tz()).date().isoformat()
    except (ValueError, TypeError):
        return iso[:10] if isinstance(iso, str) else ""


def _task_to_dict(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "content": t.content,
        "labels": t.labels,
        "due_date": t.due_date,
        "due_datetime": t.due_datetime,
    }


def _ensure_client(client: TodoistClient | None) -> dict[str, Any] | None:
    if client is None:
        return {
            "error": "todoist not configured "
            "(set TODOIST_API_TOKEN in .env and restart)",
        }
    return None


def _todoist_list_open_tasks(
    client: TodoistClient | None, project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None  # type-narrowing

    filter_ = (args.get("filter") or "today").strip().lower()
    if filter_ not in {"today", "overdue", "week", "nodue", "all"}:
        return {"error": f"unknown filter: {filter_}"}

    raw_limit = args.get("limit", 30)
    try:
        limit = max(1, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        limit = 30

    raw_q = (args.get("query") or "").strip() or None
    if raw_q is not None:
        ok, err_msg = validate_query(raw_q)
        if not ok:
            return {"error": err_msg or "invalid query"}

    tasks, err_dict = _list_tasks_safely(client, project_id)
    if err_dict is not None:
        return err_dict
    assert tasks is not None
    today = _today_iso()
    week_end = (
        datetime.fromisoformat(today) + timedelta(days=7)
    ).date().isoformat()

    def _kept(t: Task) -> bool:
        d = _task_due_iso(t)
        if filter_ == "today":
            return d == today
        if filter_ == "overdue":
            return bool(d and d < today)
        if filter_ == "week":
            return bool(d and today <= d <= week_end)
        if filter_ == "nodue":
            return not d
        return True

    out = [t for t in tasks if _kept(t)]
    if raw_q:
        q_low = raw_q.lower()
        out = [t for t in out if q_low in t.content.lower()]
    out.sort(
        key=lambda t: (_task_due_iso(t) or "9999-99-99", t.content.lower()),
    )
    return {
        "filter": filter_,
        "count": len(out),
        "tasks": [_task_to_dict(t) for t in out[:limit]],
    }


def _todoist_complete_task(
    client: TodoistClient | None, _project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "task_id required"}
    ok = client.close_task(task_id)
    return {"task_id": task_id, "completed": ok}


def _todoist_update_task(
    client: TodoistClient | None, _project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "task_id required"}
    kwargs: dict[str, Any] = {}
    if "content" in args and args["content"] is not None:
        kwargs["content"] = str(args["content"])[:500]
    if "description" in args and args["description"] is not None:
        kwargs["description"] = str(args["description"])[:16000]
    if "due_datetime" in args and args["due_datetime"] is not None:
        raw = str(args["due_datetime"])
        # Review 27/6 L2: pre-valideer ISO-8601 zodat een Claude-fout
        # ("morgen 14:00") niet stil als API-400 terugkomt.
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": (
                    "due_datetime must be ISO 8601 (e.g. '2026-06-30T15:00:00' "
                    "or with TZ offset). Call get_current_time first to "
                    "resolve relative dates."
                ),
                "received": raw,
            }
        kwargs["due_datetime"] = raw
    if not kwargs:
        return {"error": "no fields to update (use content/description/due_datetime)"}
    ok = client.update_task(task_id, **kwargs)
    return {
        "task_id": task_id, "updated": ok, "fields": list(kwargs.keys()),
    }


def _todoist_create_task(
    client: TodoistClient | None, project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Maak een Todoist-taak DIRECT aan, zonder set_reminder-omweg.
    Gebruik dit als the user expliciet zegt 'voeg toe aan Todoist
    maar herinner me niet' of als hij een gestructureerde taak wil
    met labels/priority die set_reminder niet ondersteunt."""
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    content = (args.get("content") or "").strip()
    if not content:
        return {"error": "content required"}
    if len(content) > 500:
        content = content[:500]

    due_datetime = args.get("due_datetime")
    if due_datetime is not None:
        raw = str(due_datetime)
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return {
                "error": (
                    "due_datetime must be ISO 8601 (e.g. '2026-06-30T15:00:00'). "
                    "Call get_current_time first."
                ),
                "received": raw,
            }
        due_datetime = raw

    raw_labels = args.get("labels") or []
    if not isinstance(raw_labels, list):
        return {"error": "labels must be a list of strings"}
    labels = [str(l) for l in raw_labels if l][:10]

    description = args.get("description")
    if description is not None:
        description = str(description)[:16000]

    try:
        task = client.create_task(
            content=content,
            project_id=project_id,
            labels=labels or None,
            due_datetime=due_datetime,
            description=description,
        )
    except Exception as exc:  # incl. TodoistProjectFullError
        from integrations.todoist import TodoistProjectFullError
        if isinstance(exc, TodoistProjectFullError):
            return {
                "error": (
                    "Todoist project is full (max items reached). "
                    "Ask the user to run cleanup ('ruim m'n Todoist op') "
                    "to close duplicates/stale items first."
                ),
            }
        log.warning("todoist create_task failed: %s", exc)
        return {"error": f"todoist create_task failed: {type(exc).__name__}"}

    return {
        "task_id": task.id,
        "content": task.content,
        "due_datetime": task.due_datetime,
        "labels": task.labels,
    }


def _todoist_search(
    client: TodoistClient | None, project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    raw_q = str(args.get("query") or "")
    ok, err_msg = validate_query(raw_q)
    if not ok:
        return {"error": err_msg or "invalid query"}

    tasks, err_dict = _list_tasks_safely(client, project_id)
    if err_dict is not None:
        return err_dict
    assert tasks is not None
    q_low = raw_q.strip().lower()
    matched = [t for t in tasks if q_low in t.content.lower()]
    matched.sort(
        key=lambda t: (_task_due_iso(t) or "9999-99-99", t.content.lower()),
    )
    return {
        "query": raw_q.strip(),
        "count": len(matched),
        "tasks": [_task_to_dict(t) for t in matched[:30]],
    }


def _todoist_cleanup_suggest(
    client: TodoistClient | None, project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Vind duplicaten + stale-tasks; geef voorgestelde acties terug
    met proposal_ids. Voert NIETS uit — the user moet bevestigen via
    todoist_cleanup_apply."""
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    raw_days = args.get("stale_days_threshold", 30)
    try:
        stale_days = max(1, min(int(raw_days), 365))
    except (TypeError, ValueError):
        stale_days = 30
    include_dups = bool(args.get("include_duplicates", True))
    include_stale = bool(args.get("include_stale", True))

    tasks, err_dict = _list_tasks_safely(client, project_id)
    if err_dict is not None:
        return err_dict
    assert tasks is not None

    dups: list[dict[str, Any]] = []
    if include_dups:
        for dup in find_duplicates(tasks):
            pid = register_duplicate_proposal(dup)
            dups.append({
                "proposal_id": pid,
                "keep": {"id": dup.keep_id, "content": dup.keep_content},
                "drop": {"id": dup.drop_id, "content": dup.drop_content},
                "seq_ratio": dup.seq_ratio,
                "jaccard": dup.jaccard,
            })

    stales: list[dict[str, Any]] = []
    if include_stale:
        for s in find_stale(tasks, days_threshold=stale_days):
            pid = register_stale_proposal(s)
            stales.append({
                "proposal_id": pid,
                "task_id": s.task_id,
                "content": s.content,
                "age_days": s.age_days,
            })

    return {
        "duplicates": dups,
        "stale": stales,
        "total_open_tasks": len(tasks),
        "stale_days_threshold": stale_days,
        "note": (
            "Geen acties uitgevoerd. Roep todoist_cleanup_apply aan met "
            "de proposal_ids die je wilt uitvoeren."
        ),
    }


def _todoist_review_queue_list(
    db_path: Any, _client: Any, _project_id: Any, args: dict[str, Any],
) -> dict[str, Any]:
    """Lijst pending items uit todoist_push_queue zodat the user per
    item kan approven of rejecten."""
    raw_limit = args.get("limit", 30)
    try:
        limit = max(1, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        limit = 30
    import sqlite3 as _sql
    with _sql.connect(db_path, isolation_level=None) as conn:
        rows = queue_list_pending(conn, limit=limit)
    out_items: list[dict[str, Any]] = []
    for r in rows:
        out_items.append({
            "queue_id": r["id"],
            "loop_id": r["loop_id"],
            "kind": r["kind"],
            "label": r["label"],
            "title": r["title"],
            "due_at": r.get("due_at"),
            "created_at": r["created_at"],
        })
    return {"count": len(out_items), "items": out_items}


# Cap mass-batch hetzelfde idee als cleanup_apply.
# Review-fix H2: cap=5 (was 10) — aligned met cleanup_apply zodat één
# prompt-injection-batch hooguit 5 items meekrijgt. the user kan in
# meerdere turns alsnog grotere batches doen — meer-turns is precies
# de UX-cost die we hier willen betalen voor zekerheid.
_MAX_REVIEW_PER_CALL = 5


def _todoist_review_queue_approve(
    db_path: Any, client: TodoistClient | None, project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Voor opgegeven queue_ids: push naar Todoist en markeer approved.
    Cap N voorkomt prompt-injection-gedreven mass-approval."""
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    raw_ids = args.get("queue_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return {"error": "queue_ids required (list of integers from todoist_review_queue_list)"}
    if len(raw_ids) > _MAX_REVIEW_PER_CALL:
        return {
            "error": f"too many queue_ids in one call (max {_MAX_REVIEW_PER_CALL})",
            "received": len(raw_ids),
        }

    pushed: list[dict[str, Any]] = []
    unknown: list[int] = []
    failed: list[dict[str, Any]] = []
    project_full = False

    import sqlite3 as _sql
    from integrations.todoist import TodoistProjectFullError

    with _sql.connect(db_path, isolation_level=None) as conn:
        for raw_id in raw_ids:
            try:
                qid = int(raw_id)
            except (TypeError, ValueError):
                unknown.append(raw_id)
                continue
            row = queue_get(conn, qid)
            if row is None or row["state"] != "pending":
                unknown.append(qid)
                continue
            content = with_date_prefix(row["title"], row.get("due_at"))
            description = loop_description_for_todoist(int(row["loop_id"]))
            # H3 review-fix: due_at=0 is theoretisch Unix-epoch maar
            # behandelt 'is not None' veiliger dan falsy-test.
            due_iso = (
                _to_rfc3339(int(row["due_at"]))
                if row.get("due_at") is not None else None
            )
            try:
                task = client.create_task(
                    content=content, project_id=project_id,
                    labels=[row["label"]], due_datetime=due_iso,
                    description=description,
                )
            except TodoistProjectFullError:
                project_full = True
                break
            except Exception as exc:
                failed.append({"queue_id": qid, "error": type(exc).__name__})
                continue
            # Review-fix H1: insert_link MOET vóór queue_mark_approved
            # slagen — anders is de Todoist-task er, queue zegt approved,
            # maar `pull_completions` heeft geen link om de open_loop bij
            # remote-ack te sluiten. Bij link-faal: laat queue pending
            # (the user kan retryen, dan vinden we mismatched-state).
            from extensions.todoist_sync.schema import insert_link
            try:
                link_ok = insert_link(
                    conn, kind="open_loop",
                    local_id=int(row["loop_id"]), todoist_id=task.id,
                )
            except Exception:
                log.exception("approve: insert_link faalde voor loop %s",
                              row["loop_id"])
                link_ok = False
            if not link_ok:
                # task bestaat in Todoist maar we kunnen 'em niet linken —
                # log + report als 'failed' zodat the user weet dat dit een
                # zwerver is. Queue blijft pending.
                failed.append({
                    "queue_id": qid, "task_id": task.id,
                    "error": "link_insert_failed",
                })
                continue
            queue_mark_approved(conn, queue_id=qid, todoist_id=task.id)
            pushed.append({"queue_id": qid, "task_id": task.id,
                           "title": content})

    if project_full:
        summary = (
            f"First {len(pushed)} pushed, then Todoist hit the project "
            "max-items limit. Remaining queue_ids stay pending — ask "
            "the user to run cleanup ('ruim m'n Todoist op'), then try "
            "the remaining queue_ids again."
        )
    else:
        summary = (
            f"pushed {len(pushed)}, unknown {len(unknown)}, "
            f"failed {len(failed)}"
        )
    return {
        "pushed": pushed,
        "unknown_queue_ids": unknown,
        "failed": failed,
        "project_full": project_full,
        "summary": summary,
    }


def _todoist_review_queue_reject(
    db_path: Any, _client: Any, _project_id: Any, args: dict[str, Any],
) -> dict[str, Any]:
    """Mark queue-items als rejected — geen Todoist-push. Lokaal blijft
    de open_loop wel in whats_open zichtbaar (rejecten ≠ closen)."""
    raw_ids = args.get("queue_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return {"error": "queue_ids required"}
    if len(raw_ids) > _MAX_REVIEW_PER_CALL:
        return {"error": f"too many queue_ids in one call (max {_MAX_REVIEW_PER_CALL})"}

    import sqlite3 as _sql
    rejected: list[int] = []
    unknown: list[int] = []
    with _sql.connect(db_path, isolation_level=None) as conn:
        for raw_id in raw_ids:
            try:
                qid = int(raw_id)
            except (TypeError, ValueError):
                unknown.append(raw_id)
                continue
            row = queue_get(conn, qid)
            if row is None or row["state"] != "pending":
                unknown.append(qid)
                continue
            queue_mark_rejected(conn, qid)
            rejected.append(qid)
    return {"rejected": rejected, "unknown_queue_ids": unknown}


# Review 27/6 H2: cap apply-batches op N proposals zodat een prompt-injection
# die Rosa overtuigt direct alle proposal_ids door te sturen geen mass-close
# kan triggeren. the user kan in praktijk per turn 1-5 items bevestigen; meer
# dan dat is altijd een spike-signal.
_MAX_APPLY_PER_CALL = 5


def _todoist_cleanup_apply(
    client: TodoistClient | None, _project_id: str | None,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Voer cleanup-acties uit voor expliciet doorgegeven proposal_ids.
    Onbekende of verlopen proposal_ids worden geskipt en in 'unknown'
    gerapporteerd. Cap N=_MAX_APPLY_PER_CALL voorkomt prompt-injection-
    gedreven mass-close."""
    err = _ensure_client(client)
    if err:
        return err
    assert client is not None

    raw_ids = args.get("proposal_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return {"error": "proposal_ids required (list of strings from todoist_cleanup_suggest)"}

    if len(raw_ids) > _MAX_APPLY_PER_CALL:
        return {
            "error": (
                f"too many proposal_ids in one call (max {_MAX_APPLY_PER_CALL}). "
                "Ask the user to confirm in smaller batches."
            ),
            "received": len(raw_ids),
            "max_per_call": _MAX_APPLY_PER_CALL,
        }

    closed: list[dict[str, Any]] = []
    unknown: list[str] = []
    failed: list[dict[str, Any]] = []

    for pid in raw_ids:
        spec = get_proposal(str(pid))
        if not spec:
            unknown.append(str(pid))
            continue
        if spec["action"] == "close":
            ok = client.close_task(spec["task_id"])
            if ok:
                closed.append({
                    "proposal_id": pid, "task_id": spec["task_id"],
                    "kind": spec["kind"],
                })
            else:
                failed.append({"proposal_id": pid, "task_id": spec["task_id"]})

    return {
        "closed": closed,
        "unknown_proposal_ids": unknown,
        "failed": failed,
        "summary": f"closed {len(closed)}, unknown {len(unknown)}, failed {len(failed)}",
    }


TODOIST_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "todoist_list_open_tasks",
        "description": (
            "List open tasks from the user's Todoist project. Use when he "
            "asks 'what's in my Todoist', 'what's due today in Todoist', "
            "'show overdue Todoist tasks', 'what does my Todoist look like'. "
            "filter='today' (default) | 'overdue' | 'week' | 'nodue' | 'all'. "
            "Sorted by due date ascending. Returns id + content + labels + "
            "due_date/due_datetime per task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["today", "overdue", "week", "nodue", "all"],
                    "default": "today",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1, "maximum": 100, "default": 30,
                },
                "query": {
                    **QUERY_SCHEMA,
                    "description": (
                        "Optional substring filter on task content "
                        "(≥3 chars, no wildcards)."
                    ),
                },
            },
        },
    },
    {
        "name": "todoist_complete_task",
        "description": (
            "Mark a Todoist task as completed (closed). Use when the user "
            "says 'mark X done', 'complete that Todoist task', 'klaar met "
            "die taak'. Get the task_id from todoist_list_open_tasks or "
            "todoist_search first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Todoist task ID (string).",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "todoist_update_task",
        "description": (
            "Update an existing Todoist task — change content, description, "
            "or due_datetime. Use when the user says 'move that task to "
            "Friday 3pm in Todoist', 'rename X to Y in Todoist'. "
            "due_datetime is ISO 8601 in local timezone. Call "
            "get_current_time first for relative dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "content": {"type": "string", "maxLength": 500},
                "description": {"type": "string", "maxLength": 16000},
                "due_datetime": {
                    "type": "string",
                    "description": "ISO 8601 datetime in local TZ.",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "todoist_create_task",
        "description": (
            "Create a Todoist task directly (NOT via set_reminder). Use when "
            "the user says 'add to Todoist but don't remind me', or wants "
            "structured labels/priority that set_reminder doesn't support. "
            "For the default 'remind me about X at Y' flow, prefer set_reminder "
            "(auto-syncs to Todoist within ~30s)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string", "minLength": 1, "maxLength": 500,
                },
                "due_datetime": {
                    "type": "string",
                    "description": "Optional ISO 8601 datetime.",
                },
                "labels": {
                    "type": "array", "items": {"type": "string"},
                    "maxItems": 10,
                },
                "description": {"type": "string", "maxLength": 16000},
            },
            "required": ["content"],
        },
    },
    {
        "name": "todoist_search",
        "description": (
            "Search the user's open Todoist tasks by keyword (substring, "
            "case-insensitive). Use when he asks 'find that Todoist task "
            "about X', 'is there already a Todoist task for Y'. ≥3 chars, "
            "no wildcards."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {**QUERY_SCHEMA},
            },
            "required": ["query"],
        },
    },
    {
        "name": "todoist_review_queue_list",
        "description": (
            "List pending items in the user's Todoist review-queue. "
            "Auto-detected open_loops from mail/Slack/Plaud land here "
            "instead of auto-pushing to Todoist — the user decides per "
            "item. Call when he asks 'what's waiting to go to Todoist', "
            "'review my queue', or proactively in dayclose. Each item "
            "has a queue_id needed for approve/reject."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 100,
                    "default": 30,
                },
            },
        },
    },
    {
        "name": "todoist_review_queue_approve",
        "description": (
            "Push approved queue-items to Todoist. Use ONLY for queue_ids "
            "the user explicitly approved — never auto-approve all. Cap is "
            "10 per call. After approve: a Todoist task is created and "
            "linked to the original open_loop (closing the task in "
            "Todoist will also close the loop locally)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queue_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1, "maxItems": 5,
                },
            },
            "required": ["queue_ids"],
        },
    },
    {
        "name": "todoist_review_queue_reject",
        "description": (
            "Mark queue-items as rejected (no Todoist-push). The underlying "
            "open_loop stays visible in whats_open. Use when the user says "
            "'skip those', 'don't put those in Todoist'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queue_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1, "maxItems": 5,
                },
            },
            "required": ["queue_ids"],
        },
    },
    {
        "name": "todoist_cleanup_suggest",
        "description": (
            "Scan the user's open Todoist tasks for duplicates and stale "
            "items (open >N days without a due-date). Returns proposals "
            "with proposal_ids — DOES NOT execute. Use when the user says "
            "'ruim m'n Todoist op', 'clean up my Todoist', 'are there "
            "duplicates'. After showing the proposals, ask him which "
            "proposal_ids to apply via todoist_cleanup_apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stale_days_threshold": {
                    "type": "integer", "minimum": 1, "maximum": 365,
                    "default": 30,
                    "description": "How many days open without due-date = stale.",
                },
                "include_duplicates": {"type": "boolean", "default": True},
                "include_stale": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "todoist_cleanup_apply",
        "description": (
            "Execute cleanup proposals returned by todoist_cleanup_suggest. "
            "Pass the explicit list of proposal_ids the user confirmed. "
            "Only call AFTER the user explicitly approved a subset; do NOT "
            "auto-apply all proposals. Cap is 5 proposals per call — if "
            "the user confirms more, split into multiple calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "List of proposal_ids to execute.",
                },
            },
            "required": ["proposal_ids"],
        },
    },
]


TODOIST_HANDLERS: dict[str, Any] = {
    "todoist_list_open_tasks": _todoist_list_open_tasks,
    "todoist_complete_task": _todoist_complete_task,
    "todoist_update_task": _todoist_update_task,
    "todoist_create_task": _todoist_create_task,
    "todoist_search": _todoist_search,
    "todoist_cleanup_suggest": _todoist_cleanup_suggest,
    "todoist_cleanup_apply": _todoist_cleanup_apply,
    "todoist_review_queue_list": _todoist_review_queue_list,
    "todoist_review_queue_approve": _todoist_review_queue_approve,
    "todoist_review_queue_reject": _todoist_review_queue_reject,
}
