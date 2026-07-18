"""Parse [PA-LOC] e-mails naar coords.

Ondersteunt meerdere body-formats — iOS Shortcuts rendert de
'Current Location' variable verschillend per actie en per
iOS-versie. We accepteren alle volgende varianten:

  # Format 1: expliciet ingetypt (originele PA-LOC)
  PA-LOCATION
  lat: 52.3702
  lon: 4.8952
  acc: 12

  # Format 2: Apple Maps URL (komt vaak als iOS de 'Current
  # Location' variable in een mail-body inplakt)
  https://maps.apple.com/?ll=52.3702,4.8952
  https://maps.apple.com/?q=Loc&ll=52.3702,4.8952&...

  # Format 3: degree-notatie ("Current Location" als plain text)
  52.3702° N, 4.8952° E
  52.3702 N 4.8952 E

  # Format 4: "Latitude: X / Longitude: Y" (mail render)
  Latitude: 52.3702
  Longitude: 4.8952

  # Format 5: geo: URI
  geo:52.3702,4.8952

Tolerant voor whitespace, hoofdletters, en extra regels.
"""
from __future__ import annotations

import re

# Format 1 + 4 (gedeeld): lat: X / latitude: X / Latitude: X / Lat=X
_LAT = re.compile(r"(?im)\blat(?:itude)?\s*[:=]\s*(-?\d+(?:\.\d+)?)")
_LON = re.compile(r"(?im)\blon(?:gitude|g)?\s*[:=]\s*(-?\d+(?:\.\d+)?)")
_ACC = re.compile(r"(?im)\bacc(?:uracy)?\s*[:=]\s*(\d+(?:\.\d+)?)")

# Format 2: maps.apple.com URL met ll=LAT,LON query param
_MAPS_URL = re.compile(
    r"maps\.apple\.com/[^\s)]*[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)",
    re.IGNORECASE,
)

# Format 3: degree-notation "52.3702° N, 4.8952° E" (N/S/E/W bepaalt sign)
_DEGREE = re.compile(
    r"(-?\d+\.\d+)\s*°?\s*([NS])[\s,]+(-?\d+\.\d+)\s*°?\s*([EW])",
    re.IGNORECASE,
)

# Format 5: geo:LAT,LON URI scheme
_GEO_URI = re.compile(r"\bgeo:(-?\d+\.\d+),(-?\d+\.\d+)")

SUBJECT_PREFIX = "[PA-LOC]"


def is_location_message(subject: str | None) -> bool:
    return bool(subject) and SUBJECT_PREFIX in (subject or "").upper()


def _valid_coords(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def parse_location_body(body: str) -> tuple[float, float, float | None] | None:
    """Returns (lat, lon, accuracy_m) of None als niet parseerbaar.
    Probeert meerdere formats in volgorde van strictheid."""
    if not body:
        return None

    # Format 2: Apple Maps URL — meest betrouwbaar als iOS Mail het inplakt
    m = _MAPS_URL.search(body)
    if m:
        try:
            lat, lon = float(m.group(1)), float(m.group(2))
            if _valid_coords(lat, lon):
                acc = _ACC.search(body)
                return lat, lon, (float(acc.group(1)) if acc else None)
        except ValueError:
            pass

    # Format 5: geo:LAT,LON
    m = _GEO_URI.search(body)
    if m:
        try:
            lat, lon = float(m.group(1)), float(m.group(2))
            if _valid_coords(lat, lon):
                acc = _ACC.search(body)
                return lat, lon, (float(acc.group(1)) if acc else None)
        except ValueError:
            pass

    # Format 3: degree-notation with N/S/E/W
    m = _DEGREE.search(body)
    if m:
        try:
            lat = float(m.group(1)) * (-1 if m.group(2).upper() == "S" else 1)
            lon = float(m.group(3)) * (-1 if m.group(4).upper() == "W" else 1)
            if _valid_coords(lat, lon):
                acc = _ACC.search(body)
                return lat, lon, (float(acc.group(1)) if acc else None)
        except ValueError:
            pass

    # Format 1 + 4: explicit lat:/lon: keys
    lat_match = _LAT.search(body)
    lon_match = _LON.search(body)
    if lat_match and lon_match:
        try:
            lat = float(lat_match.group(1))
            lon = float(lon_match.group(1))
            if _valid_coords(lat, lon):
                acc = _ACC.search(body)
                return lat, lon, (float(acc.group(1)) if acc else None)
        except ValueError:
            pass

    return None
