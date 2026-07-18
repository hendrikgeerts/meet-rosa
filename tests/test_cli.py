"""Tests voor de rosa CLI (doctor, backup, restore, setup)."""
from __future__ import annotations

import json
import tarfile
from unittest.mock import patch

import pytest


@pytest.fixture
def rosa_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSA_HOME", str(tmp_path))
    monkeypatch.delenv("ROSA_DEV", raising=False)
    (tmp_path / "config.yaml").write_text(
        "user:\n  name: Test\nruntime:\n  claude_model: claude-sonnet-4-6\n"
    )
    (tmp_path / "config.yaml").chmod(0o600)
    (tmp_path / "secrets.env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-test-abcdef123456\n"
        "OWNER_IMESSAGE_HANDLE=+31600000000\n"
    )
    (tmp_path / "secrets.env").chmod(0o600)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memory.db").write_bytes(b"SQLite format 3\x00")
    yield tmp_path


# --- doctor ----------------------------------------------------------------


def test_doctor_collects_all_sections(rosa_home):
    from cli.doctor import collect_diagnostics
    diag = collect_diagnostics()
    assert diag["rosa_home"] == str(rosa_home)
    assert diag["is_configured"] is True
    assert diag["config_file"]["exists"] is True
    assert diag["config_file"]["perms"] == "0o600"
    assert diag["secrets"]["exists"] is True
    # Anthropic key masked
    assert diag["secrets"]["keys"]["ANTHROPIC_API_KEY"].startswith("sk-ant")
    assert "test-abc" not in json.dumps(diag)  # not leaked


def test_doctor_masks_short_secrets(rosa_home):
    """Even for shorter tokens, we should not print the full value."""
    (rosa_home / "secrets.env").write_text("TINY=xyz\n")
    from cli.doctor import collect_diagnostics
    diag = collect_diagnostics()
    assert diag["secrets"]["keys"]["TINY"] == "***"


def test_doctor_json_output_valid(rosa_home, capsys):
    from cli.doctor import main
    with patch("wizard.health_checks._http_json",
               return_value=(200, {"models": [{"name": "llama3.1:8b"}]})):
        rc = main(["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "rosa_home" in data


# --- backup / restore ------------------------------------------------------


def test_backup_writes_tarball(rosa_home, tmp_path):
    from cli.backup import main as backup_main
    out = tmp_path / "backup.tar.gz"
    rc = backup_main(["--out", str(out)])
    assert rc == 0
    assert out.exists()

    # Verify contents
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert "config.yaml" in names
    assert "secrets.env" in names
    assert "data/memory.db" in names


def test_backup_excludes_logs_by_default(rosa_home, tmp_path):
    (rosa_home / "logs").mkdir()
    (rosa_home / "logs" / "agent.log").write_text("log content")
    from cli.backup import main as backup_main
    out = tmp_path / "backup.tar.gz"
    backup_main(["--out", str(out)])
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert not any("logs" in n for n in names)


def test_backup_includes_logs_with_flag(rosa_home, tmp_path):
    (rosa_home / "logs").mkdir()
    (rosa_home / "logs" / "agent.log").write_text("log content")
    from cli.backup import main as backup_main
    out = tmp_path / "backup.tar.gz"
    backup_main(["--out", str(out), "--include-logs"])
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert any(n.startswith("logs/") for n in names)


def test_restore_dry_run_lists_entries(rosa_home, tmp_path):
    from cli.backup import main as backup_main
    from cli.restore import main as restore_main
    out = tmp_path / "backup.tar.gz"
    backup_main(["--out", str(out)])

    rc = restore_main([str(out), "--dry-run"])
    assert rc == 0


def test_restore_refuses_overwrite_without_force(rosa_home, tmp_path):
    from cli.backup import main as backup_main
    from cli.restore import main as restore_main
    out = tmp_path / "backup.tar.gz"
    backup_main(["--out", str(out)])
    rc = restore_main([str(out)])
    assert rc == 1


def test_restore_with_force_overwrites(rosa_home, tmp_path, monkeypatch):
    from cli.backup import main as backup_main
    from cli.restore import main as restore_main
    out = tmp_path / "backup.tar.gz"
    backup_main(["--out", str(out)])

    # Wijzig config zodat we kunnen zien dat restore werkte
    (rosa_home / "config.yaml").write_text("user:\n  name: DifferentUser\n")

    rc = restore_main([str(out), "--force"])
    assert rc == 0
    assert "Test" in (rosa_home / "config.yaml").read_text()


def test_restore_rejects_unsafe_paths(rosa_home, tmp_path):
    """Path-traversal defense: tar entries with .. must be rejected."""
    from cli.restore import main as restore_main
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo(name="../etc/passwd")
        info.size = 5
        import io
        tar.addfile(info, io.BytesIO(b"hello"))
    # Move existing config to make the "already exists" check pass
    (rosa_home / "config.yaml").unlink()
    rc = restore_main([str(evil)])
    assert rc == 1


# --- CLI dispatcher --------------------------------------------------------


def test_cli_help(capsys):
    from cli.main import main
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "doctor" in out
    assert "backup" in out
    assert "restore" in out


def test_cli_unknown_command(capsys):
    from cli.main import main
    rc = main(["nonexistent"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown command" in err.lower()
