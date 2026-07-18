"""Audit-log helper voor elke externe HTTP-call die NIET via gateway loopt.

Gateway zelf logt al per Claude-call (egress-jsonl). Deze helper extends
dezelfde audit-stream voor sub-processors die the user niet via Claude
benadert: HERE Maps, Todoist, ElevenLabs, SMTP-providers, RSS-feeds.

Gebruik:
  - main.py roept `bind_audit(audit_logger)` éénmaal aan bij startup.
  - Integrations roepen `log_external(service, endpoint, ...)` aan na
    elke call. Geen content — alleen metadata zoals service, endpoint,
    bytes, status, latency_ms.

Als `bind_audit` niet is aangeroepen (tests, scripts) dan is `log_external`
een no-op zodat we niet crashen op missing init.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from core.audit import AuditLogger

log = logging.getLogger(__name__)


_audit: AuditLogger | None = None


def bind_audit(audit: AuditLogger) -> None:
    """Roep eenmaal aan bij startup. Daarna kunnen integrations log_external
    gebruiken zonder dat ze de AuditLogger expliciet hoeven te krijgen."""
    global _audit
    _audit = audit


def log_external(
    *,
    service: str,
    endpoint: str,
    status: int | None = None,
    bytes_out: int | None = None,
    bytes_in: int | None = None,
    latency_ms: int | None = None,
    note: str | None = None,
) -> None:
    """Log één external HTTP/SMTP-call. No-op als audit niet gebonden.

    Veld-conventies (consistentie voor latere queries):
      service     — 'here_maps' | 'todoist' | 'elevenlabs' | 'smtp:<account>'
                    | 'rss' | 'google_news' etc.
      endpoint    — semantic ID, bv. 'POST /v8/routes' (geen full URL met API-key)
      status      — HTTP code of None bij netwerk-fout
      bytes_out   — request body size approx
      bytes_in    — response body size approx
      latency_ms  — round-trip duur indien gemeten
      note        — optionele context, bv. 'cache_hit'
    """
    if _audit is None:
        return
    try:
        _audit.log(
            "external_call",
            service=service,
            endpoint=endpoint,
            status=status,
            bytes_out=bytes_out,
            bytes_in=bytes_in,
            latency_ms=latency_ms,
            note=note,
        )
    except Exception:
        log.exception("external_audit: failed to log %s call", service)


class _Timer:
    """Hulp-context manager om latency te meten + log_external te callen.

    Gebruik:
      with timed_call(service='here_maps', endpoint='POST /v8/routes') as ctx:
          response = httpx.post(...)
          ctx.set(status=response.status_code,
                  bytes_in=len(response.content))
    """
    def __init__(self, **fields: Any) -> None:
        self._fields = fields
        self._start = 0.0

    def __enter__(self) -> "_Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        latency_ms = int((time.monotonic() - self._start) * 1000)
        # Bij exception: log toch — status=None signaleert netwerk-fout.
        log_external(latency_ms=latency_ms, **self._fields)

    def set(self, **extra: Any) -> None:
        self._fields.update(extra)


def timed_call(**fields: Any) -> _Timer:
    return _Timer(**fields)


def audit_googleapi_execute(
    req: Any,
    *,
    service: str,
    endpoint: str,
    note: str | None = None,
) -> Any:
    """Wrap a google-api-python-client request `.execute()` in an audit-
    timer so every Gmail / Calendar egress lands in the audit-jsonl.
    SECURITY_REVIEW_2 MEDIUM-7.

    On success: logs latency + status=200 + the semantic endpoint name
    (no URL with API-key) + optional note (e.g. item-count). On HttpError
    or network failure: `timed_call` still logs the call with status=None
    on `__exit__`, so the audit-trail captures failed attempts too.

    Note: bytes_in/out are NOT populated — the googleapiclient hides the
    raw HTTP response size behind its discovery wrapper. If post-incident
    volume analysis becomes critical, wrap `httplib2.Http` instead.
    """
    with timed_call(service=service, endpoint=endpoint, note=note) as ctx:
        resp = req.execute()
        ctx.set(status=200)
    return resp
