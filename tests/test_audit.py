"""Tests for core.audit — JSONL writer with daily rotation, content-free."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.audit import AuditLogger, prune_old


def test_writes_one_jsonl_record_per_call(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path)
    log.log("claude_call", task="morning_briefing", label="internal", model="claude-sonnet-4-6")
    log.log("claude_call", task="tool_use_turn", label="public", model="claude-sonnet-4-6")

    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    file = tmp_path / f"egress-{today}.jsonl"
    assert file.exists()

    lines = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["event"] == "claude_call"
    assert lines[0]["task"] == "morning_briefing"
    assert lines[1]["task"] == "tool_use_turn"


def test_record_has_iso_timestamp(tmp_path: Path) -> None:
    log = AuditLogger(tmp_path)
    log.log("claude_call", task="x", model="y")
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    rec = json.loads((tmp_path / f"egress-{today}.jsonl").read_text(encoding="utf-8").splitlines()[0])
    # parseable ISO with offset
    parsed = datetime.fromisoformat(rec["ts"])
    assert parsed.tzinfo is not None


def test_creates_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "audit"
    AuditLogger(target).log("e", x=1)
    assert target.is_dir()


def _make(file: Path, content: str = "x\n") -> None:
    file.write_text(content, encoding="utf-8")


def test_prune_old_deletes_files_older_than_cutoff(tmp_path: Path) -> None:
    today = date.today()
    # Maak 3 audit-files: 100 dagen oud (verwijderen), 30 dagen oud
    # (behouden bij retention=90), vandaag (behouden).
    old = today - timedelta(days=100)
    mid = today - timedelta(days=30)
    _make(tmp_path / f"egress-{old.isoformat()}.jsonl")
    _make(tmp_path / f"payloads-{old.isoformat()}.jsonl")
    _make(tmp_path / f"egress-{mid.isoformat()}.jsonl")
    _make(tmp_path / f"egress-{today.isoformat()}.jsonl")

    removed = prune_old(tmp_path, max_age_days=90)
    assert removed == 2  # beide 100-dagen-oude files

    remaining = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert f"egress-{mid.isoformat()}.jsonl" in remaining
    assert f"egress-{today.isoformat()}.jsonl" in remaining
    assert f"egress-{old.isoformat()}.jsonl" not in remaining


def test_prune_skips_files_with_unrecognised_names(tmp_path: Path) -> None:
    """Niet-audit-bestanden (random files in audit_dir) blijven met rust."""
    _make(tmp_path / "random-2020-01-01.txt")           # niet .jsonl
    _make(tmp_path / "wrongprefix-2020-01-01.jsonl")    # andere prefix
    _make(tmp_path / "egress-not-a-date.jsonl")         # geen datum-format
    removed = prune_old(tmp_path, max_age_days=1)
    assert removed == 0
    assert len(list(tmp_path.glob("*"))) == 3


def test_prune_with_zero_or_negative_days_is_noop(tmp_path: Path) -> None:
    today = date.today()
    _make(tmp_path / f"egress-{(today - timedelta(days=1000)).isoformat()}.jsonl")
    assert prune_old(tmp_path, max_age_days=0) == 0
    assert prune_old(tmp_path, max_age_days=-1) == 0


# --- MED-4: split retention by prefix ----------------------------------

def test_prune_with_prefix_only_touches_that_prefix(tmp_path: Path) -> None:
    """prefix='egress' deletes only egress-*.jsonl; payloads-* survive."""
    old = date.today() - timedelta(days=200)
    _make(tmp_path / f"egress-{old.isoformat()}.jsonl")
    _make(tmp_path / f"payloads-{old.isoformat()}.jsonl")
    removed = prune_old(tmp_path, max_age_days=90, prefix="egress")
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert f"egress-{old.isoformat()}.jsonl" not in remaining
    assert f"payloads-{old.isoformat()}.jsonl" in remaining


def test_prune_payloads_with_shorter_window(tmp_path: Path) -> None:
    """The reason MED-4 exists: payloads can be pruned with a tighter
    window than the egress audit-trail."""
    today = date.today()
    d20 = today - timedelta(days=20)
    d100 = today - timedelta(days=100)
    _make(tmp_path / f"payloads-{d20.isoformat()}.jsonl")
    _make(tmp_path / f"payloads-{d100.isoformat()}.jsonl")
    _make(tmp_path / f"egress-{d20.isoformat()}.jsonl")
    _make(tmp_path / f"egress-{d100.isoformat()}.jsonl")

    # Egress kept 90 days; payloads kept 14.
    removed_egress = prune_old(tmp_path, max_age_days=90, prefix="egress")
    removed_payloads = prune_old(tmp_path, max_age_days=14, prefix="payloads")
    assert removed_egress == 1  # d100
    assert removed_payloads == 2  # d20 and d100 both > 14 days
    remaining = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    # Only the d20 egress survives.
    assert remaining == [f"egress-{d20.isoformat()}.jsonl"]


def test_prune_no_prefix_keeps_old_dual_behaviour(tmp_path: Path) -> None:
    """No prefix → original behaviour (both egress + payloads)."""
    old = date.today() - timedelta(days=200)
    _make(tmp_path / f"egress-{old.isoformat()}.jsonl")
    _make(tmp_path / f"payloads-{old.isoformat()}.jsonl")
    removed = prune_old(tmp_path, max_age_days=90)
    assert removed == 2


def test_no_content_keys_logged(tmp_path: Path) -> None:
    """Smoke check: what callers pass is what we log; we don't sneak in 'content'."""
    log = AuditLogger(tmp_path)
    log.log("claude_call", task="t", label="internal", model="m", input_tokens=10, output_tokens=20)
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    rec = json.loads((tmp_path / f"egress-{today}.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "content" not in rec
    assert "messages" not in rec
    assert "system" not in rec
    assert rec["input_tokens"] == 10
    assert rec["output_tokens"] == 20
