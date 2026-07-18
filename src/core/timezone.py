"""Active-timezone resolver — Rosa's schedule schakelt mee als the user
in een andere tijdzone zit.

Bron van waarheid: `app_state` key `active_timezone`. Bij afwezigheid:
de default uit settings (= meestal `Europe/Amsterdam`).

Caching: een short-TTL in-memory cache (5s) voorkomt dat élke
scheduler-tick een DB-hit doet. De tick draait elke 10s dus net-niet-
real-time updates is geen probleem. Bij `set_active_timezone()`
wordt de cache direct ge-invalideerd zodat the user niet 5s hoeft te
wachten op een tz-switch.

Thread-safety: cache is een tuple in een module-level var, atomic
read/write op CPython. Geen lock nodig voor de simpele use-case.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import app_state

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 5.0
_KEY = "active_timezone"

# (tz_name, ZoneInfo-instance, expires_at_monotonic)
_cache: tuple[str, ZoneInfo, float] | None = None

# Module-level default db_path zodat we niet elke caller (briefings,
# dayclose, midday, ceo_letter, tools) een db_path-parameter hoeven
# te geven. main.py roept `bind_db_path(settings.db_path)` aan bij
# startup. Vergelijkbaar pattern als external_audit.bind_audit().
_default_db_path: Path | None = None
_default_tz_name: str = "Europe/Amsterdam"


def bind(db_path: Path, *, default_timezone: str = "Europe/Amsterdam") -> None:
    """Roep aan bij daemon-startup. Daarna kunnen modules `now_local()`
    of `current_tz()` aanroepen zonder db_path-context.

    H4: idempotency-guard — bij rebind met andere db_path log een
    warning zodat test-omgevingen die per ongeluk meerdere keren binden
    detecteerbaar zijn."""
    global _default_db_path, _default_tz_name
    if _default_db_path is not None and _default_db_path != db_path:
        log.warning(
            "timezone.bind: rebinding from %s to %s — earlier callers may "
            "still reference the old db_path",
            _default_db_path, db_path,
        )
    _default_db_path = db_path
    _default_tz_name = default_timezone
    invalidate_cache()


def current_tz() -> ZoneInfo:
    """Active TZ zonder db_path-arg. Vereist eerder `bind()`. Valt
    terug op Europe/Amsterdam als `bind` niet is geroepen (tests)."""
    if _default_db_path is None:
        return ZoneInfo(_default_tz_name)
    return active_tz(db_path=_default_db_path, default=_default_tz_name)


def default_tz_name() -> str:
    """De default TZ uit settings (= waar Rosa naar terugvalt bij
    'rosa tz home'). Voor tools die status willen tonen."""
    return _default_tz_name


def now_local() -> datetime:
    """datetime.now(current_tz()) — drop-in vervanger voor
    `datetime.now(TZ)` waar TZ hard-coded was."""
    return datetime.now(current_tz())


def active_tz_name(*, db_path: Path, default: str) -> str:
    """Return de IANA-naam van de actieve TZ. Default als app_state leeg
    is. Geen ZoneInfo-lookup hier — voor de naam alleen."""
    now = time.monotonic()
    cached = _cache
    if cached is not None and cached[2] > now:
        return cached[0]
    val = app_state.get(db_path, key=_KEY, default=None)
    return val or default


def active_tz(*, db_path: Path, default: str = "Europe/Amsterdam") -> ZoneInfo:
    """Return ZoneInfo voor de actieve TZ. Gecached (~5s TTL).
    Fallback naar default (of hard-coded Europe/Amsterdam als default
    zelf invalid is — bv. unit-tests met MagicMock settings)."""
    global _cache
    now = time.monotonic()
    cached = _cache
    if cached is not None and cached[2] > now:
        return cached[1]

    # Coerce default zodat tests met MagicMock-settings niet crashen.
    try:
        default_str = str(default) if default else "Europe/Amsterdam"
        ZoneInfo(default_str)  # validate
    except (ZoneInfoNotFoundError, ValueError):
        default_str = "Europe/Amsterdam"

    name = app_state.get(db_path, key=_KEY, default=None) or default_str
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "timezone: invalid IANA-naam %r in app_state — falling back to %s",
            name, default_str,
        )
        name = default_str
        tz = ZoneInfo(default_str)
    _cache = (name, tz, now + _CACHE_TTL_SECONDS)
    return tz


_ALIASES: dict[str, str] = {
    # M2: common abbreviations die the user / Claude intuïtief gebruikt.
    # Eén-op-één naar IANA-zone. Lowercase-lookup.
    "pst": "America/Los_Angeles",
    "pt":  "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "est": "America/New_York",
    "et":  "America/New_York",
    "edt": "America/New_York",
    "cst": "America/Chicago",
    "ct":  "America/Chicago",
    "mst": "America/Denver",
    "mt":  "America/Denver",
    "jst": "Asia/Tokyo",
    "ist": "Asia/Kolkata",
    "kst": "Asia/Seoul",
    "sgt": "Asia/Singapore",
    "hkt": "Asia/Hong_Kong",
    "gst": "Asia/Dubai",
    "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney",
    "cet": "Europe/Amsterdam",
    "cest": "Europe/Amsterdam",
    "gmt": "Etc/GMT",
    "utc": "Etc/UTC",
    "bst": "Europe/London",
}


def set_active_timezone(*, db_path: Path, name: str | None) -> None:
    """Set/clear active TZ. name=None of "home"/"reset"/"off" wist de
    override — Rosa valt terug op de default uit settings.

    Common abbreviations (PST/EST/JST/CET/UTC etc.) worden gemapt op
    de canonical IANA-zone via _ALIASES.

    H3-fix: cache wordt eerst geïnvalideerd, dán DB-write. Race is
    omgekeerd: lezer ziet kort oude DB-waarde (zelf-corrigerend bij
    volgende read) ipv stale cache voor max 5s.

    Raises ValueError als name geen geldige IANA-zone en geen alias is."""
    global _cache
    is_reset = name is None or name.lower() in ("home", "reset", "off", "")
    if not is_reset:
        # Try alias first, then plain IANA.
        canonical = _ALIASES.get(name.lower(), name)
        try:
            ZoneInfo(canonical)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown IANA timezone: {name!r}")
        # H3: invalidate cache eerst zodat een concurrent lezer niet
        # de oude cache krijgt na de DB-write.
        _cache = None
        app_state.set_value(db_path, key=_KEY, value=canonical)
    else:
        _cache = None
        app_state.set_value(db_path, key=_KEY, value=None)


def active_now(*, db_path: Path, default: str = "Europe/Amsterdam") -> datetime:
    """Convenience: datetime.now(active_tz). Drop-in vervanger voor
    plekken die nu `datetime.now(TZ)` doen waar TZ hard-coded was."""
    return datetime.now(active_tz(db_path=db_path, default=default))


def invalidate_cache() -> None:
    """Voor tests of post-config-change."""
    global _cache
    _cache = None
