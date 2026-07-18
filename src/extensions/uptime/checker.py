"""Pure HTTP-check functie. Stuurt één GET, returnt CheckResult.

Geen DB-state, geen alerts — alleen "doet de site het". Caller
(UptimeWorker) interpreteert + persist.

Hardening (post-review 25/5):
  R1 — honor Retry-After header (429/503) → CheckResult.retry_after
  R2 — wall-clock timeout via ThreadPoolExecutor zodat slow-drip TLS
       de worker niet indefinitely vasthoudt
  R6 — error-veld truncate + scrub voordat het in DB belandt
  M2 — only https:// targets accepted in load_targets
  M3 — User-Agent met operator-contact (RFC mailto-stijl)
  M4 — redirect-following disabled (3xx = check-failure)
"""
from __future__ import annotations

import email.utils
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from core.external_audit import timed_call
from core.log_scrub import scrub as _scrub_pii
from extensions.uptime.schema import CheckResult

log = logging.getLogger(__name__)

# M3: operator-identifiable UA zodat WAFs ons kunnen whitelisten en niet
# als generic-bot blokkeren. Operator-mailto uit env, anders neutrale
# placeholder zodat de string nog werkt zonder secret-leak.
_OPERATOR_CONTACT = os.environ.get(
    "UPTIME_OPERATOR_CONTACT",
    "",  # leeg = generic bot; wordt door setup-wizard gevuld met user.email
).strip()
_USER_AGENT = (
    f"rosa-uptime/0.1 (+mailto:{_OPERATOR_CONTACT})"
    if _OPERATOR_CONTACT
    else "rosa-uptime/0.1"
)

# Voor truncate + scrub: max length van het error-veld in DB.
_MAX_ERROR_LEN = 200


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """M4: weiger elke redirect — een homepage hoort 200 te returnen.
    3xx zonder body wordt door urllib als HTTPError gegooid, wat onze
    error-pad correct afhandelt."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _parse_retry_after(headers_obj: Any) -> int | None:
    """Parse Retry-After header. Returns seconds (int), of None als
    onparseerbaar. Accepteert zowel `X` seconds als HTTP-date format."""
    if headers_obj is None:
        return None
    val = headers_obj.get("Retry-After")
    if not val:
        return None
    val = val.strip()
    if val.isdigit():
        try:
            n = int(val)
            return max(0, min(n, 3600))  # cap op 1 uur
        except ValueError:
            return None
    # HTTP-date format
    try:
        dt = email.utils.parsedate_to_datetime(val)
        delta = int(dt.timestamp() - time.time())
        return max(0, min(delta, 3600))
    except (TypeError, ValueError):
        return None


def _scrub_error(text: str | None) -> str | None:
    """Truncate + scrub PII zodat verbose error-pages geen klantnamen
    in uptime_events laten lekken."""
    if not text:
        return None
    cleaned = _scrub_pii(text)
    return cleaned[:_MAX_ERROR_LEN]


def _do_http_check(
    *,
    url: str,
    expected_status: int,
    timeout_seconds: float,
    expect_text: str | None,
) -> tuple[int | None, str | None, bytes, int | None]:
    """Inner check zonder thread-wrap. Returns
    (status_code, error_str, body_bytes_or_empty, retry_after_seconds)."""
    status: int | None = None
    error: str | None = None
    body_bytes: bytes = b""
    retry_after: int | None = None

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(req, timeout=timeout_seconds) as resp:
            status = resp.status
            if expect_text is not None:
                body_bytes = resp.read(65536)
    except urllib.error.HTTPError as e:
        status = e.code
        error = f"HTTP {e.code}: {e.reason}"
        retry_after = _parse_retry_after(getattr(e, "headers", None))
    except urllib.error.URLError as e:
        error = f"URLError: {e.reason}"
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:160]}"

    return status, error, body_bytes, retry_after


def check(
    *,
    name: str,
    url: str,
    expected_status: int = 200,
    timeout_seconds: float = 10.0,
    expect_text: str | None = None,
) -> CheckResult:
    """One synchronous HTTP GET. Always returns a CheckResult — geen
    exceptions naar de caller.

    R2: hard wall-clock cap via ThreadPoolExecutor zodat slow-drip
    responses of TLS-stalls de worker niet langer vasthouden dan
    timeout_seconds + 2.
    """
    started = time.monotonic()
    checked_at = int(time.time())

    status: int | None = None
    error: str | None = None
    body_bytes: bytes = b""
    retry_after: int | None = None

    with timed_call(service="uptime", endpoint=f"GET {url}") as ctx:
        # Wall-clock cap via een daemon-thread zodat een onafsluitbare
        # urllib-call (TLS-handshake hang, slow-drip body, etc.) de
        # uptime-worker niet vasthoudt.
        #
        # Hier zat een echte productie-bug: de oude implementatie
        # gebruikte ThreadPoolExecutor als context manager. De __exit__
        # daarvan roept shutdown(wait=True) aan, wat blokkeert tot alle
        # submitted futures klaar zijn. Bij TLS-stall blijft die future
        # voor altijd lopen en blokkeert de hele uptime-tick (incident
        # 2026-06-11: daemon hing 3,5 uur na een 503 op platforms).
        #
        # Nieuwe aanpak: plain daemon-thread, result-box, join met
        # timeout. Bij timeout laten we de thread lopen — daemon=True
        # betekent dat hij meegaat met process-exit.
        wall_timeout = timeout_seconds + 2
        result_box: list[Any] = []
        try:
            def _worker() -> None:
                try:
                    result_box.append(_do_http_check(
                        url=url, expected_status=expected_status,
                        timeout_seconds=timeout_seconds,
                        expect_text=expect_text,
                    ))
                except Exception as e:
                    result_box.append(e)

            t = threading.Thread(
                target=_worker, name=f"uptime-{name}", daemon=True,
            )
            t.start()
            t.join(timeout=wall_timeout)
            if t.is_alive() or not result_box:
                error = f"wall-clock timeout (>{wall_timeout:.0f}s)"
            else:
                payload = result_box[0]
                if isinstance(payload, BaseException):
                    error = f"{type(payload).__name__}: {str(payload)[:160]}"
                else:
                    status, error, body_bytes, retry_after = payload
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:160]}"
        ctx.set(
            status=status,
            bytes_in=len(body_bytes) if body_bytes else None,
        )

    latency_ms = int((time.monotonic() - started) * 1000)

    # "Up" = any 2xx OR 3xx response (CMS-platforms doen vaak een
    # redirect op de root naar /login of /dashboard — dat is een
    # gezonde server). We volgen redirects niet meer (M4: voorkomt
    # 3rd-party-leak bij gecompromitteerd CMS) maar accepteren ze wel
    # als bewijs dat de server reageert. Alleen 4xx/5xx + netwerk-
    # errors = down. Een 3xx komt binnen via _NoRedirectHandler als
    # HTTPError met status 3xx + error="HTTP 30x" — clear die error
    # zodat de audit-log "up" toont, niet een verwarrende fail-tekst.
    if expected_status == 200 and status is not None and 200 <= status < 400:
        ok = True
        if status >= 300:
            error = None  # was set by HTTPError handler; redirect is OK
    else:
        ok = (status == expected_status) and error is None
    if ok and expect_text:
        try:
            body_text = body_bytes.decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        if expect_text not in body_text:
            ok = False
            error = f"expected_text {expect_text!r} not found in body"

    return CheckResult(
        name=name, url=url, ok=ok,
        status_code=status, latency_ms=latency_ms,
        error=_scrub_error(error),  # R6: truncate + scrub
        checked_at=checked_at,
        retry_after=retry_after,    # R1
    )


def load_targets(yaml_path: Any) -> list[dict[str, Any]]:
    """Parse config/uptime.yaml → list of target-dicts.
    Returns [] als file ontbreekt of geen targets bevat.

    M2: targets met scheme != https worden geskipt met log-warning.
    Voorkomt klare-tekst probes en file:// SSRF-vectoren als YAML
    geen owner-verified is."""
    from pathlib import Path as _Path

    import yaml

    path = _Path(yaml_path)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("uptime: kon %s niet parsen", path)
        return []
    targets = data.get("targets") or []
    out: list[dict[str, Any]] = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        if not t.get("name") or not t.get("url"):
            log.warning("uptime: target zonder name/url overgeslagen: %r", t)
            continue
        url = str(t["url"])
        if not url.lower().startswith("https://"):
            log.warning(
                "uptime: target %r URL is niet https:// — geskipt (M2). "
                "Gebruik httpsoutbound-only voor productie-monitoring.",
                t.get("name"),
            )
            continue
        out.append({
            "name": str(t["name"]),
            "url": url,
            "expected_status": int(t.get("expected_status", 200)),
            "check_interval_seconds": int(t.get("check_interval_seconds", 60)),
            "timeout_seconds": float(t.get("timeout_seconds", 10)),
            "fail_threshold": int(t.get("fail_threshold", 2)),
            "re_alert_interval_seconds": int(t.get("re_alert_interval_seconds", 900)),
            "alert_channels": list(t.get("alert_channels") or ["imessage"]),
            "expect_text": t.get("expect_text"),
        })
    return out
