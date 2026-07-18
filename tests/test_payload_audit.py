"""Tests voor PayloadAuditLogger + gateway shadow-log integratie."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.audit import AuditLogger, PayloadAuditLogger
from privacy.classifier import Classifier
from privacy.gateway import Gateway
from privacy.redactor import Redactor


@dataclass
class _Block:
    type: str = "text"
    text: str = ""


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _Resp:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


@dataclass
class _FakeClaude:
    model: str = "claude-fake"
    last_call: dict[str, Any] | None = None
    response_text: str = "ok"
    def reply(self, **kwargs: Any) -> _Resp:
        self.last_call = kwargs
        return _Resp(content=[_Block(text=self.response_text)])


def _today_path(d: Path, prefix: str) -> Path:
    return d / f"{prefix}-{datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d')}.jsonl"


def _build_gateway(tmp_path: Path, *, with_payload: bool = True) -> tuple[Gateway, _FakeClaude]:
    audit = AuditLogger(tmp_path)
    payload = PayloadAuditLogger(tmp_path) if with_payload else None
    classifier = Classifier(default_label="internal")
    redactor = Redactor(vip_people=("Piet",), vip_orgs=("Heineken",))
    gw = Gateway(api_key="x", model="claude-fake", audit=audit,
                 classifier=classifier, redactor=redactor, payload_audit=payload)
    fake = _FakeClaude(model="claude-fake")
    gw._claude = fake  # type: ignore[assignment]
    return gw, fake


# --- direct PayloadAuditLogger tests --------------------------------------

def test_payload_audit_writes_record(tmp_path: Path) -> None:
    p = PayloadAuditLogger(tmp_path)
    p.log(task="t", label="internal", model="m", backend="claude",
          system_redacted="sys", messages_redacted=[{"role": "user", "content": "hi"}],
          tools_offered=[], response_text="ok",
          redactions_applied=2, stop_reason="end_turn",
          input_tokens=10, output_tokens=20, classifier_reason="default")
    f = _today_path(tmp_path, "payloads")
    assert f.exists()
    rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    assert rec["task"] == "t"
    assert rec["system_redacted"] == "sys"
    assert rec["response_text"] == "ok"
    assert rec["redactions_applied"] == 2


def test_payload_audit_locks_perms(tmp_path: Path) -> None:
    p = PayloadAuditLogger(tmp_path)
    p.log(task="t", label="x", model="m", backend="claude",
          system_redacted="", messages_redacted=[], tools_offered=[],
          response_text="", redactions_applied=0, stop_reason=None,
          input_tokens=None, output_tokens=None, classifier_reason=None)
    f = _today_path(tmp_path, "payloads")
    assert oct(f.stat().st_mode)[-3:] == "600"


# --- gateway integratie ---------------------------------------------------

def test_gateway_with_payload_audit_writes_redacted(tmp_path: Path) -> None:
    gw, fake = _build_gateway(tmp_path)
    gw.complete(task="briefing", system="be brief",
                messages=[{"role": "user", "content": "Stuur Piet bij Heineken een bericht"}])

    rec = json.loads(_today_path(tmp_path, "payloads").read_text(encoding="utf-8").splitlines()[0])
    # Redacted version naar Claude → moet placeholders bevatten, geen Piet/Heineken.
    msgs_str = json.dumps(rec["messages_redacted"], ensure_ascii=False)
    assert "Piet" not in msgs_str
    assert "Heineken" not in msgs_str
    assert "[PERSON_001]" in msgs_str
    assert "[ORG_001]" in msgs_str
    assert rec["redactions_applied"] >= 2
    assert rec["backend"] == "claude"


def test_gateway_without_payload_audit_writes_nothing(tmp_path: Path) -> None:
    gw, _ = _build_gateway(tmp_path, with_payload=False)
    gw.complete(task="t", system="s", messages=[{"role": "user", "content": "Piet"}])
    assert not _today_path(tmp_path, "payloads").exists()


def test_gateway_payload_log_does_not_contain_mapping(tmp_path: Path) -> None:
    """De cardinale regel: NOOIT mapping (placeholder→origineel) loggen."""
    gw, _ = _build_gateway(tmp_path)
    gw.complete(task="t", system="x",
                messages=[{"role": "user", "content": "Piet werkt bij Heineken"}])
    raw = _today_path(tmp_path, "payloads").read_text(encoding="utf-8")
    rec = json.loads(raw.splitlines()[0])
    # Wat Claude zag mag verwijzingen bevatten naar [PERSON_001] / [ORG_001]
    # — maar nergens een mapping-veld dat ze terugmapt naar 'Piet' / 'Heineken'.
    assert "mapping" not in rec
    # En in de top-level JSON-tekst staat geen 'mapping' string.
    assert '"mapping"' not in raw
