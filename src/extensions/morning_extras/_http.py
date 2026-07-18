"""HTTP-fetch helper met retry+backoff voor flaky externe services.

Open-Meteo + sommige RSS-bronnen gaven om 7:00 (briefing-piektijd) op
veel ochtenden een 502 of timeout — niet permanent maar zo timing-
gevoelig dat één poging de briefing een complete sectie kost.

Strategie: retry op transient errors (5xx, connection timeout, DNS).
Geen retry op 4xx (permanent — bv. verkeerde URL). Exponential backoff
zodat de tweede call ná de eerste-piek-rush valt.
"""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


def fetch_with_retry(
    url: str, *,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 2.0,
    user_agent: str = "pa-agent/0.1",
) -> bytes | None:
    """GET `url` met max `retries` extra pogingen na transient errors.

    Returns response bytes, of None bij blijvende fout. Logt op WARNING
    bij final failure, op INFO bij intermediate retry.

    Backoff is exponentieel: 1e retry na `backoff`s, 2e na 2*`backoff`s,
    enz. Default 2-4-8s ladder.

    Transient = 5xx HTTP, timeout, of socket-level errors.
    Permanent = 4xx HTTP (geen retry).
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    # Audit L-4 (28/6): strip query-string uit logged URL zodat eventuele
    # toekomstige callers met api-key-in-query niet hun key in agent.log
    # lekken. Huidige callers (Open-Meteo, RSS) hebben geen secrets in
    # de query — preventief.
    safe_url = _strip_query(url)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                log.warning("fetch %s failed (no retry): HTTP %d", safe_url, exc.code)
                return None
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc

        if attempt < retries:
            sleep_for = backoff * (2 ** attempt)
            log.info("fetch %s retry %d/%d in %.1fs: %s",
                      safe_url, attempt + 1, retries, sleep_for, last_exc)
            time.sleep(sleep_for)

    log.warning("fetch %s failed after %d attempts: %s",
                  safe_url, retries + 1, last_exc)
    return None


def _strip_query(url: str) -> str:
    """Return URL zonder query+fragment voor log-output."""
    import urllib.parse as _up
    try:
        parts = _up.urlsplit(url)
        return _up.urlunsplit(parts._replace(query="", fragment=""))
    except Exception:
        return url.split("?")[0]
