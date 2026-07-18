"""Tests voor wizard.bootstrap — guards en flow.

Deze tests draaien uvicorn NIET. Ze verifieren dat:

  1. ROSA_DEV=1 → skip zonder de wizard te importeren.
  2. ROSA_WIZARD_DISABLED=1 → skip.
  3. is_configured() True → skip.
  4. Anders → we roepen build_app() aan.

Hendrik's live daemon vertrouwt op de ROSA_DEV=1 guard — als die test
faalt, is de agent kapot voor hem.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_dev_mode_skips_wizard(monkeypatch, tmp_path):
    monkeypatch.setenv("ROSA_DEV", "1")
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))

    from wizard import bootstrap
    with patch.object(bootstrap, "_run_uvicorn") as run_uvicorn:
        bootstrap.ensure_configured()
    run_uvicorn.assert_not_called()


def test_disabled_env_skips_wizard(monkeypatch, tmp_path):
    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.setenv("ROSA_WIZARD_DISABLED", "1")
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))

    from wizard import bootstrap
    with patch.object(bootstrap, "_run_uvicorn") as run_uvicorn:
        bootstrap.ensure_configured()
    run_uvicorn.assert_not_called()


def test_already_configured_skips_wizard(monkeypatch, tmp_path):
    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.delenv("ROSA_WIZARD_DISABLED", raising=False)
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    # Fake config-file zodat is_configured() True is.
    (tmp_path / "config.yaml").write_text("user:\n  name: Alex\n")

    from wizard import bootstrap
    with patch.object(bootstrap, "_run_uvicorn") as run_uvicorn:
        bootstrap.ensure_configured()
    run_uvicorn.assert_not_called()


def test_needs_wizard_starts_uvicorn(monkeypatch, tmp_path):
    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.delenv("ROSA_WIZARD_DISABLED", raising=False)
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))

    # Pak een vrije port (default 8765 kan al in gebruik zijn).
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()

    # Fake wait_until_finished zodat we niet echt blocken op user-input.
    from wizard import bootstrap
    fake_wait = MagicMock(return_value=True)
    fake_reset = MagicMock()
    with patch.object(bootstrap, "_run_uvicorn") as run_uvicorn, \
         patch("wizard.server.wait_until_finished", fake_wait), \
         patch("wizard.server.reset_finish_event", fake_reset):
        bootstrap.ensure_configured(port=free_port)

    run_uvicorn.assert_called_once()
    fake_reset.assert_called_once()
    fake_wait.assert_called_once()


def test_port_in_use_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.delenv("ROSA_WIZARD_DISABLED", raising=False)
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))

    import socket

    from wizard import bootstrap

    # Bezet een port en probeer de bootstrap tegen exact die port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    try:
        with pytest.raises(SystemExit) as exc:
            bootstrap.ensure_configured(port=port)
        assert "port" in str(exc.value).lower()
    finally:
        sock.close()


def test_wizard_timeout_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ROSA_DEV", raising=False)
    monkeypatch.delenv("ROSA_WIZARD_DISABLED", raising=False)
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))

    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()

    from wizard import bootstrap
    with patch.object(bootstrap, "_run_uvicorn"), \
         patch("wizard.server.wait_until_finished", return_value=False), \
         patch("wizard.server.reset_finish_event"):
        with pytest.raises(SystemExit) as exc:
            bootstrap.ensure_configured(wait_timeout=0.1, port=free_port)
        assert "timed out" in str(exc.value).lower()
