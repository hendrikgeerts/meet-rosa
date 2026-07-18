"""Tests voor privacy.tool_redaction — recursive reconstruct + leak-detect."""
from __future__ import annotations

from privacy.tool_redaction import has_unresolved_placeholders, reconstruct_value

MAPPING = {
    "[PERSON_001]": "Piet",
    "[PERSON_002]": "Hendrik",
    "[EMAIL_001]": "piet@klant.nl",
    "[ORG_001]": "Heineken",
}


# --- reconstruct_value ----------------------------------------------------

def test_reconstruct_string() -> None:
    assert reconstruct_value("Schrijf [PERSON_001] over [ORG_001].", MAPPING) \
        == "Schrijf Piet over Heineken."


def test_reconstruct_list_of_strings() -> None:
    out = reconstruct_value(["[EMAIL_001]", "ander@x.nl"], MAPPING)
    assert out == ["piet@klant.nl", "ander@x.nl"]


def test_reconstruct_nested_dict() -> None:
    inp = {
        "to": "[EMAIL_001]",
        "subject": "Hi [PERSON_001]",
        "body": "Beste [PERSON_001],\n[PERSON_002]",
        "attendees": ["[EMAIL_001]"],
        "metadata": {"contact_name": "[PERSON_001]"},
    }
    out = reconstruct_value(inp, MAPPING)
    assert out == {
        "to": "piet@klant.nl",
        "subject": "Hi Piet",
        "body": "Beste Piet,\nHendrik",
        "attendees": ["piet@klant.nl"],
        "metadata": {"contact_name": "Piet"},
    }


def test_reconstruct_passes_non_strings_through() -> None:
    assert reconstruct_value(42, MAPPING) == 42
    assert reconstruct_value(True, MAPPING) is True
    assert reconstruct_value(None, MAPPING) is None


def test_reconstruct_longer_placeholder_first() -> None:
    """Edge case: [PERSON_10] mag niet kapot door [PERSON_1] eerst te
    matchen. Mapping bevat beide; sortering moet langste eerst doen."""
    mapping = {"[PERSON_1]": "Anna", "[PERSON_10]": "Lisa"}
    out = reconstruct_value("Met [PERSON_1] en [PERSON_10]", mapping)
    assert out == "Met Anna en Lisa"


def test_reconstruct_empty_mapping_is_noop() -> None:
    assert reconstruct_value("Hello [PERSON_001]", {}) == "Hello [PERSON_001]"


# --- has_unresolved_placeholders ------------------------------------------

def test_no_placeholder_returns_false() -> None:
    assert has_unresolved_placeholders("Plain text without brackets") is False
    assert has_unresolved_placeholders({"a": 1, "b": ["c"]}) is False


def test_simple_placeholder_detected() -> None:
    assert has_unresolved_placeholders("Send to [EMAIL_999]") is True


def test_placeholder_in_nested_dict_detected() -> None:
    inp = {"body": "Hi [PERSON_42]", "to": "real@example.com"}
    assert has_unresolved_placeholders(inp) is True


def test_placeholder_in_list_detected() -> None:
    assert has_unresolved_placeholders(["plain", "[ORG_007]"]) is True


def test_unrelated_brackets_not_flagged() -> None:
    """Markdown / code with brackets isn't a PII leak — only the strict
    [CAT_NNN] pattern triggers."""
    assert has_unresolved_placeholders("[markdown](http://x.nl)") is False
    assert has_unresolved_placeholders("array[0]") is False
    assert has_unresolved_placeholders("[lowercase_001]") is False
