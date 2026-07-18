"""Egress audit logger — JSON-lines per day, content-free.

Every external API call (today: Claude; later: local model dispatch decisions,
web search) writes a single line here. We never log message bodies — only the
shape and intent: timestamp, task label, sensitivity label, model, byte/token
counts, stop reason, redaction stats.

The replay-ability requirement from PRIVACY_LAYER §6 is: from these logs alone
we should be able to answer "what kind of data went out, when, to whom" without
the data itself being persisted.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from core.perms import open_secure, secure_dir

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

# audit-bestanden hebben format `<prefix>-YYYY-MM-DD.jsonl`
# admin = config-mutaties + tool-calls die runtime-state veranderen
# (set_timezone, uptime silence, etc) — apart van egress/payloads zodat
# auditors een directe trail hebben voor A.12.4.3 (administrator logs).
_AUDIT_FILE_RE = re.compile(r"^(egress|payloads|admin)-(\d{4})-(\d{2})-(\d{2})\.jsonl$")


class AuditLogger:
    """Thread-safe append-only JSONL writer with daily rotation."""

    def __init__(self, audit_dir: Path) -> None:
        self._dir = secure_dir(audit_dir)
        self._lock = Lock()

    @property
    def directory(self) -> Path:
        return self._dir

    def log(self, event: str, **fields: Any) -> None:
        now = datetime.now(TZ)
        rec = {"ts": now.isoformat(), "event": event, **fields}
        path = self._dir / f"egress-{now.strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock, open_secure(path, "a") as f:
            f.write(line + "\n")


class AdminActionLogger:
    """Append-only log van runtime-state-mutaties: config-wijzigingen,
    tool-calls die het systeem-gedrag veranderen (set_timezone,
    uptime_silence_target, config_wish_set_status, set_reminder met
    long-term impact). Apart van egress/payloads zodat een formele
    auditor één file kan grijpen voor A.12.4.3 (administrator logs)
    + A.18.1.3 (Protection of records).

    Format: `admin-YYYY-MM-DD.jsonl` met velden:
      ts        ISO timestamp (NL voor forensische consistentie)
      action    bv. 'set_timezone' | 'uptime_silence' | ...
      actor     wie/wat triggerde (iMessage-handle of 'system')
      from      vorige waarde (string-snapshot, optioneel)
      to        nieuwe waarde
      reason    optionele toelichting
      extra     dict met action-specifieke velden

    Retention default 365 dagen (langer dan egress=90, payloads=14) —
    admin-acties hebben langere relevantie voor incident-forensics
    en jaarlijkse ISO-audits.
    """

    def __init__(self, audit_dir: Path) -> None:
        self._dir = secure_dir(audit_dir)
        self._lock = Lock()

    def log(
        self, *, action: str, actor: str,
        from_value: Any = None, to_value: Any = None,
        reason: str | None = None, **extra: Any,
    ) -> None:
        now = datetime.now(TZ)
        rec: dict[str, Any] = {
            "ts": now.isoformat(),
            "action": action,
            "actor": actor,
        }
        if from_value is not None:
            rec["from"] = from_value
        if to_value is not None:
            rec["to"] = to_value
        if reason:
            rec["reason"] = reason
        if extra:
            rec["extra"] = extra
        path = self._dir / f"admin-{now.strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock, open_secure(path, "a") as f:
            f.write(line + "\n")


# Module-level singleton zodat tools.py + andere modules zonder context
# `log_admin_action(...)` kunnen aanroepen. Wordt gebound in main.py.
_admin: AdminActionLogger | None = None


def bind_admin_logger(logger: AdminActionLogger) -> None:
    global _admin
    _admin = logger


def log_admin_action(
    *, action: str, actor: str,
    from_value: Any = None, to_value: Any = None,
    reason: str | None = None, **extra: Any,
) -> None:
    """Module-level convenience voor admin-action logging.
    No-op als bind_admin_logger nog niet is geroepen (tests/scripts)."""
    if _admin is None:
        return
    try:
        _admin.log(
            action=action, actor=actor,
            from_value=from_value, to_value=to_value,
            reason=reason, **extra,
        )
    except Exception:
        log.exception("admin-action log failed: action=%s", action)


class PayloadAuditLogger:
    """Optional shadow-log van wat Claude écht heeft gezien (redacted system
    + messages + response). Bedoeld voor menselijke audit via het lokale
    dashboard: the user kan hier nakijken of er per ongeluk PII door de
    redactor is geglipt. **Mapping wordt NOOIT gelogd** — anders zou je
    een nieuw lekrisico creëren (placeholders → echte namen op disk).

    File-mode 0600 op nieuwe bestanden. Daily rotation (`payloads-YYYY-MM-DD.jsonl`).
    """

    def __init__(self, audit_dir: Path) -> None:
        self._dir = secure_dir(audit_dir)
        self._lock = Lock()

    def log(
        self,
        *,
        task: str,
        label: str,
        model: str,
        backend: str,                       # 'claude' or 'local'
        system_redacted: str,
        messages_redacted: list[Any],
        tools_offered: list[str],
        response_text: str,
        redactions_applied: int,
        stop_reason: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        classifier_reason: str | None,
    ) -> None:
        now = datetime.now(TZ)
        rec = {
            "ts": now.isoformat(),
            "task": task,
            "label": label,
            "backend": backend,
            "model": model,
            "classifier_reason": classifier_reason,
            "redactions_applied": redactions_applied,
            "stop_reason": stop_reason,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tools_offered": tools_offered,
            "system_redacted": system_redacted,
            "messages_redacted": messages_redacted,
            "response_text": response_text,
        }
        path = self._dir / f"payloads-{now.strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock, open_secure(path, "a") as f:
            f.write(line + "\n")


def prune_old(
    audit_dir: Path, *,
    max_age_days: int,
    prefix: str | None = None,
) -> int:
    """Verwijder audit-bestanden ouder dan `max_age_days`. Returns aantal
    verwijderde files. Gebaseerd op de datum in de bestandsnaam (niet mtime)
    omdat dat exacter is — files zijn append-only voor de hele dag.

    `prefix` (optional): "egress" of "payloads" om de prune te scopen tot
    één type bestand. None = beide types (oud gedrag). SECURITY_REVIEW_2
    MED-4: egress-metadata blijft 90 dagen, shadow-payloads (1.6 MB/dag,
    geredacteerde bodies) kunnen veiliger op 14 dagen — twee aparte
    retention-vensters."""
    if max_age_days <= 0:
        return 0
    today = date.today()
    cutoff = today - timedelta(days=max_age_days)
    removed = 0
    for f in audit_dir.glob("*.jsonl"):
        m = _AUDIT_FILE_RE.match(f.name)
        if not m:
            continue
        if prefix is not None and m.group(1) != prefix:
            continue
        try:
            file_date = date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                f.unlink()
                removed += 1
                log.info("audit: pruned %s (date=%s, cutoff=%s)",
                         f.name, file_date, cutoff)
            except OSError:
                log.exception("audit: failed to delete %s", f.name)
    return removed
