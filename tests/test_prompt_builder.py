"""Tests voor core.prompt_builder — SYSTEM_PROMPT per-installatie render.

Twee belangrijke garanties:

  1. Hendrik-modus (user.name='Hendrik'): output identiek aan input.
     Voorkomt regressie op zijn draaiende daemon met ROSA_DEV=1.

  2. Nieuwe klant (user.name='Alex'): elke "Hendrik"-mention vervangen.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.prompt_builder import render_system_prompt


def _fake_settings(user_name: str, user_company: str = "") -> MagicMock:
    s = MagicMock()
    s.user_name = user_name
    s.user_company = user_company
    return s


def test_hendrik_setup_produces_identical_output() -> None:
    """ROSA_DEV=1 met user.name='Hendrik' → geen wijzigingen. Cruciale
    guard rail zodat Hendrik's live daemon niet raakt bij de generic
    refactor."""
    template = "You are Rosa, Hendrik's personal assistant. Hendrik uses this."
    out = render_system_prompt(template, _fake_settings("Hendrik"))
    assert out == template


def test_new_user_replaces_hendrik_refs() -> None:
    template = "You are Rosa, Hendrik's personal assistant. Hendrik uses this."
    out = render_system_prompt(template, _fake_settings("Alex"))
    assert "Hendrik" not in out
    assert "Alex's personal assistant" in out
    assert "Alex uses this" in out


def test_user_name_placeholder_substitution() -> None:
    template = "Reply to ${user_name} directly. Signature: ${user_signature}."
    out = render_system_prompt(template, _fake_settings("Sam"))
    assert "${user_name}" not in out
    assert "${user_signature}" not in out
    assert "Reply to Sam directly" in out
    assert "Signature: Sam" in out


def test_empty_user_name_falls_back_to_you() -> None:
    template = "Hi ${user_name}, ready?"
    out = render_system_prompt(template, _fake_settings(""))
    assert out == "Hi you, ready?"


def test_possessive_ordering_avoids_yous_bug() -> None:
    """Naive .replace("Hendrik", "you") followed by .replace("Hendrik's", ...)
    would create "you's" which is grammatically wrong. Order matters."""
    template = "Hendrik's mail is Hendrik@example.com."
    out = render_system_prompt(template, _fake_settings("you"))
    assert "you's mail" in out
    assert "you@example.com" in out
    assert "Hendrik" not in out


def test_hendrikdpm_imap_placeholder_generified_for_non_hendrik() -> None:
    template = "Set source='imap', account='hendrikdpm' for that mailbox."
    out = render_system_prompt(template, _fake_settings("Alex"))
    assert "hendrikdpm" not in out
    assert "mymail" in out


def test_hendrikdpm_kept_for_hendrik_himself() -> None:
    template = "Set source='imap', account='hendrikdpm' for that mailbox."
    out = render_system_prompt(template, _fake_settings("Hendrik"))
    assert "hendrikdpm" in out


def test_empty_template_passthrough() -> None:
    assert render_system_prompt("", _fake_settings("Alex")) == ""


def test_ceo_letter_company_substitution_when_set() -> None:
    template = "Hendrik is CEO at DST Templates / HGE Ventures. Reflect."
    out = render_system_prompt(
        template, _fake_settings("Alex", "Acme Ventures"),
    )
    assert "DST Templates" not in out
    assert "HGE Ventures" not in out
    assert "Acme Ventures" in out
    assert "Alex is CEO at Acme Ventures" in out


def test_ceo_letter_company_falls_back_generic_when_unset() -> None:
    template = "Hendrik is CEO at DST Templates / HGE Ventures. Reflect."
    out = render_system_prompt(template, _fake_settings("Alex", ""))
    assert "DST Templates" not in out
    assert "HGE Ventures" not in out
    assert "Alex is CEO at his company" in out


def test_hendrik_ceo_letter_unchanged() -> None:
    """Regressie-guard: Hendrik's eigen CEO-letter blijft byte-identiek."""
    template = (
        "You are Rosa, Hendrik's personal assistant. Hendrik is CEO at "
        "DST Templates / HGE Ventures. Reflect for Hendrik."
    )
    out = render_system_prompt(
        template,
        _fake_settings("Hendrik", "DST Templates / HGE Ventures"),
    )
    assert out == template


def test_user_company_placeholder_substitution() -> None:
    template = "Reflect on ${user_company} this week."
    out = render_system_prompt(
        template, _fake_settings("Alex", "Acme"),
    )
    assert "Reflect on Acme this week." == out


def test_user_company_placeholder_generic_fallback() -> None:
    template = "Reflect on ${user_company} this week."
    out = render_system_prompt(template, _fake_settings("Alex", ""))
    assert "the business you run" in out
