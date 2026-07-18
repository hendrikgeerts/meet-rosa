"""Todoist Unified API v1 client — minimal subset that pa-agent needs.

Auth: Bearer-token uit `.env`. Endpoints: tasks (create/list/close/reopen)
en projects (list/create). Gebruikt de nieuwe unified API
(`/api/v1/`) — REST v2 (`/rest/v2/`) is per 2025 gradueel uitgefaseerd
en geeft 410 Gone op /projects.

Docs: https://developer.todoist.com/api/v1/
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.external_audit import timed_call

log = logging.getLogger(__name__)

BASE = "https://api.todoist.com/api/v1"


class TodoistProjectFullError(Exception):
    """Project heeft Todoist's max-items-per-project limiet bereikt
    (error_tag=MAX_ITEMS_LIMIT_REACHED). Sync-callers vangen dit en
    stoppen retry-spam tot the user het project heeft opgeschoond."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str


@dataclass(frozen=True)
class Task:
    id: str
    content: str
    project_id: str | None
    is_completed: bool
    labels: list[str]
    due_date: str | None        # 'YYYY-MM-DD' of None
    due_datetime: str | None    # ISO-8601 of None
    created_at: str | None = None  # ISO-8601 — voor stale-task detectie in cleanup


class TodoistClient:
    def __init__(self, api_token: str, *, timeout: float = 15.0) -> None:
        if not api_token:
            raise ValueError("TODOIST_API_TOKEN missing")
        self._token = api_token
        self._timeout = timeout

    # --- projects ---------------------------------------------------------

    def list_projects(self) -> list[Project]:
        data = self._request("GET", "/projects")
        return [Project(id=str(p["id"]), name=p["name"]) for p in _items(data)]

    def create_project(self, name: str) -> Project:
        data = self._request("POST", "/projects", body={"name": name})
        return Project(id=str(data["id"]), name=data["name"])

    def find_or_create_project(self, name: str) -> Project:
        for p in self.list_projects():
            if p.name == name:
                return p
        return self.create_project(name)

    # --- tasks ------------------------------------------------------------

    def list_tasks(
        self, *, project_id: str | None = None,
        max_pages: int = 20,
    ) -> list[Task]:
        """Paginate door alle open tasks. Todoist unified API returnt
        max 50 per pagina + `next_cursor`. Zonder pagineren miste
        `list_tasks` alles voorbij item 50 (bug 13/7 waardoor Rosa een
        bestaande taak niet kon vinden). Cap op max_pages voorkomt
        runaway bij defecte cursor-loop."""
        out: list[Task] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, str] = {}
            if project_id:
                params["project_id"] = project_id
            if cursor:
                params["cursor"] = cursor
            path = "/tasks"
            if params:
                path += "?" + urllib.parse.urlencode(params)
            data = self._request("GET", path)
            out.extend(_to_task(d) for d in _items(data))
            if isinstance(data, dict):
                cursor = data.get("next_cursor")
            else:
                cursor = None
            if not cursor:
                break
        return out

    def create_task(
        self,
        *,
        content: str,
        project_id: str | None = None,
        labels: list[str] | None = None,
        due_string: str | None = None,
        due_datetime: str | None = None,
        description: str | None = None,
    ) -> Task:
        body: dict[str, Any] = {"content": content[:500]}
        if project_id:
            body["project_id"] = project_id
        if labels:
            body["labels"] = labels
        if due_datetime:
            body["due_datetime"] = due_datetime
        elif due_string:
            body["due_string"] = due_string
        if description:
            body["description"] = description[:16000]
        data = self._request("POST", "/tasks", body=body)
        return _to_task(data)

    def update_task(
        self, task_id: str, *,
        content: str | None = None,
        description: str | None = None,
        due_datetime: str | None = None,
    ) -> bool:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content[:500]
        if description is not None:
            body["description"] = description[:16000]
        if due_datetime is not None:
            body["due_datetime"] = due_datetime
        if not body:
            return True
        try:
            self._request("POST", f"/tasks/{task_id}", body=body)
            return True
        except urllib.error.HTTPError as e:
            log.warning("todoist update failed for %s: HTTP %s", task_id, e.code)
            return False

    def close_task(self, task_id: str) -> bool:
        """Mark task as completed."""
        try:
            self._request("POST", f"/tasks/{task_id}/close", expect_empty=True)
            return True
        except urllib.error.HTTPError as e:
            log.warning("todoist close failed for %s: HTTP %s", task_id, e.code)
            return False

    def reopen_task(self, task_id: str) -> bool:
        try:
            self._request("POST", f"/tasks/{task_id}/reopen", expect_empty=True)
            return True
        except urllib.error.HTTPError as e:
            log.warning("todoist reopen failed for %s: HTTP %s", task_id, e.code)
            return False

    # --- internals --------------------------------------------------------

    def _request(
        self, method: str, path: str, *,
        body: dict[str, Any] | None = None,
        expect_empty: bool = False,
    ) -> Any:
        url = f"{BASE}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "pa-agent/0.1",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with timed_call(
            service="todoist",
            endpoint=f"{method} {path.split('?')[0]}",
            bytes_out=len(data) if data else 0,
        ) as audit_ctx:
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                audit_ctx.set(status=resp.status, bytes_in=len(raw))
            except urllib.error.HTTPError as exc:
                # CLAUDE.md "Log egress, not content" — Todoist 4xx-bodies
                # echo'en het verzonden payload (task-titles met namen)
                # terug. Parse JSON, log alleen error_tag/error_code +
                # bytes_in van de body — geen content.
                err_body = ""
                err_tag = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")[:500]
                    parsed = json.loads(err_body)
                    err_tag = str(parsed.get("error_tag") or "")
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
                except Exception:
                    pass
                audit_ctx.set(status=exc.code, bytes_in=len(err_body))
                log.warning(
                    "todoist %s %s -> HTTP %s (tag=%s, body_bytes=%d)",
                    method, path.split('?')[0], exc.code,
                    err_tag or "?", len(err_body),
                )
                # Detecteer project-vol situatie zodat sync-callers de
                # 30+ items-per-tick retry-storm stoppen.
                if exc.code == 403 and err_tag == "MAX_ITEMS_LIMIT_REACHED":
                    raise TodoistProjectFullError(err_tag) from exc
                raise
        if expect_empty or not raw:
            return None
        return json.loads(raw)


def _items(data: Any) -> list[dict[str, Any]]:
    """De unified `/api/v1/` API geeft list-responses als `{results: [...]}`,
    de oude `/rest/v2/` gaf bare arrays. Beide aankunnen voor robustness."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _to_task(d: dict[str, Any]) -> Task:
    due = d.get("due") or {}
    return Task(
        id=str(d["id"]),
        content=d.get("content", ""),
        project_id=str(d["project_id"]) if d.get("project_id") else None,
        is_completed=bool(d.get("is_completed", False)),
        labels=list(d.get("labels") or []),
        due_date=due.get("date") if due else None,
        due_datetime=due.get("datetime") if due else None,
        created_at=d.get("created_at") or d.get("added_at"),
    )
