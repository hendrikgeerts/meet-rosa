"""Tests voor OKR-loader + tools."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from extensions.okrs.loader import (
    KeyResult, Objective, load_okrs, to_briefing_snapshot,
    update_kr_progress,
)
from extensions.okrs.tools import (
    okrs_check_handler, okrs_list_handler, okrs_update_progress_handler,
)


_SAMPLE_YAML = """\
period: "Q2 2026"
period_start: "2026-04-01"
period_end: "2026-06-30"
objectives:
  - id: dst-arr
    title: Verdubbel DST ARR
    company: DST
    why: Hoofdhefboom
    status: active
    key_results:
      - id: kr1
        text: "EUR 60K ARR"
        target: 60000
        unit: EUR
        current: 30000
      - id: kr2
        text: "10 nieuwe klanten"
        target: 10
        unit: count
        current: 5
  - id: hge-launch
    title: PA-agent v1
    company: HGE
    status: paused
    key_results:
      - id: kr1
        text: ISO audit
        target: 1
        unit: milestone
        current: 0
"""


@pytest.fixture
def okrs_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "okrs.yaml"
    p.write_text(_SAMPLE_YAML, encoding="utf-8")
    return p


def test_load_okrs_parses_objectives_and_krs(okrs_yaml: Path) -> None:
    period = load_okrs(okrs_yaml)
    assert period is not None
    assert period.period == "Q2 2026"
    assert len(period.objectives) == 2
    obj = period.find("dst-arr")
    assert obj is not None
    assert obj.company == "DST"
    assert len(obj.key_results) == 2
    assert obj.key_results[0].progress_pct == 50
    assert obj.avg_progress_pct == 50  # (50 + 50) / 2


def test_active_filters_status(okrs_yaml: Path) -> None:
    period = load_okrs(okrs_yaml)
    active = period.active()
    assert len(active) == 1
    assert active[0].id == "dst-arr"


def test_load_okrs_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_okrs(tmp_path / "nope.yaml") is None


def test_kr_progress_pct_caps_at_100(tmp_path: Path) -> None:
    kr = KeyResult(id="x", text="x", target=10, unit="n", current=999)
    assert kr.progress_pct == 100


def test_kr_progress_pct_zero_target() -> None:
    kr = KeyResult(id="x", text="x", target=0, unit="n", current=5)
    assert kr.progress_pct == 0


def test_to_briefing_snapshot_only_active(okrs_yaml: Path) -> None:
    snap = to_briefing_snapshot(load_okrs(okrs_yaml))
    assert len(snap) == 1
    assert snap[0]["id"] == "dst-arr"
    assert snap[0]["avg_progress_pct"] == 50
    assert len(snap[0]["key_results"]) == 2


def test_update_kr_progress(okrs_yaml: Path) -> None:
    ok = update_kr_progress(okrs_yaml,
                            objective_id="dst-arr", kr_id="kr2", current=8)
    assert ok is True
    period = load_okrs(okrs_yaml)
    obj = period.find("dst-arr")
    assert obj.key_results[1].current == 8
    assert obj.key_results[1].progress_pct == 80


def test_update_kr_progress_unknown_returns_false(okrs_yaml: Path) -> None:
    assert update_kr_progress(okrs_yaml, objective_id="nope", kr_id="kr1",
                               current=1) is False


def test_okrs_list_handler_filters_by_company(okrs_yaml: Path) -> None:
    out = okrs_list_handler(okrs_yaml, {"company": "HGE"})
    # HGE is paused → filter on company AFTER active-only filter, so empty.
    assert out["objectives"] == []
    out = okrs_list_handler(okrs_yaml, {"company": "DST"})
    assert len(out["objectives"]) == 1


def test_okrs_list_handler_missing_yaml(tmp_path: Path) -> None:
    out = okrs_list_handler(tmp_path / "missing.yaml", {})
    assert out["period"] is None
    assert "ontbreekt" in out["note"]


def test_okrs_update_progress_handler(okrs_yaml: Path) -> None:
    out = okrs_update_progress_handler(okrs_yaml, {
        "objective_id": "dst-arr", "kr_id": "kr1", "current": 45000,
    })
    assert out["ok"] is True
    assert out["progress_pct"] == 75


def test_okrs_check_handler_parses_claude_json(okrs_yaml: Path) -> None:
    fake_response = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = json.dumps([
        {"objective_id": "dst-arr", "score": 8,
         "rationale": "Direct ARR work", "recommend": "go"},
        {"overall": "go", "max_score": 8},
    ])
    fake_response.content = [fake_block]

    fake_gateway = MagicMock()
    fake_gateway.complete.return_value = fake_response

    out = okrs_check_handler(
        okrs_yaml, {"proposal": "Pitch enterprise klant X"},
        gateway=fake_gateway,
    )
    assert "scores" in out
    assert out["scores"][0]["recommend"] == "go"
    assert fake_gateway.complete.call_args.kwargs["task"] == "okrs_check"


def test_okrs_check_handler_handles_markdown_fence(okrs_yaml: Path) -> None:
    fake_response = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = (
        "```json\n[{\"objective_id\":\"dst-arr\",\"score\":3,"
        "\"rationale\":\"weak\",\"recommend\":\"discuss\"}]\n```"
    )
    fake_response.content = [fake_block]

    fake_gateway = MagicMock()
    fake_gateway.complete.return_value = fake_response

    out = okrs_check_handler(
        okrs_yaml, {"proposal": "Iets"}, gateway=fake_gateway,
    )
    assert out["scores"][0]["score"] == 3


def test_okrs_check_handler_no_active(tmp_path: Path) -> None:
    p = tmp_path / "okrs.yaml"
    p.write_text("period: Q1\nobjectives: []\n", encoding="utf-8")
    fake_gateway = MagicMock()
    out = okrs_check_handler(p, {"proposal": "x"}, gateway=fake_gateway)
    assert "note" in out
    fake_gateway.complete.assert_not_called()


def test_okrs_check_handler_empty_proposal(okrs_yaml: Path) -> None:
    fake_gateway = MagicMock()
    out = okrs_check_handler(okrs_yaml, {"proposal": "  "}, gateway=fake_gateway)
    assert "error" in out
    fake_gateway.complete.assert_not_called()
