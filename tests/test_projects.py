"""Tests voor project-tracker schema, aggregator en tools."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.comm_intel.schema import init_comm_schema
from extensions.decisions.schema import (
    init_decisions_schema, insert_decision,
)
from extensions.open_loops.schema import init_open_loops_schema
from extensions.projects.aggregator import project_status
from extensions.projects.schema import (
    delete_project, get_project, init_projects_schema, insert_project,
    list_projects, update_project,
)
from extensions.projects.tools import (
    project_create_handler, project_delete_handler, project_list_handler,
    project_status_handler, project_update_handler,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "proj.db"
    init_projects_schema(p)
    init_comm_schema(p)
    init_decisions_schema(p)
    init_open_loops_schema(p)
    return p


def test_insert_and_list_projects(db: Path) -> None:
    with sqlite3.connect(db) as c:
        pid = insert_project(c, slug="pa-v1", title="PA-agent v1",
                              company="HGE", keywords=["rosa", "pa-agent"])
    assert pid > 0
    with sqlite3.connect(db) as c:
        rows = list_projects(c)
    assert len(rows) == 1
    assert rows[0]["slug"] == "pa-v1"
    assert rows[0]["keywords"] == ["rosa", "pa-agent"]


def test_unique_slug_constraint(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_project(c, slug="x", title="X")
        with pytest.raises(sqlite3.IntegrityError):
            insert_project(c, slug="x", title="X dup")


def test_invalid_status_rejected(db: Path) -> None:
    with sqlite3.connect(db) as c:
        with pytest.raises(ValueError):
            insert_project(c, slug="x", title="X", status="weird")


def test_update_partial(db: Path) -> None:
    with sqlite3.connect(db) as c:
        pid = insert_project(c, slug="x", title="Old", company="DST")
        ok = update_project(c, project_id=pid, title="New", status="paused")
        assert ok is True
        proj = get_project(c, project_id=pid)
    assert proj["title"] == "New"
    assert proj["status"] == "paused"
    assert proj["company"] == "DST"  # unchanged


def test_update_clear_deadline(db: Path) -> None:
    with sqlite3.connect(db) as c:
        pid = insert_project(c, slug="x", title="X", deadline_at=1700000000)
        update_project(c, project_id=pid, clear_deadline=True)
        proj = get_project(c, project_id=pid)
    assert proj["deadline_at"] is None


def test_filter_by_status_and_company(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_project(c, slug="a", title="A", status="active", company="DST")
        insert_project(c, slug="b", title="B", status="paused", company="DST")
        insert_project(c, slug="c", title="C", status="active", company="HGE")
        active = list_projects(c, status="active")
        dst = list_projects(c, company="DST")
    assert {p["slug"] for p in active} == {"a", "c"}
    assert {p["slug"] for p in dst} == {"a", "b"}


def test_project_status_aggregates_linked(db: Path) -> None:
    with sqlite3.connect(db) as c:
        insert_project(c, slug="rosa", title="Rosa PA",
                        keywords=["rosa", "pa-agent"])
        # Decision met 'rosa' in body
        insert_decision(c, title="Stop met ElevenLabs",
                        body="Voor Rosa kiezen we lokale Ava-stem.")
        # Decision die niet matcht
        insert_decision(c, title="Verhuizing", body="Niets met PA")
        # comm_item met 'pa-agent' in subject
        c.execute(
            "INSERT INTO comm_items (source, account, external_id, direction, "
            "subject, occurred_at, body_full) VALUES (?,?,?,?,?,?,?)",
            ("gmail", "gmail", "ext1", "in", "pa-agent v1 update?",
             int(_time.time()), "stand van zaken pa-agent"),
        )
        # open_loop matchend op title
        c.execute(
            "INSERT INTO open_loops (source, kind, who, title, body_excerpt) "
            "VALUES (?,?,?,?,?)",
            ("comm", "incoming_question", "klant",
             "Vraag over Rosa workflow", "wanneer release rosa"),
        )

    result = project_status(db, slug="rosa", days_back=30)
    assert result["project"]["slug"] == "rosa"
    decision_titles = [d["title"] for d in result["recent_decisions"]]
    assert "Stop met ElevenLabs" in decision_titles
    assert "Verhuizing" not in decision_titles
    assert len(result["recent_comm"]) == 1
    assert len(result["open_loops"]) == 1


def test_project_status_unknown_slug(db: Path) -> None:
    result = project_status(db, slug="nope")
    assert "error" in result


def test_project_create_handler_derives_slug(db: Path) -> None:
    out = project_create_handler(db, {"title": "DST Templates Revamp"})
    assert out["ok"] is True
    assert out["project"]["slug"] == "dst-templates-revamp"


def test_project_create_handler_duplicate_slug(db: Path) -> None:
    project_create_handler(db, {"title": "X", "slug": "x"})
    out = project_create_handler(db, {"title": "X again", "slug": "x"})
    assert "error" in out


def test_project_create_parses_deadline(db: Path) -> None:
    out = project_create_handler(db, {
        "title": "Q2 push", "deadline": "2026-06-30",
    })
    assert out["ok"]
    assert out["project"]["deadline"] == "2026-06-30"


def test_project_update_handler_changes_status(db: Path) -> None:
    project_create_handler(db, {"title": "X", "slug": "x"})
    out = project_update_handler(db, {"slug": "x", "status": "done"})
    assert out["ok"]
    assert out["project"]["status"] == "done"


def test_project_update_handler_clears_deadline(db: Path) -> None:
    project_create_handler(db, {"title": "X", "slug": "x",
                                  "deadline": "2026-06-30"})
    out = project_update_handler(db, {"slug": "x", "deadline": ""})
    assert out["ok"]
    assert out["project"].get("deadline") is None


def test_project_update_handler_no_args_is_noop(db: Path) -> None:
    project_create_handler(db, {"title": "X", "slug": "x"})
    out = project_update_handler(db, {"slug": "x"})
    assert out.get("note") == "no fields changed"


def test_project_status_handler_via_tools(db: Path) -> None:
    project_create_handler(db, {"title": "Rosa", "slug": "rosa",
                                  "keywords": ["rosa"]})
    out = project_status_handler(db, {"slug": "rosa"})
    assert out["project"]["slug"] == "rosa"
    assert out["recent_comm"] == []


def test_project_delete_handler(db: Path) -> None:
    project_create_handler(db, {"title": "X", "slug": "x"})
    out = project_delete_handler(db, {"slug": "x"})
    assert out["ok"] is True
    out = project_list_handler(db, {})
    assert out == []


def test_project_list_handler_format(db: Path) -> None:
    project_create_handler(db, {"title": "A", "slug": "a", "company": "DST"})
    out = project_list_handler(db, {"company": "DST"})
    assert len(out) == 1
    assert out[0]["slug"] == "a"
    assert "created" in out[0]
