"""Sanity tests voor de generic rosa.plist template.

Doel: garanderen dat de template geen Hendrik-persoonlijke defaults
bevat, dat alle placeholders geldig zijn na substitutie, en dat het
XML parse't.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "scripts" / "rosa.plist.template"


def test_template_exists():
    assert TEMPLATE.is_file()


def test_template_has_no_hendrik_specific_defaults():
    body = TEMPLATE.read_text()
    for offender in [
        "com.hendrik.",
        "/Users/you",
        "/Users/hendrik",
        "digitalsignage-templates",
    ]:
        assert offender not in body, f"template still contains {offender!r}"


def test_template_uses_generic_label():
    body = TEMPLATE.read_text()
    assert "com.rosa.pa-agent" in body


def test_template_has_all_placeholders():
    body = TEMPLATE.read_text()
    for placeholder in ("{{PYTHON}}", "{{REPO_DIR}}", "{{ROSA_HOME}}"):
        assert placeholder in body, f"missing {placeholder}"


def test_substituted_template_is_valid_plist(tmp_path):
    body = TEMPLATE.read_text()
    substituted = (
        body
        .replace("{{PYTHON}}", "/home/user/venv/bin/python")
        .replace("{{REPO_DIR}}", "/home/user/rosa")
        .replace("{{ROSA_HOME}}", "/home/user/rosa-home")
    )
    parsed = plistlib.loads(substituted.encode())
    assert parsed["Label"] == "com.rosa.pa-agent"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"] == [
        "/home/user/venv/bin/python",
        "/home/user/rosa/src/main.py",
    ]
    assert parsed["WorkingDirectory"] == "/home/user/rosa"
    assert parsed["StandardOutPath"] == "/home/user/rosa-home/logs/stdout.log"
    assert parsed["EnvironmentVariables"]["ROSA_HOME"] == "/home/user/rosa-home"


def test_install_script_exists_and_executable():
    script = REPO_ROOT / "scripts" / "install_launchagent.sh"
    assert script.is_file()
    import os
    assert os.access(script, os.X_OK), "install_launchagent.sh not executable"


def test_install_script_uses_template():
    script = (REPO_ROOT / "scripts" / "install_launchagent.sh").read_text()
    assert "rosa.plist.template" in script
    assert "com.rosa.pa-agent" in script


def test_install_script_has_no_hendrik_hardcodes():
    body = (REPO_ROOT / "scripts" / "install_launchagent.sh").read_text()
    for offender in [
        "com.hendrik.", "/Users/you", "/Users/hendrik",
    ]:
        assert offender not in body
