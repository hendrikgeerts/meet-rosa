"""HERE Maps Routing v8 client — travel-time + traffic-aware duration.

Documentatie: https://www.here.com/docs/bundle/routing-api-developer-guide-v8/
Free tier: 250K requests/maand, geen credit card vereist.

We vragen route met `transportMode=car`, `routingMode=fast`, en
`return=summary,travelSummary` zodat we de (traffic-aware) duration
in seconden krijgen. HERE rekent automatisch met huidige verkeers-
situatie als `departureTime=now` (of weglaten).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.external_audit import timed_call

log = logging.getLogger(__name__)

# Valid HERE transport modes for the routing API (v8).
VALID_TRANSPORT_MODES = (
    "car", "pedestrian", "bicycle", "publicTransport", "scooter", "taxi",
)


@dataclass(frozen=True)
class RouteSummary:
    duration_seconds: int          # met traffic
    base_duration_seconds: int     # zonder traffic ("free flow")
    distance_meters: int
    transport_mode: str = "car"

    @property
    def traffic_delay_seconds(self) -> int:
        return max(0, self.duration_seconds - self.base_duration_seconds)


class HereMapsClient:
    BASE = "https://router.hereapi.com/v8/routes"
    GEOCODE = "https://geocode.search.hereapi.com/v1/geocode"

    def __init__(
        self, api_key: str, *,
        timeout: float = 10.0,
        cache_db_path: Path | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("HERE_API_KEY missing")
        self._api_key = api_key
        self._timeout = timeout
        self._cache_db_path = cache_db_path
        # In-memory L1 cache, persistent SQLite is L2 (geocode_cache table).
        # L1 saves repeat lookups within one tick; L2 saves across daemon
        # restarts so HERE quota niet onnodig wordt gebrand.
        self._mem_cache: dict[str, tuple[float, float] | None] = {}

    def geocode(self, address: str) -> tuple[float, float] | None:
        """Vrije-tekst adres → (lat, lon). Two-tier cached (memory +
        persistent SQLite). Returns None als HERE geen match vindt."""
        key = address.strip().lower()
        if not key:
            return None
        if key in self._mem_cache:
            return self._mem_cache[key]
        # L2: persistent SQLite cache (survives restarts).
        if self._cache_db_path is not None:
            from extensions.travel_alerts.schema import (
                geocode_cache_get,
            )
            try:
                with sqlite3.connect(self._cache_db_path, isolation_level=None) as conn:
                    cached = geocode_cache_get(conn, address=key)
                # Note: cached=None means either "not seen" OR "negative
                # cache hit" — we don't distinguish. To re-query a
                # previously-failed address, prune the row manually.
                if cached is not None:
                    self._mem_cache[key] = cached
                    return cached
            except sqlite3.OperationalError:
                pass  # geocode_cache table missing (older DB) — fall through

        params = {"q": address, "limit": "1", "apiKey": self._api_key}
        url = f"{self.GEOCODE}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "pa-agent/0.1"})
        with timed_call(service="here_maps",
                         endpoint="GET /v1/geocode") as audit_ctx:
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = resp.read()
                audit_ctx.set(status=resp.status, bytes_in=len(body))
                data = json.loads(body)
            except Exception:
                # Audit L-3 (28/6): adres-string kan klant-/huisadres zijn —
                # niet rauw in logs.
                log.exception(
                    "HERE geocode failed for address_len=%d",
                    len(address or ""),
                )
                self._mem_cache[key] = None
                self._persist_geocode(key, None)
                return None

        items = data.get("items") or []
        result: tuple[float, float] | None
        if not items:
            result = None
        else:
            pos = (items[0].get("position") or {})
            lat = pos.get("lat"); lon = pos.get("lng")
            result = (float(lat), float(lon)) if lat is not None and lon is not None else None
        self._mem_cache[key] = result
        self._persist_geocode(key, result)
        return result

    def _persist_geocode(self, key: str, coords: tuple[float, float] | None) -> None:
        """Write to L2 (SQLite) if cache_db_path is configured."""
        if self._cache_db_path is None:
            return
        from extensions.travel_alerts.schema import geocode_cache_set
        try:
            with sqlite3.connect(self._cache_db_path, isolation_level=None) as conn:
                geocode_cache_set(conn, address=key, coords=coords)
        except sqlite3.OperationalError:
            pass  # cache table missing — non-fatal

    def car_travel_time(
        self,
        *,
        origin_lat: float, origin_lon: float,
        dest_lat: float, dest_lon: float,
    ) -> RouteSummary | None:
        """Backwards-compat alias for travel_time(mode='car')."""
        return self.travel_time(
            origin_lat=origin_lat, origin_lon=origin_lon,
            dest_lat=dest_lat, dest_lon=dest_lon,
            mode="car",
        )

    def travel_time(
        self,
        *,
        origin_lat: float, origin_lon: float,
        dest_lat: float, dest_lon: float,
        mode: str = "car",
    ) -> RouteSummary | None:
        """Bereken route via `mode` (car / bicycle / pedestrian /
        publicTransport / scooter / taxi). Auto-mode is traffic-aware
        ('any' departureTime); fiets/lopen kennen geen traffic-delay.
        Returns None bij geen route of API-fout."""
        if mode not in VALID_TRANSPORT_MODES:
            log.warning("HERE: unknown transport mode %r, defaulting to car", mode)
            mode = "car"
        params = {
            "transportMode": mode,
            "origin": f"{origin_lat:.6f},{origin_lon:.6f}",
            "destination": f"{dest_lat:.6f},{dest_lon:.6f}",
            "return": "summary,travelSummary",
            "apiKey": self._api_key,
        }
        if mode in ("car", "scooter", "taxi"):
            params["departureTime"] = "any"  # = NOW met current traffic
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "pa-agent/0.1"})
        with timed_call(service="here_maps",
                         endpoint=f"GET /v8/routes (mode={mode})") as audit_ctx:
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = resp.read()
                audit_ctx.set(status=resp.status, bytes_in=len(body))
                data = json.loads(body)
            except Exception:
                log.exception("HERE Maps routing call failed (mode=%s)", mode)
                return None

        routes = data.get("routes") or []
        if not routes:
            return None
        sections = routes[0].get("sections") or []
        if not sections:
            return None

        total_duration = 0
        total_base = 0
        total_distance = 0
        for s in sections:
            ts = s.get("travelSummary") or s.get("summary") or {}
            total_duration += int(ts.get("duration") or 0)
            total_base += int(ts.get("baseDuration") or ts.get("duration") or 0)
            total_distance += int(ts.get("length") or 0)

        return RouteSummary(
            duration_seconds=total_duration,
            base_duration_seconds=total_base,
            distance_meters=total_distance,
            transport_mode=mode,
        )
