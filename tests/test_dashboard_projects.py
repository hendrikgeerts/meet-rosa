"""Smoke-tests voor de project-CRUD pages in het dashboard."""
from __future__ import annotations

from pathlib import Path

import pytest

from extensions.comm_intel.schema import init_comm_schema
from extensions.decisions.schema import init_decisions_schema
from extensions.open_loops.schema import init_open_loops_schema
from extensions.projects.schema import init_projects_schema


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "projects.db"
    init_projects_schema(p)
    init_comm_schema(p)
    init_decisions_schema(p)
    init_open_loops_schema(p)
    return p


@pytest.fixture
def client(db: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    return TestClient(create_app(audit, db_path=db), base_url="http://127.0.0.1:8080")


def test_index_empty_state(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/projects")
    assert r.status_code == 200
    assert "No projects yet" in r.text


def test_create_project_via_form(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/projects/new", data={
        "title": "Rosa v1",
        "slug": "",  # auto from title
        "company": "HGE",
        "owner": "Hendrik",
        "status": "active",
        "deadline": "2026-06-30",
        "keywords": "rosa, pa-agent",
        "description": "Eerste release",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/projects?message=created" in r.headers["location"]

    r = client.get("/projects")
    assert "Rosa v1" in r.text
    assert "rosa-v1" in r.text  # slug derived from title


def test_create_with_explicit_slug(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/projects/new", data={
        "title": "DST Revamp", "slug": "dst-rev",
        "status": "active", "keywords": "",
    }, follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/projects/dst-rev")
    assert r.status_code == 200
    assert "DST Revamp" in r.text


def test_create_duplicate_slug(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/projects/new", data={"title": "X", "slug": "x", "status": "active",
                                          "keywords": ""})
    r = client.post("/projects/new", data={"title": "X2", "slug": "x", "status": "active",
                                              "keywords": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "slug+already+exists" in r.headers["location"]


def test_invalid_status_rejected_in_create(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/projects/new", data={"title": "X", "slug": "x", "status": "weird",
                                              "keywords": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "invalid+status" in r.headers["location"]


def test_edit_updates_fields(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/projects/new", data={"title": "Old", "slug": "x", "status": "active",
                                          "keywords": ""})
    r = client.post("/projects/x/edit", data={
        "title": "New Title", "company": "DST", "owner": "Anouk",
        "status": "paused", "deadline": "", "keywords": "alpha, beta",
        "description": "updated",
    }, follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/projects/x")
    assert "New Title" in r.text
    assert "paused" in r.text
    assert "Anouk" in r.text
    assert "alpha" in r.text


def test_edit_unknown_slug_redirects(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/projects/nope/edit", data={"title": "x", "status": "active",
                                                    "keywords": ""},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/projects"


def test_delete_removes_project(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/projects/new", data={"title": "X", "slug": "x", "status": "active",
                                          "keywords": ""})
    r = client.post("/projects/x/delete", follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/projects")
    assert "No projects yet" in r.text


def test_detail_unknown_slug_404(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/projects/does-not-exist")
    assert r.status_code == 404


def test_unsafe_slug_rejected(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/projects/..%2F..%2Fetc%2Fpasswd")
    # FastAPI will url-decode the slug; the safety check rejects bad chars.
    # Either 400 from our check or 404 from missing project — both acceptable.
    assert r.status_code in (400, 404)


def test_filter_by_status(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/projects/new", data={"title": "A", "slug": "a", "status": "active",
                                          "keywords": ""})
    client.post("/projects/new", data={"title": "B", "slug": "b", "status": "done",
                                          "keywords": ""})
    r = client.get("/projects?status=active")
    assert "A" in r.text
    # The 'B' row should not be present in active filter
    assert ">B<" not in r.text or "/projects/b" not in r.text
