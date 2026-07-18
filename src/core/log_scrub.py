"""Scrub PII uit log-records vóór ze naar agent.log schrijven.

Reden: agent.log bevat onder meer iMessage-snippets, tool_use args (event-
titels met namen), en handles. Die staan op disk in een file die elk
proces onder dezelfde user kan lezen. Scrub heeft géén impact op het
runtime-gedrag — alleen op wat er gepersisteerd wordt naar `data/logs/`.

Aanpak: subclass van `logging.FileHandler` die scrubt op `format()`-tijd
(de geformatteerde string, niet de gedeelde LogRecord). Daardoor blijft
stdout/stderr ongetouched — handig voor live debugging — terwijl de
on-disk log gescrubt is. Tegelijk worden nieuwe logfiles met 0600 mode
geboren via `core.perms.open_secure`.
"""
from __future__ import annotations

import logging
import os
import re

from core.perms import open_secure

_E164 = re.compile(r"\+\d{8,15}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# `incoming from X: <body>` / `replied to X: <body>` / `outgoing to X: <body>`
# / `dayclose sent (N chars)` blijft intact (geen body).
_MSG_BODY = re.compile(
    r"((?:incoming from|replied to|outgoing to)\s+\S+:\s)(.+)$",
    re.DOTALL,
)
# `tool_use #N: tool_name({...})` — laat naam staan, redact args.
_TOOL_ARGS = re.compile(r"(tool_use\s+#\d+:\s+\w+\()([^)]*)(\))")
# Latitude/longitude pair with ≥4 decimals — home address, HERE
# geocode failures etc. Matches "51.5407,4.9358" / "51.5407, 4.9358".
# 3-decimal coords are ~100m precision; pair-with-≥4 dec is where the
# privacy risk starts.
_GPS_PAIR = re.compile(r"-?\d{1,3}\.\d{4,}\s*[,;]\s*-?\d{1,3}\.\d{4,}")
# Slack user-ID: literal U followed by ≥9 alnum chars. Real IDs are
# 9–11 chars; require ≥9 to avoid matching unrelated tokens like "USA01".
_SLACK_UID = re.compile(r"\bU[A-Z0-9]{9,}\b")
# PDF basename: `vendor-orderno-12345.pdf`. Filenames in expense logs
# typically carry vendor + order-ref — leak risk on log compromise.
# Char-class is restricted to identifier-like characters (no spaces, no
# slashes, no leading dot) so the regex matches a single basename and
# stops at the first whitespace / path separator. Earlier version had
# space + slash in the class which made the match eat surrounding log
# context across multiple filenames.
_PDF_NAME = re.compile(r"\b[\w][\w.\-]*\.pdf\b", re.IGNORECASE)


def scrub(text: str) -> str:
    text = _E164.sub("[PHONE]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _GPS_PAIR.sub("[COORD]", text)
    text = _SLACK_UID.sub("[SLACK_UID]", text)
    text = _PDF_NAME.sub("[PDF]", text)
    text = _MSG_BODY.sub(lambda m: m.group(1) + "[BODY-REDACTED]", text)
    text = _TOOL_ARGS.sub(lambda m: m.group(1) + "[ARGS-REDACTED]" + m.group(3), text)
    return text


class ScrubbingFileHandler(logging.FileHandler):
    """FileHandler die het geformatteerde record scrubt vóór schrijven en
    de logfile met 0600 birth-mode aanmaakt.

    `delay=True` is bewust gezet: stream wordt pas geopend bij de eerste
    `emit()`, en dan via `open_secure` (umask 0o077 + open(...0o600)).
    """

    def __init__(self, filename, mode: str = "a", encoding: str | None = "utf-8") -> None:
        super().__init__(filename, mode=mode, encoding=encoding, delay=True)

    def _open(self):  # type: ignore[override]
        return open_secure(self.baseFilename, self.mode, encoding=self.encoding)

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class ScrubbingStreamHandler(logging.StreamHandler):
    """StreamHandler die scrubt vóór schrijven. Bedoeld voor stdout/stderr
    onder launchd, zodat `data/logs/stdout.log` (door launchd geredirect)
    niet onversleutelde PII bevat — een regressie t.o.v. de
    ScrubbingFileHandler die HIGH-1 uit review #1 oploste. ISO_AUDIT 2026-05
    CRITICAL-A.
    """

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))
