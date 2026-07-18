"""Unit tests voor open_loops.deadline.extract_deadline."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from extensions.open_loops.deadline import extract_deadline

TZ = ZoneInfo("Europe/Amsterdam")


def _ref(year: int = 2026, month: int = 4, day: int = 30,
         hour: int = 10) -> datetime:
    """Default ref-tijd voor reproducible tests: do 30/4/2026 10:00."""
    return datetime(year, month, day, hour, 0, tzinfo=TZ)


def _ts_to_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")


# --- day-of-week ---------------------------------------------------------

def test_voor_vrijdag_returns_friday_17h() -> None:
    """Donderdag 30/4 — 'voor vrijdag' = morgen 17:00."""
    ts = extract_deadline("Stuur jij me dit voor vrijdag?", ref=_ref())
    assert _ts_to_str(ts) == "2026-05-01 17:00"


def test_voor_vrijdag_op_vrijdag_returns_next_week() -> None:
    """Op vrijdag zelf 'voor vrijdag' = volgende vrijdag."""
    ts = extract_deadline("Voor vrijdag graag",
                            ref=_ref(month=5, day=1))  # vr 1/5
    assert _ts_to_str(ts) == "2026-05-08 17:00"


def test_by_friday_english() -> None:
    ts = extract_deadline("Could you send this by Friday?", ref=_ref())
    assert _ts_to_str(ts) == "2026-05-01 17:00"


def test_voor_maandag_jumps_to_next_monday() -> None:
    """Donderdag — 'voor maandag' = aankomende maandag."""
    ts = extract_deadline("voor maandag aub", ref=_ref())
    assert _ts_to_str(ts) == "2026-05-04 17:00"


# --- relative ------------------------------------------------------------

def test_voor_morgen_returns_tomorrow_5pm() -> None:
    ts = extract_deadline("voor morgen graag", ref=_ref())
    assert _ts_to_str(ts) == "2026-05-01 17:00"


def test_voor_vandaag_returns_today_5pm() -> None:
    ts = extract_deadline("voor vandaag eind van de dag", ref=_ref())
    assert _ts_to_str(ts) == "2026-04-30 17:00"


def test_morgen_5pm_explicit_time() -> None:
    ts = extract_deadline("by tomorrow 6pm please", ref=_ref())
    # 6 pm-style → 18:00
    assert _ts_to_str(ts) == "2026-05-01 18:00"


def test_voor_morgen_17u() -> None:
    ts = extract_deadline("voor morgen 9u", ref=_ref())
    # 9u <8 → +12 → 21:00. Hmm rare interpretatie maar consistent.
    # Voor nu: 9 valt onder "<8" niet, dus 9 → 21:00? Nee, 9 < 8 is False.
    # Laat me checken: condition is `if hh < 8: hh += 12`. 9 niet <8, blijft 9.
    assert _ts_to_str(ts) == "2026-05-01 09:00"


# --- buckets -------------------------------------------------------------

def test_eod() -> None:
    ts = extract_deadline("Need this by EOD", ref=_ref())
    assert _ts_to_str(ts) == "2026-04-30 17:00"


def test_eind_van_de_week() -> None:
    """Donderdag 30/4 — eind van de week = vrijdag 17:00."""
    ts = extract_deadline("Voor eind van de week graag", ref=_ref())
    # match patterns: bucket "eind van de week" → vrijdag 17u
    assert _ts_to_str(ts) == "2026-05-01 17:00"


def test_eow_already_friday_after_5pm() -> None:
    """Op vrijdag na 17u → eind volgende week."""
    ref = _ref(month=5, day=1, hour=18)  # vr 1/5 18:00
    ts = extract_deadline("EOW please", ref=ref)
    assert _ts_to_str(ts) == "2026-05-08 17:00"


def test_volgende_week() -> None:
    """Donderdag 30/4 → volgende maandag 9:00."""
    ts = extract_deadline("Lever volgende week aan", ref=_ref())
    # Hmm — vereist 'voor/before' prefix. Dit zou geen hit geven.
    # Test eigenlijk dat zonder 'voor' prefix deze geen match geeft
    # OR dat de bucket-regex zelfs zonder prefix matcht.
    # Bekijk regex — _BUCKET_RE matcht zonder prefix. Dus → ja deze match.
    assert _ts_to_str(ts) == "2026-05-04 09:00"


# --- explicit dates ------------------------------------------------------

def test_voor_15_mei() -> None:
    ts = extract_deadline("Voor 15 mei moet dit binnen zijn", ref=_ref())
    assert _ts_to_str(ts) == "2026-05-15 17:00"


def test_by_may_5_english() -> None:
    ts = extract_deadline("by 5 May please", ref=_ref())
    assert _ts_to_str(ts) == "2026-05-05 17:00"


def test_past_date_jumps_to_next_year() -> None:
    """Op 30/4/2026 'voor 5 jan' moet doelen op 2027."""
    ts = extract_deadline("voor 5 jan factuur sluiten", ref=_ref())
    assert _ts_to_str(ts) == "2027-01-05 17:00"


# --- no match -------------------------------------------------------------

def test_no_keyword_returns_none() -> None:
    assert extract_deadline("Hoe gaat het met je?", ref=_ref()) is None


def test_empty_returns_none() -> None:
    assert extract_deadline("", ref=_ref()) is None
    assert extract_deadline("   ", ref=_ref()) is None


def test_random_text_returns_none() -> None:
    assert extract_deadline("Notulen van vorige week zijn klaar.",
                              ref=_ref()) is None
