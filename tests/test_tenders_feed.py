"""Tests voor extensions.tenders.feed — review-finding M4.

HTTP-laag is dunne urllib-wrapper; we mocken urlopen om foutpaden te
forceren:
- HTTP 4xx/5xx → TenderNedError
- HTTP 429 → TenderNedRateLimited met geparseerde Retry-After (M1)
- Malformed JSON → TenderNedError
- Lege/garbage payload → graceful default
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from extensions.tenders.feed import (
    TenderNedError,
    TenderNedRateLimited,
    _parse_retry_after,
    _request,
    fetch_publication_detail,
    fetch_recent_summaries,
    overview_url,
)


def _mock_urlopen_resp(body_bytes: bytes):
    """Mimic context-manager urlopen response."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: body_bytes))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _mock_http_error(code: int, headers: dict | None = None):
    return urllib.error.HTTPError(
        url="https://example", code=code, msg="bad",
        hdrs=headers or {}, fp=io.BytesIO(b""),
    )


# --- M1: Retry-After --------------------------------------------------

def test_parse_retry_after_valid_int() -> None:
    assert _parse_retry_after("60") == 60
    assert _parse_retry_after("3600") == 3600


def test_parse_retry_after_capped_at_one_hour() -> None:
    """Pathologische waarden (een dag, een jaar) cappen op 3600s."""
    assert _parse_retry_after("99999") == 3600


def test_parse_retry_after_negative_clamped_to_one() -> None:
    assert _parse_retry_after("-5") == 1


def test_parse_retry_after_missing_returns_default() -> None:
    assert _parse_retry_after(None) == 60
    assert _parse_retry_after("") == 60


def test_parse_retry_after_garbage_returns_default() -> None:
    """HTTP-date format wordt niet ondersteund — geven default 60s."""
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") == 60
    assert _parse_retry_after("soon") == 60


def test_request_raises_rate_limited_on_429() -> None:
    err = _mock_http_error(429, {"Retry-After": "120"})
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(TenderNedRateLimited) as exc_info:
            _request("https://test")
    assert exc_info.value.retry_after_seconds == 120


def test_request_raises_rate_limited_without_header() -> None:
    """429 zonder Retry-After header → default 60s."""
    err = _mock_http_error(429, {})
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(TenderNedRateLimited) as exc_info:
            _request("https://test")
    assert exc_info.value.retry_after_seconds == 60


# --- generic error paths ------------------------------------------------

def test_request_raises_on_500() -> None:
    err = _mock_http_error(500)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(TenderNedError) as exc_info:
            _request("https://test")
    assert "500" in str(exc_info.value)
    # Niet als rate-limited geclassificeerd
    assert not isinstance(exc_info.value, TenderNedRateLimited)


def test_request_raises_on_404() -> None:
    err = _mock_http_error(404)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(TenderNedError) as exc_info:
            _request("https://test")
    assert "404" in str(exc_info.value)


def test_request_raises_on_url_error() -> None:
    """DNS / connection-refused → URLError → TenderNedError."""
    with patch("urllib.request.urlopen",
                side_effect=urllib.error.URLError("DNS fail")):
        with pytest.raises(TenderNedError) as exc_info:
            _request("https://test")
    assert "URL error" in str(exc_info.value)


def test_request_raises_on_timeout() -> None:
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        with pytest.raises(TenderNedError) as exc_info:
            _request("https://test")
    assert "timeout" in str(exc_info.value)


def test_request_raises_on_malformed_json() -> None:
    """JSON-parse-fail → TenderNedError (geen verzonnen lege return)."""
    with patch("urllib.request.urlopen",
                return_value=_mock_urlopen_resp(b"not json {{{")):
        with pytest.raises(TenderNedError) as exc_info:
            _request("https://test")
    assert "JSON parse" in str(exc_info.value)


# --- happy paths --------------------------------------------------------

def test_request_returns_parsed_dict() -> None:
    body = json.dumps({"hello": "world"}).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=_mock_urlopen_resp(body)):
        result = _request("https://test")
    assert result == {"hello": "world"}


def test_fetch_recent_summaries_returns_content_list() -> None:
    body = json.dumps({"content": [{"publicatieId": 1}, {"publicatieId": 2}]}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen_resp(body)):
        out = fetch_recent_summaries(size=10)
    assert len(out) == 2
    assert out[0]["publicatieId"] == 1


def test_fetch_recent_summaries_handles_missing_content() -> None:
    """API returnt {} zonder 'content' veld → lege lijst, geen crash."""
    with patch("urllib.request.urlopen",
                return_value=_mock_urlopen_resp(b"{}")):
        out = fetch_recent_summaries()
    assert out == []


def test_fetch_recent_summaries_raises_on_non_dict_payload() -> None:
    """Als payload een list of string is, weet de feed niet wat te
    doen → expliciete TenderNedError."""
    with patch("urllib.request.urlopen",
                return_value=_mock_urlopen_resp(b'["wrong shape"]')), pytest.raises(TenderNedError):
        fetch_recent_summaries()


def test_fetch_publication_detail_returns_dict() -> None:
    body = json.dumps({"publicatieId": 419614, "aanbestedingNaam": "X"}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen_resp(body)):
        detail = fetch_publication_detail(419614)
    assert detail["publicatieId"] == 419614


def test_fetch_publication_detail_raises_on_non_dict() -> None:
    with patch("urllib.request.urlopen",
                return_value=_mock_urlopen_resp(b'"not a dict"')), pytest.raises(TenderNedError):
        fetch_publication_detail(1)


def test_overview_url_format() -> None:
    assert overview_url(419614) == "https://www.tenderned.nl/aankondigingen/overzicht/419614"
