"""Tests voor user_profile loader + tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from extensions.user_profile.profile import (
    load_user_profile,
    render_for_prompt,
)
from extensions.user_profile.tools import (
    USER_PROFILE_HANDLERS,
    USER_PROFILE_TOOL_SCHEMAS,
)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=True)


# ---- loader ----------------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "nope.yaml"
    assert load_user_profile(p) == {}


def test_load_yaml_returns_dict(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {"name": "Hendrik", "expertise_areas": ["x"]})
    out = load_user_profile(p)
    assert out["name"] == "Hendrik"


def test_load_non_dict_yaml_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    p.write_text("- just a list\n- not a dict\n", encoding="utf-8")
    assert load_user_profile(p) == {}


# ---- render_for_prompt ----------------------------------------------

def test_render_empty_returns_empty() -> None:
    assert render_for_prompt({}) == ""


def test_render_includes_identity_and_companies() -> None:
    profile = {
        "name": "Hendrik", "role": "ISO",
        "companies": ["DST", "HGE"],
    }
    text = render_for_prompt(profile)
    assert "Hendrik" in text and "ISO" in text
    assert "DST" in text and "HGE" in text


def test_render_skips_empty_list_fields() -> None:
    profile = {"name": "X", "expertise_areas": []}
    text = render_for_prompt(profile)
    assert "Strengths" not in text


def test_render_caps_goals_at_five() -> None:
    profile = {"goals": [f"goal{i}" for i in range(20)]}
    text = render_for_prompt(profile)
    assert text.count("•") <= 5


# ---- update-tool ----------------------------------------------------

def test_update_appends_to_list_field(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {"expertise_areas": ["one"]})
    out = USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "expertise_areas", "value": "two", "action": "append",
    })
    assert out["ok"] is True
    after = load_user_profile(p)
    assert after["expertise_areas"] == ["one", "two"]


def test_update_append_dedups(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {"expertise_areas": ["one"]})
    USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "expertise_areas", "value": "one", "action": "append",
    })
    after = load_user_profile(p)
    assert after["expertise_areas"] == ["one"]


def test_update_remove_from_list(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {"goals": ["A", "B", "C"]})
    USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "goals", "value": "B", "action": "remove",
    })
    assert load_user_profile(p)["goals"] == ["A", "C"]


def test_update_scalar_set(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {})
    USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "working_style", "value": "direct, no-fluff",
    })
    assert load_user_profile(p)["working_style"] == "direct, no-fluff"


def test_update_set_on_list_field_rejected(tmp_path: Path) -> None:
    """H7 review-fix: action='set' op list-fields zou hele lijst wissen.
    Moet afgewezen worden met duidelijke foutmelding."""
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {"expertise_areas": ["original1", "original2"]})
    out = USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "expertise_areas", "value": "new", "action": "set",
    })
    assert "error" in out
    assert "append" in out["error"] or "remove" in out["error"]
    # Originele lijst onaangetast
    after = load_user_profile(p)
    assert after["expertise_areas"] == ["original1", "original2"]


def test_update_unknown_field_errors(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {})
    out = USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "made_up_field", "value": "x",
    })
    assert "error" in out


def test_update_empty_value_errors(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {})
    out = USER_PROFILE_HANDLERS["user_profile_update"](p, {
        "field": "name", "value": "  ",
    })
    assert "error" in out


def test_get_returns_dict(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    _write_yaml(p, {"name": "Hendrik"})
    out = USER_PROFILE_HANDLERS["user_profile_get"](p, {})
    assert out["name"] == "Hendrik"


def test_tool_schemas_have_required_names() -> None:
    names = {s["name"] for s in USER_PROFILE_TOOL_SCHEMAS}
    assert names == {"user_profile_get", "user_profile_update"}
