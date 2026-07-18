"""Ntfy.sh client — push notifications die door iOS Do-Not-Disturb breken.

Ntfy.sh is een open-source pub/sub voor push-notifications: je POST een
bericht naar `https://ntfy.sh/<topic>`, iOS-app abonneert op `<topic>`
en krijgt het binnen. Geen account vereist, alleen een lange random
topic-name = je private kanaal.

Critical priority (5) zet `interruption-level=critical` op de iOS-app
zodat de melding door Focus / Do-Not-Disturb / Silent mode heen
doorkomt. Net wat je wil voor uptime-alerts 's nachts.

Geen DPA-vereiste: zelf-hostable, geen persoonsdata in topic of body
strikt nodig.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from core.external_audit import timed_call

log = logging.getLogger(__name__)


def send_push(
    *,
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: int = 5,
    tags: list[str] | None = None,
    click_url: str | None = None,
    timeout: float = 10.0,
) -> bool:
    """POST een melding naar Ntfy.sh.

    priority: 1 (min) tot 5 (critical, override Do-Not-Disturb).
    Returns True bij geslaagde POST (HTTP 200), False anders.
    """
    if not topic:
        log.debug("ntfy: no topic configured — skip")
        return False

    url = f"{server.rstrip('/')}/{urllib.parse.quote(topic, safe='')}"
    body_bytes = message.encode("utf-8")
    headers: dict[str, str] = {
        "Title": title,
        "Priority": str(priority),
        "User-Agent": "pa-agent/0.1",
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    with timed_call(service="ntfy", endpoint="POST /<topic>",
                     bytes_out=len(body_bytes)) as ctx:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body_in = resp.read()
            ctx.set(status=resp.status, bytes_in=len(body_in))
            return resp.status == 200
        except Exception:
            # Audit L-2 (28/6): Ntfy-topic is een shared secret (wie 'em
            # kent kan meelezen). Niet rauw in logs — fingerprint van
            # eerste 6 chars + lengte is genoeg voor diagnose.
            log.exception(
                "ntfy POST failed for topic=%s…(len=%d)",
                topic[:6], len(topic),
            )
            return False
