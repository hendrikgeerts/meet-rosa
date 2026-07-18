"""Tests voor birthday/jubilea tracker."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from extensions.birthdays.tracker import (
    _next_anniversary,
    _parse_date,
    describe_today,
    list_upcoming,
)


@pytest.fixture
def vip_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "vip.yaml"
    p.write_text(
        "people:\n"
        "  - name: Piet Janssens\n"
        "    birthday: '1985-04-26'\n"
        "    tier: A\n"
        "  - name: Anouk\n"
        "    birthday: '1990-05-03'\n"
        "    jubilea:\n"
        "      - { date: '2018-09-01', label: 'Begin samenwerking' }\n"
        "  - name: Geen birthday\n"
        "    tier: C\n"
        "organizations:\n"
        "  - name: Initiale\n"
        "    founded: '2015-01-15'\n"
    )
    return p


# --- helpers --------------------------------------------------------------

def test_parse_date_valid() -> None:
    assert _parse_date("2026-04-26") == date(2026, 4, 26)


def test_parse_date_invalid_returns_none() -> None:
    assert _parse_date("nonsense") is None
    assert _parse_date(None) is None
    assert _parse_date("") is None


def test_next_anniversary_future_in_year() -> None:
    today = date(2026, 1, 1)
    base = date(1985, 6, 12)
    assert _next_anniversary(base, today) == date(2026, 6, 12)


def test_next_anniversary_past_rolls_to_next_year() -> None:
    today = date(2026, 7, 1)
    base = date(1985, 6, 12)
    assert _next_anniversary(base, today) == date(2027, 6, 12)


def test_next_anniversary_today() -> None:
    today = date(2026, 6, 12)
    base = date(1985, 6, 12)
    assert _next_anniversary(base, today) == date(2026, 6, 12)


def test_next_anniversary_handles_leap_day() -> None:
    today = date(2026, 1, 1)
    base = date(2000, 2, 29)
    # 2026 is geen schrikkeljaar → moet 1 maart zijn
    assert _next_anniversary(base, today) == date(2026, 3, 1)


# --- list_upcoming --------------------------------------------------------

def test_list_today_finds_birthday(vip_yaml: Path) -> None:
    items = list_upcoming(vip_yaml, days_forward=0, today=date(2026, 4, 26))
    assert any(i["name"] == "Piet Janssens" and i["kind"] == "birthday" for i in items)
    piet = next(i for i in items if i["name"] == "Piet Janssens")
    assert piet["turning_age"] == 41
    assert piet["days_until"] == 0


def test_list_window_finds_jubileum(vip_yaml: Path) -> None:
    # Jubileum 1 sept; check op 25 augustus, window 14 dagen
    items = list_upcoming(vip_yaml, days_forward=14, today=date(2026, 8, 25))
    jub = [i for i in items if i["kind"] == "jubileum"]
    assert len(jub) == 1
    assert jub[0]["name"] == "Anouk"
    assert jub[0]["years"] == 8
    assert jub[0]["days_until"] == 7


def test_list_includes_org_anniversary(vip_yaml: Path) -> None:
    items = list_upcoming(vip_yaml, days_forward=14, today=date(2026, 1, 14))
    org = [i for i in items if i["kind"] == "org_anniversary"]
    assert len(org) == 1
    assert org[0]["name"] == "Initiale"
    assert org[0]["years"] == 11


def test_list_skips_people_without_birthday(vip_yaml: Path) -> None:
    items = list_upcoming(vip_yaml, days_forward=365, today=date(2026, 1, 1))
    names = [i["name"] for i in items]
    assert "Geen birthday" not in names


def test_list_sorted_by_days_until(vip_yaml: Path) -> None:
    items = list_upcoming(vip_yaml, days_forward=365, today=date(2026, 1, 1))
    days = [i["days_until"] for i in items]
    assert days == sorted(days)


# --- describe_today ------------------------------------------------------

def test_describe_today_birthday(vip_yaml: Path) -> None:
    out = describe_today(vip_yaml, today=date(2026, 4, 26))
    assert out is not None
    assert "Piet Janssens" in out
    assert "🎂" in out
    assert "41" in out


def test_describe_today_returns_none_when_empty(vip_yaml: Path) -> None:
    out = describe_today(vip_yaml, today=date(2026, 12, 31))
    assert out is None


def test_list_with_missing_yaml_returns_empty(tmp_path: Path) -> None:
    out = list_upcoming(tmp_path / "nonexistent.yaml", days_forward=14)
    assert out == []
