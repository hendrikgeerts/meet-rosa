"""Tests voor PII-scrub op file-log + 0600 birth-mode."""
from __future__ import annotations

import io
import logging
from pathlib import Path

from core.log_scrub import ScrubbingFileHandler, ScrubbingStreamHandler, scrub


def test_scrub_phone() -> None:
    out = scrub("incoming from +31600000000: hoi")
    assert "[PHONE]" in out
    assert "+31600000000" not in out


def test_scrub_email() -> None:
    out = scrub("reminder #4 fired to hendrik@example.com")
    assert "[EMAIL]" in out
    assert "hendrik@example.com" not in out


def test_scrub_msg_body_truncates() -> None:
    out = scrub("incoming from +31600000000: zaterdag 15:00 koffie ouders Michelle")
    assert "[BODY-REDACTED]" in out
    assert "Michelle" not in out
    assert "koffie" not in out


def test_scrub_replied_to_body() -> None:
    out = scrub("replied to +31600000000: Aanstaande zaterdag is 25 april ...")
    assert "[BODY-REDACTED]" in out
    assert "zaterdag" not in out


def test_scrub_tool_args_redacted() -> None:
    out = scrub("tool_use #1: calendar_create_event({'title': 'Koffie ouders Michelle'})")
    assert "calendar_create_event(" in out
    assert "[ARGS-REDACTED]" in out
    assert "Michelle" not in out
    assert "Koffie" not in out


def test_scrub_passes_normal_lines() -> None:
    out = scrub("scheduler started (briefing_next=2026-04-23T07:00)")
    assert out == "scheduler started (briefing_next=2026-04-23T07:00)"


# --- MEDIUM-5: extended scrub patterns -------------------------------

def test_scrub_gps_coord_pair() -> None:
    """HERE geocode failures and travel-alert logs leak lat/lon — scrub."""
    out = scrub("HERE geocode failed for '51.5407,4.9358'")
    assert "[COORD]" in out
    assert "51.5407" not in out
    assert "4.9358" not in out


def test_scrub_gps_coord_with_space() -> None:
    out = scrub("current_location row: 51.5407, 4.9358 at 2026-05-23")
    assert "[COORD]" in out
    assert "51.5407" not in out


def test_scrub_keeps_low_precision_coords() -> None:
    """3 decimals ≈ 100m precision — not in scope for PII-scrub (would
    over-redact normal version numbers / progress logs)."""
    out = scrub("ETA in 12.345 km remaining 6.789 km")
    assert "12.345" in out
    assert "[COORD]" not in out


def test_scrub_slack_user_id() -> None:
    out = scrub("comm-ingest slack/hendrikslack: U01ABCDEF12 sent message")
    assert "[SLACK_UID]" in out
    assert "U01ABCDEF12" not in out


def test_scrub_slack_uid_ignores_short_tokens() -> None:
    """Short U-prefixed strings (e.g. 'USA01') must not match."""
    out = scrub("country=USA01 region=EU")
    assert "[SLACK_UID]" not in out
    assert "USA01" in out


def test_scrub_pdf_filename() -> None:
    out = scrub("expenses: Coolblue-bestelling-12345.pdf gemarkeerd als geen bon")
    assert "[PDF]" in out
    assert "Coolblue" not in out
    assert "12345" not in out


def test_scrub_pdf_filename_with_path() -> None:
    out = scrub("attachment saved to /tmp/Datadog-invoice-Q1-2026.pdf")
    assert "[PDF]" in out
    assert "Datadog" not in out


def test_scrub_pdf_preserves_surrounding_context() -> None:
    """Regression guard: an earlier version's char-class included space
    and slash, which made the regex eat both filenames PLUS the word
    'and' in between. The basename-only regex must leave the connective
    words intact."""
    out = scrub("see report.pdf and invoice.pdf for details")
    assert "see" in out
    assert "and" in out
    assert "for details" in out
    assert out.count("[PDF]") == 2
    assert "report.pdf" not in out
    assert "invoice.pdf" not in out


def test_scrub_pdf_preserves_path_segments() -> None:
    """The `/tmp/` prefix is a directory hint, not the filename leak —
    leave it in place; only the basename gets redacted."""
    out = scrub("attachment saved to /tmp/Datadog-invoice.pdf")
    assert "saved to" in out
    assert "/tmp/" in out
    assert "[PDF]" in out


def test_stream_handler_scrubs_to_stdout() -> None:
    """ISO_AUDIT 2026-05 CRITICAL-A: stdout (under launchd) lands in
    data/logs/stdout.log. The handler routed to stdout must scrub PII
    just like the file handler."""
    buf = io.StringIO()
    handler = ScrubbingStreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test_scrub_stream")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("incoming from +31600000000: secret body content")
        logger.info("user mail hendrik@example.com forwarded")
    finally:
        handler.close()
        logger.removeHandler(handler)

    out = buf.getvalue()
    assert "[PHONE]" in out
    assert "[EMAIL]" in out
    assert "[BODY-REDACTED]" in out
    assert "+31600000000" not in out
    assert "secret body content" not in out
    assert "hendrik@example.com" not in out


def test_handler_creates_file_with_0600(tmp_path: Path) -> None:
    log_file = tmp_path / "agent.log"
    handler = ScrubbingFileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test_scrub_birth_perms")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("incoming from +31600000000: secret body content")
    finally:
        handler.close()
        logger.removeHandler(handler)

    assert log_file.exists()
    mode = log_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    content = log_file.read_text()
    assert "[PHONE]" in content
    assert "[BODY-REDACTED]" in content
    assert "secret body content" not in content
