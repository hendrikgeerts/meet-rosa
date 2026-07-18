"""Deadline-extractie uit mail/Slack-tekst voor open_loops.

Heuristiek: regex-set voor de meest voorkomende patronen ("voor vrijdag",
"by Friday", "morgen 17u", "uiterlijk 1 mei"). Bij hit → bereken absolute
datetime t.o.v. message-datum, return unix-ts.

Bewust regex-only (geen Llama) zodat de ingest-flow niet nog meer Ollama-
calls krijgt. Edge-cases die regex mist blijven `due_at = NULL` — geen
verbetering vergeleken met v1, geen verslechtering.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")

# Mapping van weekdagnaam → weekday() index
_WEEKDAYS = {
    "ma": 0, "maandag": 0, "monday": 0, "mon": 0,
    "di": 1, "dinsdag": 1, "tuesday": 1, "tue": 1, "tues": 1,
    "wo": 2, "woensdag": 2, "wednesday": 2, "wed": 2,
    "do": 3, "donderdag": 3, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "vr": 4, "vrijdag": 4, "friday": 4, "fri": 4,
    "za": 5, "zaterdag": 5, "saturday": 5, "sat": 5,
    "zo": 6, "zondag": 6, "sunday": 6, "sun": 6,
}

# "voor/before vrijdag" — eind-van-die-dag (17:00) als deadline.
_DAY_RE = re.compile(
    r"(?i)\b(?:voor|before|by|deadline|uiterlijk|tegen)\s+"
    r"(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"ma|di|wo|do|vr|za|zo|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
)

# "voor morgen 17u" / "tomorrow 5pm" / "vandaag eod"
_RELATIVE_RE = re.compile(
    r"(?i)\b(?:voor|before|by|tegen|uiterlijk)\s+"
    r"(vandaag|today|morgen|tomorrow|overmorgen)"
    r"(?:\s+(\d{1,2})(?::(\d{2}))?\s*(?:u|uur|am|pm|h)?)?\b"
)

# "eind van de week" / "end of week" / "EOW" / "EOD"
_BUCKET_RE = re.compile(
    r"(?i)\b("
    r"eind\s+van\s+de\s+week|einde\s+week|end\s+of\s+(?:the\s+)?week|eow|"
    r"end\s+of\s+(?:the\s+)?day|eod|cob|close\s+of\s+business|"
    r"begin\s+volgende\s+week|next\s+week|volgende\s+week|"
    r"deze\s+week|this\s+week"
    r")\b"
)

# Expliciete datum: "voor 1 mei" / "by May 5" / "voor 15-05"
_DATE_RE = re.compile(
    r"(?i)\b(?:voor|before|by|deadline|uiterlijk)\s+"
    r"(\d{1,2})[-/\s]+"
    r"(jan|feb|mrt|maa|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec|"
    r"januari|februari|maart|march|april|june|july|august|september|"
    r"october|november|december|"
    r"\d{1,2})"
)

_NL_MONTHS = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2,
    "mrt": 3, "maa": 3, "maart": 3, "march": 3,
    "apr": 4, "april": 4, "mei": 5, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9,
    "okt": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def extract_deadline(text: str, *, ref: datetime | None = None) -> int | None:
    """Probeer een deadline uit `text` te halen, returns unix-ts of None.

    `ref` is het referentie-moment voor relatieve termen (default: nu in
    Europe/Amsterdam tz). Voor reproducible tests altijd ref meegeven."""
    if not text:
        return None
    if ref is None:
        ref = datetime.now(TZ)
    elif ref.tzinfo is None:
        ref = ref.replace(tzinfo=TZ)

    # Try most-specific eerst (datums, dan relatief, dan day-of-week, dan bucket)
    for fn in (_match_explicit_date, _match_relative,
               _match_day_of_week, _match_bucket):
        ts = fn(text, ref)
        if ts is not None:
            return ts
    return None


def _at(d: date, hour: int = 17, minute: int = 0) -> int:
    """Combineer datum met tijd in Europe/Amsterdam, return unix-ts."""
    dt = datetime.combine(d, time(hour, minute, tzinfo=TZ))
    return int(dt.timestamp())


def _match_day_of_week(text: str, ref: datetime) -> int | None:
    m = _DAY_RE.search(text)
    if not m:
        return None
    day_word = m.group(1).lower()
    target_idx = _WEEKDAYS.get(day_word)
    if target_idx is None:
        return None
    today = ref.date()
    days_ahead = (target_idx - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # "voor vrijdag" op vrijdag → volgende vrijdag
    return _at(today + timedelta(days=days_ahead))


def _match_relative(text: str, ref: datetime) -> int | None:
    m = _RELATIVE_RE.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    today = ref.date()
    if word in ("vandaag", "today"):
        d = today
    elif word in ("morgen", "tomorrow"):
        d = today + timedelta(days=1)
    elif word == "overmorgen":
        d = today + timedelta(days=2)
    else:
        return None

    hour_str = m.group(2)
    minute_str = m.group(3)
    if hour_str:
        hh = int(hour_str)
        # Disambig pm uit context — als "5 pm" → 17. Voor scope: keep simple.
        # 1-7 zonder am/pm interpreten als middag (5u → 17u) want zakelijk
        # spreken we niet over 5 uur 's nachts. >12 of >=17 zelf.
        if hh < 8:
            hh += 12
        mm = int(minute_str) if minute_str else 0
        return _at(d, hh, mm)
    return _at(d, 17)


def _match_bucket(text: str, ref: datetime) -> int | None:
    m = _BUCKET_RE.search(text)
    if not m:
        return None
    bucket = m.group(1).lower()
    today = ref.date()
    if "eod" in bucket or "cob" in bucket or ("end of" in bucket and "day" in bucket):
        return _at(today, 17)
    if "eow" in bucket or "eind" in bucket or "einde week" in bucket or "end of" in bucket:
        # Eind van de week = vrijdag 17u
        days_ahead = (4 - today.weekday()) % 7
        if days_ahead == 0 and ref.hour >= 17:
            days_ahead = 7
        return _at(today + timedelta(days=days_ahead))
    if "deze week" in bucket or "this week" in bucket:
        # Default deze week → vrijdag
        days_ahead = (4 - today.weekday()) % 7
        return _at(today + timedelta(days=days_ahead))
    if "volgende week" in bucket or "next week" in bucket:
        # Volgende maandag
        days_ahead = (7 - today.weekday()) % 7 or 7
        return _at(today + timedelta(days=days_ahead), 9)
    return None


def _match_explicit_date(text: str, ref: datetime) -> int | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    day = int(m.group(1))
    month_token = m.group(2).lower()
    month = _NL_MONTHS.get(month_token)
    if month is None:
        # Mogelijk numeriek (DD-MM)
        if month_token.isdigit():
            month = int(month_token)
        else:
            return None
    year = ref.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if d < ref.date():
        # Datum is in het verleden → bedoel volgende jaar
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None
    return _at(d)
