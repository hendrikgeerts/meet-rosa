"""Smoke-tests voor /vendors en /receipt-runs dashboard pagina's."""
from __future__ import annotations

import sqlite3
import time as _time
from pathlib import Path

import pytest

from extensions.receipt_collector.schema import (
    init_receipt_collector_schema, insert_run, insert_run_item,
    update_run_counts, update_run_item, upsert_vendor_strategy,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "rc.db"
    init_receipt_collector_schema(p)
    return p


@pytest.fixture
def client(db: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from web.app import create_app
    audit = tmp_path / "audit"
    audit.mkdir()
    return TestClient(create_app(audit, db_path=db), base_url="http://127.0.0.1:8080")


# --- /vendors ------------------------------------------------------------

def test_vendors_index_empty(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/vendors")
    assert r.status_code == 200
    assert "Nog geen vendor strategies" in r.text


def test_create_vendor_email(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/vendors/new", data={
        "name": "Amazon",
        "source_kind": "email",
        "aliases": "aws, amazon web services",
        "email_query_hint": "from:billing@aws.amazon.com",
        "portal_url": "",
        "portal_notes": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/vendors?message=saved" in r.headers["location"]

    with sqlite3.connect(db) as c:
        row = c.execute("SELECT name, source_kind FROM vendor_strategies").fetchone()
    assert row == ("Amazon", "email")


def test_create_vendor_invalid_kind(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/vendors/new", data={
        "name": "X", "source_kind": "ufo",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "invalid+source_kind" in r.headers["location"]


def test_edit_vendor(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        upsert_vendor_strategy(c, name="Old", source_kind="email")
        vid = c.execute("SELECT id FROM vendor_strategies").fetchone()[0]

    r = client.post(f"/vendors/{vid}/edit", data={
        "name": "Old",
        "source_kind": "portal",
        "portal_url": "https://x.test",
        "portal_notes": "Login → Billing",
    }, follow_redirects=False)
    assert r.status_code == 303

    with sqlite3.connect(db) as c:
        row = c.execute("SELECT source_kind, portal_url FROM vendor_strategies WHERE id=?",
                          (vid,)).fetchone()
    assert row[0] == "portal"
    assert row[1] == "https://x.test"


def test_edit_vendor_rename(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        upsert_vendor_strategy(c, name="OldName", source_kind="email")
        vid = c.execute("SELECT id FROM vendor_strategies").fetchone()[0]

    r = client.post(f"/vendors/{vid}/edit", data={
        "name": "NewName",
        "source_kind": "email",
    }, follow_redirects=False)
    assert r.status_code == 303

    with sqlite3.connect(db) as c:
        row = c.execute("SELECT name FROM vendor_strategies WHERE id=?",
                          (vid,)).fetchone()
    assert row[0] == "NewName"


def test_delete_vendor(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        upsert_vendor_strategy(c, name="X", source_kind="email")
        vid = c.execute("SELECT id FROM vendor_strategies").fetchone()[0]

    r = client.post(f"/vendors/{vid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT * FROM vendor_strategies").fetchall()
    assert rows == []


def test_vendors_form_prefill_via_query(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/vendors/new?prefill_name=Amazon&prefill_alias=50140+-+Amazon+%28cc%29&prefill_amount=127.50&prefill_date=2026-04-05")
    assert r.status_code == 200
    assert "Amazon" in r.text
    assert "50140 - Amazon (cc)" in r.text
    assert "127.50" in r.text


def test_import_unknowns_no_runs(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/vendors/import-unknowns")
    assert r.status_code == 404


def test_import_unknowns_lists_grouped(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        rid = insert_run(c, excel_path="/x.xlsx", output_dir="/out",
                          period_label="Q1-2026",
                          date_window_start=0, date_window_end=0,
                          transaction_count=4)
        for vendor, amt in [("50140 - Amazon (cc)", -10000),
                              ("50140 - Amazon (cc)", -20000),
                              ("50088 - Tidio LLC (cc)", -6120),
                              ("Mol*ADL", -3000)]:
            iid = insert_run_item(c, run_id=rid, row_idx=1,
                                    transaction_date=int(_time.time()),
                                    vendor_raw=vendor, amount_cents=amt)
            update_run_item(c, iid, status="unknown_vendor")
    r = client.get("/vendors/import-unknowns")
    assert r.status_code == 200
    assert "Amazon" in r.text
    assert "Tidio" in r.text
    # Amazon staat 2x → telling 2; Mol*ADL telt 1
    assert ">2<" in r.text or "2</td>" in r.text


# --- /receipt-runs --------------------------------------------------------

def test_runs_index_empty(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/receipt-runs")
    assert r.status_code == 200
    assert "Nog geen runs gedaan" in r.text


def test_runs_index_lists(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        rid = insert_run(c, excel_path="/x.xlsx", output_dir="/out",
                          period_label="Q2-2026",
                          date_window_start=0, date_window_end=0,
                          transaction_count=10)
        update_run_counts(c, rid, matched=5, needs_portal=2, unknown=3,
                           status="needs_input", completed=False)
    r = client.get("/receipt-runs")
    assert "Q2-2026" in r.text
    assert "needs_input" in r.text


def test_run_detail_unknown_run(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/receipt-runs/999")
    assert r.status_code == 404


def test_run_detail_with_items_and_filter(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        rid = insert_run(c, excel_path="/x.xlsx", output_dir="/out",
                          period_label="Q1-2026",
                          date_window_start=0, date_window_end=0,
                          transaction_count=2)
        i1 = insert_run_item(c, run_id=rid, row_idx=2,
                               transaction_date=int(_time.time()),
                               vendor_raw="Amazon", amount_cents=-12750)
        update_run_item(c, i1, status="matched", matched_via="gmail",
                         match_score=0.78, attachment_path="aws.pdf")
        i2 = insert_run_item(c, run_id=rid, row_idx=3,
                               transaction_date=int(_time.time()),
                               vendor_raw="Mol*ADL", amount_cents=-3000)
        update_run_item(c, i2, status="unknown_vendor")

    r = client.get(f"/receipt-runs/{rid}")
    assert r.status_code == 200
    assert "Amazon" in r.text
    assert "Mol*ADL" in r.text

    # Filter on matched only
    r2 = client.get(f"/receipt-runs/{rid}?status=matched")
    assert "Amazon" in r2.text
    assert "Mol*ADL" not in r2.text


def test_run_attachment_path_safety(client, db: Path) -> None:  # type: ignore[no-untyped-def]
    with sqlite3.connect(db) as c:
        rid = insert_run(c, excel_path="/x.xlsx", output_dir="/tmp/out",
                          period_label="x", date_window_start=0,
                          date_window_end=0, transaction_count=0)
    r = client.get(f"/receipt-runs/{rid}/attachment/..%2Fetc%2Fpasswd")
    # Either 400 (our path-safety check) or 404 (Starlette rejects the path)
    assert r.status_code in (400, 404)


def test_run_attachment_serves_file(
    client, db: Path, tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    pdf = out_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake\n")
    with sqlite3.connect(db) as c:
        rid = insert_run(c, excel_path="/x.xlsx", output_dir=str(out_dir),
                          period_label="x", date_window_start=0,
                          date_window_end=0, transaction_count=0)
    r = client.get(f"/receipt-runs/{rid}/attachment/test.pdf")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4\n%fake\n"


def test_run_attachment_missing_file_404(client, db: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with sqlite3.connect(db) as c:
        rid = insert_run(c, excel_path="/x.xlsx", output_dir=str(out_dir),
                          period_label="x", date_window_start=0,
                          date_window_end=0, transaction_count=0)
    r = client.get(f"/receipt-runs/{rid}/attachment/nope.pdf")
    assert r.status_code == 404
