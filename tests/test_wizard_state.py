"""Tests voor wizard.state — progress + config-persistence.

Focus:
  - WizardState.load/save roundtrip
  - is_finished() volgt REQUIRED_STEPS
  - update_config merget zonder eerdere data te droppen
  - save_secret behoudt bestaande sleutels + zet 0600 perms
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("yaml")

from wizard.state import (
    REQUIRED_STEPS,
    STEP_IDS,
    WizardState,
    load_config,
    load_secrets,
    save_config,
    save_secret,
    update_config,
)


def test_required_steps_are_subset_of_step_ids():
    assert set(REQUIRED_STEPS).issubset(set(STEP_IDS))


def test_load_missing_file_returns_empty(tmp_path):
    st = WizardState.load(tmp_path / "nope.json")
    assert st.completed == set() and st.skipped == set()


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    st = WizardState(completed={"welcome", "identity"}, skipped={"slack"})
    st.save(p)
    reloaded = WizardState.load(p)
    assert reloaded.completed == {"welcome", "identity"}
    assert reloaded.skipped == {"slack"}


def test_mark_done_clears_skipped(tmp_path):
    st = WizardState(completed=set(), skipped={"todoist"})
    st.mark_done("todoist")
    assert "todoist" in st.completed
    assert "todoist" not in st.skipped


def test_is_finished_only_when_all_required(tmp_path):
    st = WizardState(completed=set(REQUIRED_STEPS), skipped=set())
    assert st.is_finished() is True
    st.completed.discard("claude")
    assert st.is_finished() is False


def test_update_config_merges_nested(tmp_path):
    p = tmp_path / "config.yaml"
    update_config(p, {"user": {"name": "Alex", "email": "a@ex.com"}})
    update_config(p, {"user": {"timezone": "Europe/Berlin"}})
    cfg = load_config(p)
    assert cfg["user"]["name"] == "Alex"
    assert cfg["user"]["email"] == "a@ex.com"
    assert cfg["user"]["timezone"] == "Europe/Berlin"


def test_update_config_overwrites_non_dict(tmp_path):
    p = tmp_path / "config.yaml"
    save_config(p, {"features": {"slack": True}})
    update_config(p, {"features": {"slack": False, "todoist": True}})
    cfg = load_config(p)
    assert cfg["features"] == {"slack": False, "todoist": True}


def test_save_secret_creates_0600(tmp_path):
    p = tmp_path / "secrets.env"
    save_secret(p, "ANTHROPIC_API_KEY", "sk-ant-abc")
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600
    assert "sk-ant-abc" in p.read_text()


def test_save_secret_preserves_existing_keys(tmp_path):
    p = tmp_path / "secrets.env"
    save_secret(p, "KEY_A", "value-a")
    save_secret(p, "KEY_B", "value-b")
    secrets = load_secrets(p)
    assert secrets["KEY_A"] == "value-a"
    assert secrets["KEY_B"] == "value-b"


def test_save_secret_quotes_values_with_spaces(tmp_path):
    p = tmp_path / "secrets.env"
    save_secret(p, "K", "hello world")
    body = p.read_text()
    assert 'K="hello world"' in body
    # roundtrip via load_secrets strips the quotes
    assert load_secrets(p)["K"] == "hello world"
