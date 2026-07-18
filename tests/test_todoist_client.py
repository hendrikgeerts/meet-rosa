"""Tests voor integrations.todoist._request error-handling.

Specifiek: H1 review-fix — error-body mag niet rauw in de log
verschijnen (CLAUDE.md "Log egress, not content"); alleen error_tag
+ bytes. Plus de TodoistProjectFullError-detectie."""
from __future__ import annotations

import json
import logging
import urllib.error
from io import BytesIO
from typing import Any
from unittest.mock import patch

import pytest

from integrations.todoist import (
    BASE,
    TodoistClient,
    TodoistProjectFullError,
)


def _http_error(code: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    """Construct een HTTPError met een leesbare body."""
    payload = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        url=f"{BASE}/tasks",
        code=code,
        msg="forbidden" if code == 403 else "error",
        hdrs={},  # type: ignore[arg-type]
        fp=BytesIO(payload),
    )


def test_max_items_limit_raises_project_full_error(caplog) -> None:
    """403 + error_tag=MAX_ITEMS_LIMIT_REACHED → custom exception."""
    client = TodoistClient("dummy-token")
    err = _http_error(403, {
        "error": "Maximum number of items per user project limit reached",
        "error_tag": "MAX_ITEMS_LIMIT_REACHED",
        "error_code": 49,
    })
    with patch("integrations.todoist.urllib.request.urlopen",
                side_effect=err), pytest.raises(TodoistProjectFullError):
        client._request("POST", "/tasks", body={"content": "x"})


def test_log_does_not_contain_raw_body_content(caplog) -> None:
    """H1 review-fix: rauwe body met user-content mag NIET in de log."""
    client = TodoistClient("dummy-token")
    sensitive = {
        "error": "validation failed",
        "error_tag": "FIELD_VALIDATION",
        "details": "Content 'Bel verzekeraar Anouk' is too long",
    }
    err = _http_error(400, sensitive)
    with patch("integrations.todoist.urllib.request.urlopen",
                side_effect=err), caplog.at_level(logging.WARNING, logger="integrations.todoist"):
        with pytest.raises(urllib.error.HTTPError):
            client._request("POST", "/tasks", body={"content": "x"})

    log_text = "\n".join(r.message for r in caplog.records)
    # Geen content uit body
    assert "Anouk" not in log_text
    assert "verzekeraar" not in log_text
    # Wel het error_tag (diagnose) + status + bytes-count
    assert "FIELD_VALIDATION" in log_text
    assert "HTTP 400" in log_text


def test_non_json_error_body_doesnt_crash(caplog) -> None:
    """Sommige Todoist-errors (5xx) geven HTML / plain-text terug —
    parse moet gracefully falen."""
    err = urllib.error.HTTPError(
        url=f"{BASE}/tasks", code=502, msg="bad gateway",
        hdrs={},  # type: ignore[arg-type]
        fp=BytesIO(b"<html>upstream timeout</html>"),
    )
    client = TodoistClient("dummy-token")
    with patch("integrations.todoist.urllib.request.urlopen",
                side_effect=err), caplog.at_level(logging.WARNING, logger="integrations.todoist"):
        with pytest.raises(urllib.error.HTTPError):
            client._request("GET", "/projects")
    log_text = "\n".join(r.message for r in caplog.records)
    assert "HTTP 502" in log_text
    # Geen rauwe HTML in log
    assert "upstream timeout" not in log_text
