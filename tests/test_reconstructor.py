"""Tests for privacy.reconstructor — happy path, parens-variant, and the
hallucination-defense layer that strips leftover [CAT_NNN] tokens that
Claude invented (not present in mapping).
"""
from __future__ import annotations

from privacy.reconstructor import (
    ReconstructResult,
    reconstruct,
    reconstruct_with_info,
)


# --- basic mapping replacement ------------------------------------------

def test_replaces_exact_placeholders() -> None:
    text = "Hi [PERSON_001], your order at [ORG_001] is ready."
    mapping = {"[PERSON_001]": "Marc", "[ORG_001]": "Heineken"}
    assert reconstruct(text, mapping) == "Hi Marc, your order at Heineken is ready."


def test_handles_parens_bare_variant() -> None:
    """Claude soms paraphrases [PERSON_001] → (PERSON_001) — should also map."""
    text = "Confirmation from (PERSON_001) received."
    mapping = {"[PERSON_001]": "Marc"}
    assert reconstruct(text, mapping) == "Confirmation from Marc received."


def test_empty_mapping_returns_text_unchanged_when_no_placeholders() -> None:
    text = "Just some normal text."
    assert reconstruct(text, {}) == text


def test_longest_placeholder_first_no_partial_match() -> None:
    """[PERSON_10] must replace before [PERSON_1] eats the '1'."""
    text = "Meeting with [PERSON_10] and [PERSON_1]."
    mapping = {"[PERSON_1]": "Anne", "[PERSON_10]": "Marc"}
    assert reconstruct(text, mapping) == "Meeting with Marc and Anne."


# --- hallucination-defense (the actual fix) -----------------------------

def test_strips_leftover_person_to_someone() -> None:
    """Claude invented [PERSON_001] that the mapping doesn't know — replace
    with 'someone' rather than leaking the raw placeholder."""
    text = "Today's standup with [PERSON_001] at 09:30."
    result = reconstruct(text, {})  # empty mapping = everything is hallucinated
    assert "[PERSON_001]" not in result
    assert "someone" in result


def test_strips_leftover_url_to_link() -> None:
    """Hendrik's exact complaint: 'Meet: [URL_001]' must not reach iMessage."""
    text = "ontwikkelgesprek — Today 11:00–12:00 | Meet: [URL_001]"
    result = reconstruct(text, {})
    assert "[URL_001]" not in result
    assert "a link" in result


def test_strips_per_category() -> None:
    """Each placeholder-category has its own neutral fallback."""
    text = (
        "From [PERSON_001] at [ORG_001] — email [EMAIL_001], phone "
        "[PHONE_001], bank [IBAN_001], amount [AMOUNT_001], date "
        "[DATE_001], address [ADDRESS_001]."
    )
    result = reconstruct(text, {})
    # No raw placeholders remain
    import re
    assert re.search(r"\[[A-Z]+_\d+\]", result) is None
    # All eight fallbacks present
    for fb in ("someone", "an organization", "an email address",
               "a phone number", "a bank account", "an amount",
               "a date", "a location"):
        assert fb in result


def test_mapping_takes_precedence_over_fallback() -> None:
    """If a placeholder IS in mapping, use the real value — don't fall back."""
    text = "Hi [PERSON_001], standup with [PERSON_002] today."
    mapping = {"[PERSON_001]": "Marc"}  # only PERSON_001 known
    result = reconstruct(text, mapping)
    assert result == "Hi Marc, standup with someone today."


def test_strip_leftover_disabled_keeps_placeholders() -> None:
    """When strip_leftover=False, behaviour matches the pre-fix reconstruct."""
    text = "Hi [PERSON_001]."
    result = reconstruct(text, {}, strip_leftover=False)
    assert result == "Hi [PERSON_001]."


def test_reconstruct_with_info_reports_leftovers() -> None:
    """Caller (gateway) needs the count for audit-stream monitoring."""
    text = "Meeting [PERSON_001] at [URL_001] re [ORG_001]."
    mapping = {"[ORG_001]": "Heineken"}  # only ORG known
    info = reconstruct_with_info(text, mapping)
    assert isinstance(info, ReconstructResult)
    # Only PERSON_001 and URL_001 are leftovers; ORG_001 was in mapping
    assert sorted(info.leftovers) == ["PERSON", "URL"]
    assert "Heineken" in info.text
    assert "[PERSON_001]" not in info.text
    assert "[URL_001]" not in info.text


def test_reconstruct_with_info_empty_leftovers_when_all_known() -> None:
    text = "Hi [PERSON_001]."
    mapping = {"[PERSON_001]": "Marc"}
    info = reconstruct_with_info(text, mapping)
    assert info.leftovers == []
    assert info.text == "Hi Marc."


def test_no_false_positives_on_normal_brackets() -> None:
    """Square brackets in normal text (markdown links, JSON) must not be
    falsely matched. Our regex requires CAT_NNN format."""
    text = "See [docs](http://example.com) and [todo item] — [some text]."
    result = reconstruct(text, {})
    assert result == text  # unchanged


def test_unknown_category_does_not_crash() -> None:
    """If somehow a [FOO_001] sneaks through (unrecognized category), the
    regex shouldn't match it — `FOO` is not in our category-set."""
    text = "Mystery [FOO_001] token."
    result = reconstruct(text, {})
    assert "[FOO_001]" in result  # left alone, no false-positive replace


# --- H1: parens-bare leftover variant (Claude paraphrase) ---------------

def test_strips_leftover_parens_bare_variant() -> None:
    """Review-finding H1: Claude paraphraseert soms [URL_001] naar
    (URL_001). Voor hallucinated parens-bare placeholders moet de
    fallback óók grijpen, anders ontsnapt 'Meet: (URL_001)' alsnog naar
    Hendrik."""
    text = "Meet: (URL_001) — bel even"
    result = reconstruct(text, {})
    assert "(URL_001)" not in result
    assert "a link" in result


def test_strips_leftover_parens_bare_per_category() -> None:
    text = "Standup met (PERSON_001) en (ORG_001)."
    result = reconstruct(text, {})
    assert "(PERSON_001)" not in result
    assert "(ORG_001)" not in result
    assert "someone" in result
    assert "an organization" in result


def test_parens_bare_in_known_mapping_uses_real_value_not_fallback() -> None:
    """De bestaande mapping-laag handelt parens-variant van bekende
    entities al af. Mag niet regressie geven door de nieuwe fallback."""
    text = "Confirmation from (PERSON_001) received."
    mapping = {"[PERSON_001]": "Marc"}
    result = reconstruct(text, mapping)
    assert result == "Confirmation from Marc received."
    assert "someone" not in result  # fallback not triggered


# --- Bare-without-delimiters (Hendrik's "[A] PERSON_021 — 353d stil") ----

def test_strips_leftover_bare_without_brackets() -> None:
    """Hendrik's exact production-bug: Claude formatteerde
    `[A] [PERSON_021]` als `[A] PERSON_021` — de inner brackets
    weggevallen, mapping kent 'em niet (hallucinated, of bracket-strip
    dat lookup mismatched). Bare-detection moet alsnog grijpen."""
    text = "[A] PERSON_021 — 353d stil"
    result = reconstruct(text, {})
    assert "PERSON_021" not in result
    assert "someone" in result
    # Het bracket-met-letter-marker uit het VIP-format moet blijven
    assert "[A]" in result


def test_strips_bare_per_category() -> None:
    """Alle hallucinated bare-variants krijgen hun categorie-fallback."""
    text = "Standup PERSON_001 at URL_001 re ORG_001 amount AMOUNT_001."
    result = reconstruct(text, {})
    assert "PERSON_001" not in result
    assert "URL_001" not in result
    assert "ORG_001" not in result
    assert "AMOUNT_001" not in result
    assert "someone" in result
    assert "a link" in result
    assert "an organization" in result
    assert "an amount" in result


def test_bare_in_mapping_reconstructs_to_real_value() -> None:
    """Voor BEKENDE placeholders moet bare-variant ook reconstrueren naar
    de echte waarde (niet naar fallback)."""
    text = "[A] PERSON_021 — 353d stil"
    mapping = {"[PERSON_021]": "Anne van Heineken"}
    result = reconstruct(text, mapping)
    assert "Anne van Heineken" in result
    assert "PERSON_021" not in result
    assert "someone" not in result


def test_bare_word_boundary_prevents_substring_match() -> None:
    """Word-boundary regex moet false-positives in code-achtige tokens
    voorkomen. `my_PERSON_021_field` is een identifier — niet matchen."""
    text = "Code: my_PERSON_021_field is set."
    result = reconstruct(text, {})
    assert result == text  # geen match, geen verandering


def test_bare_longer_id_no_partial_match() -> None:
    """`PERSON_0211` mag niet stiekem `PERSON_021` matchen via shared prefix."""
    text = "PERSON_0211 en PERSON_021."
    result = reconstruct(text, {})
    # beide krijgen "someone" als category-fallback (PERSON_0211 ook bare)
    assert "PERSON_0211" not in result
    assert "PERSON_021" not in result
    assert result.count("someone") == 2


def test_bare_mixed_known_and_hallucinated() -> None:
    """Hendrik's gemixte scenario: Joost (known) + Anne (hallucinated)
    in dezelfde VIP-block."""
    text = (
        "[A] PERSON_021 — 353d stil\n"
        "[A] PERSON_022 — 353d stil\n"
        "[A] PERSON_023 — 302d stil"
    )
    mapping = {"[PERSON_023]": "Joost Geleijns"}
    result = reconstruct(text, mapping)
    # Joost is reconstructed (legit)
    assert "Joost Geleijns" in result
    # Andere twee → "someone" (geen mapping)
    assert "PERSON_021" not in result
    assert "PERSON_022" not in result
    assert result.count("someone") == 2
