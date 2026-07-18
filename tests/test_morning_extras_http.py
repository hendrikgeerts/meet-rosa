"""Unit tests voor extensions.morning_extras._http.fetch_with_retry."""
from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

from extensions.morning_extras._http import fetch_with_retry


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs=None, fp=None,  # type: ignore
    )


def _ok_response(payload: bytes = b"OK") -> MagicMock:
    """Mock urlopen context-manager dat `payload` retourneert."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = payload
    cm.__exit__.return_value = False
    return cm


def test_first_call_succeeds() -> None:
    with patch("urllib.request.urlopen", return_value=_ok_response(b"hello")):
        result = fetch_with_retry("http://x", retries=2)
    assert result == b"hello"


def test_5xx_retries_then_succeeds() -> None:
    """502 → 503 → 200."""
    seq = [_http_error(502), _http_error(503), _ok_response(b"hi")]

    def side_effect(*args, **kwargs):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    with patch("time.sleep"), patch(
        "urllib.request.urlopen", side_effect=side_effect,
    ):
        result = fetch_with_retry("http://x", retries=2, backoff=0.01)
    assert result == b"hi"


def test_4xx_returns_immediately_without_retry() -> None:
    """404 is permanent → 1 attempt only."""
    err = _http_error(404)
    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        raise err

    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = fetch_with_retry("http://x", retries=2)
    assert result is None
    assert call_count["n"] == 1  # geen retry op 404


def test_timeout_retries() -> None:
    seq = [TimeoutError("read timeout"), TimeoutError("read timeout"),
           _ok_response(b"recovered")]

    def side_effect(*args, **kwargs):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    with patch("time.sleep"), patch(
        "urllib.request.urlopen", side_effect=side_effect,
    ):
        result = fetch_with_retry("http://x", retries=2, backoff=0.01)
    assert result == b"recovered"


def test_all_retries_fail_returns_none() -> None:
    with patch("time.sleep"), patch(
        "urllib.request.urlopen", side_effect=_http_error(502),
    ):
        result = fetch_with_retry("http://x", retries=2, backoff=0.01)
    assert result is None


def test_backoff_schedule_exponential() -> None:
    """Backoff van 1s → 1s, 2s, 4s op opeenvolgende retries."""
    sleeps: list[float] = []
    with patch("time.sleep", side_effect=sleeps.append), patch(
        "urllib.request.urlopen", side_effect=_http_error(502),
    ):
        fetch_with_retry("http://x", retries=3, backoff=1.0)
    assert sleeps == [1.0, 2.0, 4.0]
