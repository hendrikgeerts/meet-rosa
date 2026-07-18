"""TenderNed JSON-API client. Twee endpoints:

1. `/papi/tenderned-rs-tns/publicaties?page=0&size=N` — laatste N
   publicaties (summary). Voor de polling-loop.
2. `/papi/tenderned-rs-tns/publicaties/{publicatieId}` — volledige
   detail van één publicatie inclusief CPV-codes, trefwoord1/2,
   NUTS-regio. Voor matching + opslag.

Publiek endpoint, geen auth. Audit-wrap via `core.external_audit.timed_call`
voor egress-tracking.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from core.external_audit import timed_call

log = logging.getLogger(__name__)

_BASE = "https://www.tenderned.nl/papi/tenderned-rs-tns"
_DEFAULT_TIMEOUT = 20.0
import os as _os
_OPERATOR_CONTACT = _os.environ.get("UPTIME_OPERATOR_CONTACT", "").strip()
_USER_AGENT = (
    f"rosa-tenders/1.0 (+mailto:{_OPERATOR_CONTACT})"
    if _OPERATOR_CONTACT
    else "rosa-tenders/1.0"
)


class TenderNedError(RuntimeError):
    """Iets ging mis bij het ophalen — netwerk, status, of JSON parse."""


class TenderNedRateLimited(TenderNedError):
    """HTTP 429. `retry_after_seconds` is de Retry-After header (cap 3600)."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"rate-limited, retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


def _parse_retry_after(value: str | None) -> int:
    """Retry-After header kan een int-seconden zijn of een HTTP-date. We
    accepteren alleen integers en cappen op 1 uur — alles boven dat is
    pathologisch of een misconfiguration."""
    if not value:
        return 60
    try:
        n = int(value.strip())
    except (TypeError, ValueError):
        return 60
    return max(1, min(n, 3600))


def _request(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        # M1 — honoreer Retry-After bij 429 zodat we niet doorhameren op
        # een al overbelast endpoint. Worker kan deze fout opvangen en
        # de polling-interval verlengen.
        if e.code == 429:
            retry_after = _parse_retry_after(e.headers.get("Retry-After"))
            raise TenderNedRateLimited(retry_after) from e
        raise TenderNedError(f"HTTP {e.code} on {url}") from e
    except urllib.error.URLError as e:
        raise TenderNedError(f"URL error on {url}: {e}") from e
    except TimeoutError as e:
        raise TenderNedError(f"timeout on {url}") from e
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise TenderNedError(f"JSON parse failed on {url}: {e}") from e


def fetch_recent_summaries(
    *, size: int = 100, page: int = 0, timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Pak de laatste `size` publicaties (summary). Default 100 — TenderNed
    publiceert ~100-200 items/dag dus 30 min polling met size=100 mist
    niets. Returns lijst van dicts met o.a. `publicatieId`, `kenmerk`,
    `aanbestedingNaam`."""
    url = f"{_BASE}/publicaties?page={int(page)}&size={int(size)}"
    with timed_call(service="tenderned", endpoint="publicaties.list",
                     note=f"size={size}") as ctx:
        payload = _request(url, timeout=timeout)
        ctx.set(status=200)
    if not isinstance(payload, dict):
        raise TenderNedError(f"unexpected payload shape: {type(payload).__name__}")
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    return content


def fetch_publication_detail(
    publicatie_id: int, *, timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Pak volledige detail van één publicatie. Inclusief
    `cpvCodes` (array van dicts met code/omschrijving/isHoofdOpdracht),
    `trefwoord1`, `trefwoord2`, `nutsCodes`, `sluitingsDatum`, etc."""
    url = f"{_BASE}/publicaties/{int(publicatie_id)}"
    with timed_call(service="tenderned", endpoint="publicaties.detail",
                     note=f"id={publicatie_id}") as ctx:
        payload = _request(url, timeout=timeout)
        ctx.set(status=200)
    if not isinstance(payload, dict):
        raise TenderNedError(f"detail payload not a dict for id {publicatie_id}")
    return payload


def overview_url(publicatie_id: int) -> str:
    """Public TenderNed UI-link voor in alerts en `tenders_search`."""
    return f"https://www.tenderned.nl/aankondigingen/overzicht/{int(publicatie_id)}"
