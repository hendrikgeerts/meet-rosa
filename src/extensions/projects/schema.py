"""projects tabel — actieve initiatives als eerste-klas entiteiten.

Status verandert wekelijks (paused → active, deadlines schuiven), dus
waarom DB en niet yaml: SQL maakt joins met comm_items / decisions /
open_loops triviaal en het dashboard kan CRUD doen zonder yaml-roundtrip.

`keywords` is een JSON-array van trefwoorden die de aggregator gebruikt
om recent comm-verkeer / beslissingen aan een project te koppelen
(LIKE-scan, geen embedding nodig). the user kiest zelf: bv. project
'PA-agent v1' krijgt keywords ['pa-agent', 'rosa', 'PA agent'].
"""
from __future__ import annotations

import json
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    company TEXT,
    owner TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','paused','done','abandoned')),
    keywords TEXT,                  -- JSON array of strings
    deadline_at INTEGER,            -- unix ts; NULL = no hard deadline
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_company ON projects(company);
"""


VALID_STATUS = ("active", "paused", "done", "abandoned")


def init_projects_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def insert_project(
    conn: sqlite3.Connection, *,
    slug: str, title: str,
    description: str | None = None,
    company: str | None = None,
    owner: str | None = None,
    status: str = "active",
    keywords: list[str] | None = None,
    deadline_at: int | None = None,
) -> int:
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status!r}")
    cur = conn.execute(
        "INSERT INTO projects (slug, title, description, company, owner, "
        "status, keywords, deadline_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (slug, title, description, company, owner, status,
         json.dumps(keywords or [], ensure_ascii=False), deadline_at),
    )
    return cur.lastrowid or 0


def update_project(
    conn: sqlite3.Connection, *,
    project_id: int,
    title: str | None = None,
    description: str | None = None,
    company: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    keywords: list[str] | None = None,
    deadline_at: int | None = None,
    clear_deadline: bool = False,
) -> bool:
    """Partial update. Pass None to leave a field untouched. Use
    clear_deadline=True om expliciet `deadline_at = NULL` te zetten."""
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if description is not None:
        sets.append("description = ?"); params.append(description)
    if company is not None:
        sets.append("company = ?"); params.append(company)
    if owner is not None:
        sets.append("owner = ?"); params.append(owner)
    if status is not None:
        if status not in VALID_STATUS:
            raise ValueError(f"invalid status: {status!r}")
        sets.append("status = ?"); params.append(status)
    if keywords is not None:
        sets.append("keywords = ?")
        params.append(json.dumps(keywords, ensure_ascii=False))
    if clear_deadline:
        sets.append("deadline_at = NULL")
    elif deadline_at is not None:
        sets.append("deadline_at = ?"); params.append(deadline_at)

    if not sets:
        return False
    sets.append("updated_at = ?"); params.append(int(_time.time()))
    params.append(project_id)
    cur = conn.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params,
    )
    return cur.rowcount > 0


def delete_project(conn: sqlite3.Connection, project_id: int) -> bool:
    cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return cur.rowcount > 0


def list_projects(
    conn: sqlite3.Connection, *,
    status: str | None = None,
    company: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM projects WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"; params.append(status)
    if company:
        sql += " AND company = ?"; params.append(company)
    sql += " ORDER BY status='active' DESC, deadline_at IS NULL, deadline_at ASC, title ASC"
    conn.row_factory = sqlite3.Row
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def get_project(
    conn: sqlite3.Connection, *, slug: str | None = None,
    project_id: int | None = None,
) -> dict[str, Any] | None:
    if not slug and not project_id:
        return None
    if project_id:
        sql = "SELECT * FROM projects WHERE id = ?"
        params: tuple[Any, ...] = (project_id,)
    else:
        sql = "SELECT * FROM projects WHERE slug = ?"
        params = (slug,)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    return _row_to_dict(row) if row else None


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    try:
        keywords = json.loads(r["keywords"] or "[]")
    except (ValueError, TypeError):
        keywords = []
    return {
        "id": r["id"],
        "slug": r["slug"],
        "title": r["title"],
        "description": r["description"],
        "company": r["company"],
        "owner": r["owner"],
        "status": r["status"],
        "keywords": keywords,
        "deadline_at": r["deadline_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
