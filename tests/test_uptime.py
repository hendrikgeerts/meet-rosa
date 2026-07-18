"""Tests voor extensions.uptime — schema, checker, worker, alerts."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from extensions.uptime.checker import check, load_targets
from extensions.uptime.schema import (
    CheckResult, get_target_state, init_uptime_schema, insert_event,
    list_targets_state, recent_events, record_check, set_silence,
    upsert_target,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "uptime.db"
    init_uptime_schema(p)
    return p


# --- schema basics ----------------------------------------------------

def test_upsert_target_creates_row(db: Path) -> None:
    with sqlite3.connect(db) as c:
        upsert_target(c, name="DST", url="https://dst.test/")
        state = get_target_state(c, name="DST")
    assert state is not None
    assert state["url"] == "https://dst.test/"
    assert state["consecutive_failures"] == 0
    assert state["is_down"] == 0


def test_upsert_idempotent_updates_url(db: Path) -> None:
    with sqlite3.connect(db) as c:
        upsert_target(c, name="DST", url="https://old.test/")
        upsert_target(c, name="DST", url="https://new.test/")
        state = get_target_state(c, name="DST")
    assert state["url"] == "https://new.test/"


def test_list_targets_state_sorted(db: Path) -> None:
    with sqlite3.connect(db) as c:
        upsert_target(c, name="Z", url="https://z.test")
        upsert_target(c, name="A", url="https://a.test")
        rows = list_targets_state(c)
    assert [r["name"] for r in rows] == ["A", "Z"]


# --- record_check semantics ------------------------------------------

def test_record_check_marks_down(db: Path) -> None:
    with sqlite3.connect(db) as c:
        upsert_target(c, name="DST", url="https://dst.test/")
        result = CheckResult(
            name="DST", url="https://dst.test/", ok=False,
            status_code=503, latency_ms=120,
            error="HTTP 503", checked_at=int(time.time()),
        )
        record_check(c, result=result, consecutive_failures=2,
                      is_down=True, down_since=int(time.time()) - 60)
        s = get_target_state(c, name="DST")
    assert s["consecutive_failures"] == 2
    assert s["is_down"] == 1
    assert s["last_status_code"] == 503


def test_record_check_marks_up_resets_counters(db: Path) -> None:
    now = int(time.time())
    with sqlite3.connect(db) as c:
        upsert_target(c, name="DST", url="https://dst.test/")
        result = CheckResult(
            name="DST", url="https://dst.test/", ok=True,
            status_code=200, latency_ms=85,
            error=None, checked_at=now,
        )
        record_check(c, result=result, consecutive_failures=0,
                      is_down=False, down_since=None)
        s = get_target_state(c, name="DST")
    assert s["consecutive_failures"] == 0
    assert s["is_down"] == 0
    assert s["down_since"] is None


def test_insert_event_appends_row(db: Path) -> None:
    with sqlite3.connect(db) as c:
        upsert_target(c, name="DST", url="https://dst.test/")
        insert_event(c, target_name="DST", kind="alert",
                     status_code=503, latency_ms=10000,
                     error="timeout")
        evs = recent_events(c, target_name="DST")
    assert len(evs) == 1
    assert evs[0]["kind"] == "alert"
    assert evs[0]["error"] == "timeout"


# --- silence -----------------------------------------------------------

def test_set_silence_then_clear(db: Path) -> None:
    with sqlite3.connect(db) as c:
        upsert_target(c, name="DST", url="https://dst.test/")
        future = int(time.time()) + 3600
        set_silence(c, name="DST", until=future)
        s = get_target_state(c, name="DST")
        assert s["silence_until"] == future
        set_silence(c, name="DST", until=None)
        s = get_target_state(c, name="DST")
        assert s["silence_until"] is None


# --- checker (HTTP) — mocking op _do_http_check niveau ----------------

def test_check_success() -> None:
    """Happy-path: status 200, geen expect_text → ok=True."""
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(200, None, b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.ok is True
    assert result.status_code == 200
    assert result.error is None
    assert result.retry_after is None


def test_check_wrong_status_is_fail() -> None:
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(503, "HTTP 503: Service Unavailable", b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.ok is False
    assert result.status_code == 503
    assert "HTTP 503" in (result.error or "")


def test_check_url_error_caught() -> None:
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(None, "URLError: dns lookup failed", b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.ok is False
    assert result.status_code is None
    assert "URLError" in (result.error or "")


def test_check_expect_text_match() -> None:
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(200, None, b"<html>welcome to DST</html>", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5,
                        expect_text="DST")
    assert result.ok is True


def test_check_expect_text_missing_is_fail() -> None:
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(200, None, b"<html>maintenance</html>", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5,
                        expect_text="DST")
    assert result.ok is False
    assert "expected_text" in (result.error or "")


# --- R1: Retry-After honoring ------------------------------------------

def test_check_captures_retry_after_from_503() -> None:
    """503 met Retry-After: 120 → CheckResult.retry_after = 120."""
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(503, "HTTP 503: Service Unavailable", b"", 120)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.retry_after == 120
    assert result.ok is False


def test_check_accepts_302_as_up_with_default_expected_status() -> None:
    """CMS-platforms redirecten root → /login. Met default
    expected_status=200 moet een 302 als 'up' tellen, niet 'down'.
    HTTPError wordt door urllib gegooid bij de no-redirect-handler;
    we moeten 'm clearen zodat de audit-trail eerlijk is."""
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(302, "HTTP 302: Found", b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.ok is True
    assert result.status_code == 302
    assert result.error is None  # cleared


def test_check_accepts_301_as_up() -> None:
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(301, "HTTP 301: Moved Permanently", b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.ok is True
    assert result.error is None


def test_check_204_with_explicit_expected_status_strict_match() -> None:
    """Als user expliciet een non-200 status_code wil (bv. 204 voor
    health-endpoints), match exact — 200 of 302 telt dan NIET als up."""
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(200, None, b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5,
                        expected_status=204)
    assert result.ok is False


def test_check_4xx_still_down() -> None:
    """404, 500, etc. blijven gewoon down ook met default expected_status."""
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(404, "HTTP 404: Not Found", b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.ok is False
    assert result.status_code == 404


def test_check_no_retry_after_when_absent() -> None:
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(500, "HTTP 500: Server Error", b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.retry_after is None


def test_parse_retry_after_seconds() -> None:
    from extensions.uptime.checker import _parse_retry_after
    class _H:
        def get(self, key): return "60"
    assert _parse_retry_after(_H()) == 60


def test_parse_retry_after_caps_at_one_hour() -> None:
    """Pathological Retry-After: 999999 → cap op 3600 om geen
    accidental hour-long silences te krijgen."""
    from extensions.uptime.checker import _parse_retry_after
    class _H:
        def get(self, key): return "999999"
    assert _parse_retry_after(_H()) == 3600


def test_parse_retry_after_invalid_returns_none() -> None:
    from extensions.uptime.checker import _parse_retry_after
    class _H:
        def get(self, key): return "not-a-number"
    assert _parse_retry_after(_H()) is None


# --- R6: error scrub + truncate ----------------------------------------

def test_check_error_truncated_to_200_chars() -> None:
    """Verbose error van een rebellious CMS mag het DB-veld niet
    laten overlopen met klantdata."""
    long_err = "HTTP 500: " + ("xxx " * 500)
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(500, long_err, b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.error is not None
    assert len(result.error) <= 200


def test_check_error_scrubs_email() -> None:
    """PII-leak preventie: e-mail in error → wordt door log_scrub vervangen."""
    err = "HTTP 500: failed to email client john@example.com about issue"
    with patch("extensions.uptime.checker._do_http_check",
                return_value=(500, err, b"", None)):
        result = check(name="DST", url="https://dst.test/", timeout_seconds=5)
    assert result.error is not None
    assert "john@example.com" not in result.error
    assert "[EMAIL]" in result.error


# --- load_targets (config) --------------------------------------------

def test_load_targets_parses_full_config(tmp_path: Path) -> None:
    yaml = tmp_path / "uptime.yaml"
    yaml.write_text(
        "targets:\n"
        "  - name: DST\n"
        "    url: https://dst.test/\n"
        "    expected_status: 200\n"
        "    check_interval_seconds: 30\n"
        "    fail_threshold: 3\n"
        "    alert_channels: [imessage, ntfy]\n"
    )
    out = load_targets(yaml)
    assert len(out) == 1
    t = out[0]
    assert t["name"] == "DST"
    assert t["check_interval_seconds"] == 30
    assert t["fail_threshold"] == 3
    assert t["alert_channels"] == ["imessage", "ntfy"]


def test_load_targets_defaults_when_minimal(tmp_path: Path) -> None:
    yaml = tmp_path / "uptime.yaml"
    yaml.write_text(
        "targets:\n"
        "  - name: DST\n"
        "    url: https://dst.test/\n"
    )
    out = load_targets(yaml)
    assert out[0]["check_interval_seconds"] == 60
    assert out[0]["fail_threshold"] == 2
    assert out[0]["alert_channels"] == ["imessage"]


def test_load_targets_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_targets(tmp_path / "nope.yaml") == []


def test_load_targets_skips_invalid(tmp_path: Path) -> None:
    yaml = tmp_path / "uptime.yaml"
    yaml.write_text(
        "targets:\n"
        "  - name: DST\n"          # geen url → skip
        "  - url: https://x.test/\n"  # geen name → skip
        "  - name: OK\n"
        "    url: https://ok.test/\n"
    )
    out = load_targets(yaml)
    assert [t["name"] for t in out] == ["OK"]


# --- alerts (format strings) ------------------------------------------

def test_dispatch_alert_sends_imessage_on_down() -> None:
    from extensions.uptime.alerts import dispatch_alert

    sent: list[tuple[str, str]] = []
    target = {"name": "DST", "url": "https://dst.test/",
              "alert_channels": ["imessage"]}
    result = CheckResult(
        name="DST", url="https://dst.test/", ok=False,
        status_code=503, latency_ms=120,
        error="HTTP 503", checked_at=int(time.time()),
    )
    body = dispatch_alert(
        target=target, result=result, duration_seconds=180,
        re_alert=False, kind="down", channels={"imessage"},
        send_imessage=lambda h, b: sent.append((h, b)),
        primary_handle="+316",
    )
    assert len(sent) == 1
    assert "DOWN" in body
    assert "DST" in body
    assert "HTTP 503" in body
    assert "3m 0s" in body or "3m" in body  # 180s humanized


def test_dispatch_alert_re_alert_prefix() -> None:
    from extensions.uptime.alerts import dispatch_alert

    sent: list[str] = []
    target = {"name": "DST", "url": "https://dst.test/"}
    result = CheckResult(
        name="DST", url="https://dst.test/", ok=False,
        status_code=None, latency_ms=10000,
        error="URLError: timed out", checked_at=int(time.time()),
    )
    body = dispatch_alert(
        target=target, result=result, duration_seconds=1200,
        re_alert=True, kind="down", channels={"imessage"},
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316",
    )
    assert "STILL DOWN" in body


def test_dispatch_alert_recovery_text() -> None:
    from extensions.uptime.alerts import dispatch_alert

    sent: list[str] = []
    target = {"name": "DST", "url": "https://dst.test/"}
    body = dispatch_alert(
        target=target, result=None, duration_seconds=600,
        re_alert=False, kind="recovery", channels={"imessage"},
        send_imessage=lambda h, b: sent.append(b),
        primary_handle="+316",
    )
    assert "RECOVERED" in body
    assert "10m" in body  # 600s humanized


# ---- review fix: wall-clock timeout does NOT block the worker ----------

def test_check_wall_clock_timeout_returns_quickly() -> None:
    """De ThreadPoolExecutor-bug van 11/6: bij TLS-hang bleef de
    `with`-block wachten op shutdown(wait=True). Met de fix gebruiken we
    een daemon-thread + join met timeout — moet binnen wall_timeout+1s
    terugkomen ongeacht of de underliggende call ooit voltooid.

    We forceren een hang door _do_http_check te monkey-patchen naar
    iets dat oneindig slaapt.
    """
    import time as _t
    from extensions.uptime import checker as _checker

    def _hanger(**kw):
        _t.sleep(60)  # zou 60s blokkeren als de fix er niet was
        return (None, None, b"", None)

    original = _checker._do_http_check
    _checker._do_http_check = _hanger
    try:
        started = _t.monotonic()
        result = _checker.check(
            name="hang-test", url="https://example.test/",
            timeout_seconds=1, expected_status=200,
        )
    finally:
        _checker._do_http_check = original

    elapsed = _t.monotonic() - started
    # wall_timeout = timeout_seconds + 2 = 3s; plus enige overhead.
    # Strikt: moet onder 5s zijn (dus geen 60s wachten).
    assert elapsed < 5.0, f"check took {elapsed:.1f}s — wall-clock timeout faalde"
    assert result.ok is False
    assert "wall-clock timeout" in (result.error or "")


# ---- review fix: escalation-laag voor lange downtime -------------------

def _seed_target_for_worker_test(db_path: Path, name: str = "DST") -> None:
    """Helper: maak een minimale uptime_checks-rij + init schema zodat
    get_target_state iets teruggeeft (M1 anti-storm guard checkt
    `escalated_at` op die rij)."""
    import sqlite3 as _sql
    from extensions.uptime.schema import init_uptime_schema, upsert_target
    init_uptime_schema(db_path)
    with _sql.connect(db_path, isolation_level=None) as conn:
        upsert_target(conn, name=name, url="https://x.test/")


def test_send_down_alert_escalates_after_threshold(tmp_path: Path) -> None:
    """Na overschrijden van escalate_after_seconds wordt 'ntfy'
    automatisch aan de channels toegevoegd, zelfs als target alleen
    'imessage' heeft."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "DST")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test-topic-xyz",
        escalate_after_seconds=600,
    )

    target = {
        "name": "DST", "url": "https://dst.test/",
        "expected_status": 200,
        "alert_channels": ["imessage"],
    }
    result = CheckResult(
        name="DST", url="https://dst.test/", ok=False,
        status_code=503, latency_ms=200,
        error="HTTP 503", checked_at=int(time.time()),
    )

    captured: dict = {}
    def _dispatch_capture(**kw):
        captured.update(kw)
        return "body"

    # 800s downtime (>= 600s threshold) → ntfy moet erbij
    with patch("extensions.uptime.worker.dispatch_alert",
                side_effect=_dispatch_capture):
        worker._send_down_alert(
            target, result=result, re_alert=False, duration_seconds=800,
        )
    assert "ntfy" in captured["channels"]
    assert "imessage" in captured["channels"]


def test_send_down_alert_no_escalation_before_threshold(tmp_path: Path) -> None:
    """Onder threshold blijft de channel-set zoals geconfigureerd."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "DST")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test-topic",
        escalate_after_seconds=600,
    )
    target = {
        "name": "DST", "url": "https://dst.test/",
        "expected_status": 200,
        "alert_channels": ["imessage"],
    }
    result = CheckResult(
        name="DST", url="https://dst.test/", ok=False,
        status_code=503, latency_ms=200,
        error="HTTP 503", checked_at=int(time.time()),
    )
    captured: dict = {}
    with patch("extensions.uptime.worker.dispatch_alert",
                side_effect=lambda **kw: captured.update(kw) or "body"):
        worker._send_down_alert(
            target, result=result, re_alert=False, duration_seconds=300,
        )
    assert "ntfy" not in captured["channels"]


def test_send_down_alert_no_escalation_without_topic(tmp_path: Path) -> None:
    """Zonder ntfy_topic geconfigureerd → geen escalatie ook al is
    duration ruim boven threshold. Voorkomt 'wel willen escaleren maar
    geen plek om naartoe te sturen'-foutje."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "DST")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic=None,  # NIET geconfigureerd
        escalate_after_seconds=600,
    )
    target = {"name": "DST", "url": "https://dst.test/",
               "expected_status": 200,
               "alert_channels": ["imessage"]}
    result = CheckResult(
        name="DST", url="https://dst.test/", ok=False, status_code=503,
        latency_ms=200, error="HTTP 503", checked_at=int(time.time()),
    )
    captured: dict = {}
    with patch("extensions.uptime.worker.dispatch_alert",
                side_effect=lambda **kw: captured.update(kw) or "body"):
        worker._send_down_alert(
            target, result=result, re_alert=False, duration_seconds=900,
        )
    assert "ntfy" not in captured["channels"]


def test_send_down_alert_per_target_escalation_override(tmp_path: Path) -> None:
    """Per-target `escalate_after_seconds` overschrijft de default —
    kritieke platforms kunnen sneller escaleren dan minder kritieke."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "Critical-Platform")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test",
        escalate_after_seconds=3600,  # default 1 uur
    )
    target = {
        "name": "Critical-Platform", "url": "https://x.test/",
        "expected_status": 200,
        "alert_channels": ["imessage"],
        "escalate_after_seconds": 120,  # override: 2 min
    }
    result = CheckResult(
        name="Critical-Platform", url="https://x.test/", ok=False,
        status_code=503, latency_ms=200,
        error="HTTP 503", checked_at=int(time.time()),
    )
    captured: dict = {}
    # 200s = onder 3600 default, boven 120 override → escalatie wel
    with patch("extensions.uptime.worker.dispatch_alert",
                side_effect=lambda **kw: captured.update(kw) or "body"):
        worker._send_down_alert(
            target, result=result, re_alert=False, duration_seconds=200,
        )
    assert "ntfy" in captured["channels"]


# ---- M1 fix: Ntfy-storm preventie via escalated_at ----------------------

def test_send_down_alert_escalates_only_once_per_incident(tmp_path: Path) -> None:
    """M1 — escalation moet één keer per incident vuren, niet bij elke
    re-alert. Anders krijgt Hendrik elke 15 min een Critical Ntfy."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "DST")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test-topic",
        escalate_after_seconds=600,
    )
    target = {"name": "DST", "url": "https://x.test/",
               "expected_status": 200, "alert_channels": ["imessage"]}
    result = CheckResult(
        name="DST", url="https://x.test/", ok=False, status_code=503,
        latency_ms=200, error="HTTP 503", checked_at=int(time.time()),
    )

    calls: list[set] = []
    def _capture(**kw):
        calls.append(kw["channels"])
        return "body"

    with patch("extensions.uptime.worker.dispatch_alert", side_effect=_capture):
        # Eerste re-alert na 700s — escalatie vuurt (ntfy erbij)
        worker._send_down_alert(target, result=result,
                                 re_alert=True, duration_seconds=700)
        # Tweede re-alert na 1500s — escalatie mag NIET opnieuw vuren
        worker._send_down_alert(target, result=result,
                                 re_alert=True, duration_seconds=1500)
        # Derde — idem
        worker._send_down_alert(target, result=result,
                                 re_alert=True, duration_seconds=2200)

    assert "ntfy" in calls[0], "eerste alert moet wel escaleren"
    assert "ntfy" not in calls[1], "tweede alert mag geen storm geven"
    assert "ntfy" not in calls[2], "derde alert mag geen storm geven"


def test_send_down_alert_re_escalates_after_recovery(tmp_path: Path) -> None:
    """Na recovery → escalated_at NULL → volgende incident kan opnieuw
    escaleren. Anders zou de allereerste outage de escalation voor het
    leven van het platform uitschakelen."""
    import sqlite3 as _sql
    import threading
    from extensions.uptime.worker import UptimeWorker
    from extensions.uptime.schema import record_check
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "DST")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test-topic",
        escalate_after_seconds=600,
    )
    target = {"name": "DST", "url": "https://x.test/",
               "expected_status": 200, "alert_channels": ["imessage"]}
    result_down = CheckResult(
        name="DST", url="https://x.test/", ok=False, status_code=503,
        latency_ms=200, error="HTTP 503", checked_at=int(time.time()),
    )
    result_up = CheckResult(
        name="DST", url="https://x.test/", ok=True, status_code=200,
        latency_ms=120, error=None, checked_at=int(time.time()),
    )

    calls: list[set] = []
    def _capture(**kw):
        calls.append(kw["channels"])
        return "body"

    with patch("extensions.uptime.worker.dispatch_alert", side_effect=_capture):
        # Incident 1: escalatie vuurt
        worker._send_down_alert(target, result=result_down,
                                 re_alert=False, duration_seconds=900)
        assert "ntfy" in calls[0]

        # Recovery simuleren via record_check is_down=False
        with _sql.connect(db_path, isolation_level=None) as conn:
            record_check(
                conn, result=result_up,
                consecutive_failures=0, is_down=False, down_since=None,
            )

        # Incident 2: escalatie moet opnieuw kunnen vuren
        worker._send_down_alert(target, result=result_down,
                                 re_alert=False, duration_seconds=900)
        assert "ntfy" in calls[1], "na recovery moet escalatie weer kunnen"


# ---- H2 fix: integration-test op _check_one tick-flow ------------------

def test_check_one_full_flow_escalates_on_long_outage(tmp_path: Path) -> None:
    """End-to-end op _check_one: target reeds down sinds 900s →
    nieuwe check fail → duration berekend → escalatie naar ntfy. Bewijst
    dat duration_seconds correct doorgaat naar de escalation-check."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from extensions.uptime.schema import upsert_target, init_uptime_schema
    from unittest.mock import patch
    import sqlite3 as _sql

    db_path = tmp_path / "x.db"
    init_uptime_schema(db_path)
    now = int(time.time())
    down_since = now - 900  # 15 min geleden begonnen
    with _sql.connect(db_path, isolation_level=None) as conn:
        upsert_target(conn, name="DST", url="https://x.test/")
        # Forceer is_down=1 + down_since zodat tick het als "blijft down" ziet
        conn.execute(
            "UPDATE uptime_checks SET is_down=1, down_since=?, "
            "consecutive_failures=5, last_alert_at=? WHERE name='DST'",
            (down_since, now - 1000),  # last alert zo oud dat re-alert fire't
        )

    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test-topic",
        escalate_after_seconds=600,
    )
    target = {
        "name": "DST", "url": "https://x.test/",
        "expected_status": 200, "alert_channels": ["imessage"],
        "check_interval_seconds": 60, "fail_threshold": 2,
        "re_alert_interval_seconds": 900, "timeout_seconds": 5,
    }

    captured: dict = {}
    def _capture(**kw):
        captured.update(kw)
        return "body"

    # Mock check() naar permanent-fail
    def _failing_check(**kw):
        return CheckResult(
            name="DST", url="https://x.test/", ok=False, status_code=503,
            latency_ms=200, error="HTTP 503", checked_at=now,
        )

    with patch("extensions.uptime.worker.check", side_effect=_failing_check), \
         patch("extensions.uptime.worker.dispatch_alert", side_effect=_capture):
        worker._check_one(target, now=now)

    # Belangrijkste assertie: integration → escalatie heeft 'ntfy' bereikt
    assert captured.get("channels") is not None, (
        "_check_one heeft dispatch_alert niet aangeroepen — re-alert path?"
    )
    assert "ntfy" in captured["channels"], (
        f"Expected ntfy in channels after 900s down, got: {captured['channels']}"
    )


# ---- M2 fix: check-thread is daemon (process exit safety) --------------

def test_check_uses_daemon_thread(tmp_path: Path) -> None:
    """De wall-clock-timeout-fix moet de underlying worker-thread als
    daemon=True markeren — anders blokkeert process-exit op een
    nooit-eindigende TLS-stall. Snapshot threads voor/na."""
    import threading as _th
    import time as _t
    from extensions.uptime import checker as _checker

    def _hanger(**kw):
        _t.sleep(30)
        return (None, None, b"", None)

    before = {t.ident for t in _th.enumerate()}
    original = _checker._do_http_check
    _checker._do_http_check = _hanger
    try:
        _checker.check(
            name="daemon-test", url="https://example.test/",
            timeout_seconds=1, expected_status=200,
        )
    finally:
        _checker._do_http_check = original

    new_threads = [
        t for t in _th.enumerate()
        if t.ident not in before and t.name.startswith("uptime-")
    ]
    # Mag zombie-thread bestaan, maar daemon=True dwingt process-exit-safety
    for t in new_threads:
        assert t.daemon, f"thread {t.name} is NIET daemon → blokkeert process exit"


# ---- L3 fix: escalate_after_seconds=0 disables ------------------------

def test_send_down_alert_escalation_disabled_when_zero(tmp_path: Path) -> None:
    """escalate_after_seconds=0 → escalation volledig uit, ook na uren
    downtime."""
    import threading
    from extensions.uptime.worker import UptimeWorker
    from unittest.mock import patch

    db_path = tmp_path / "x.db"
    _seed_target_for_worker_test(db_path, "DST")
    worker = UptimeWorker(
        db_path=db_path,
        config_path=tmp_path / "uptime.yaml",
        stop_event=threading.Event(),
        send_imessage=lambda h, t: None,
        primary_handle="test@me",
        ntfy_topic="test-topic",
        escalate_after_seconds=0,  # uit
    )
    target = {"name": "DST", "url": "https://x.test/",
               "expected_status": 200, "alert_channels": ["imessage"]}
    result = CheckResult(
        name="DST", url="https://x.test/", ok=False, status_code=503,
        latency_ms=200, error="HTTP 503", checked_at=int(time.time()),
    )
    captured: dict = {}
    with patch("extensions.uptime.worker.dispatch_alert",
                side_effect=lambda **kw: captured.update(kw) or "body"):
        worker._send_down_alert(
            target, result=result, re_alert=False, duration_seconds=99999,
        )
    assert "ntfy" not in captured["channels"]
