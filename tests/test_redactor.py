"""Unit tests for privacy.redactor — regex + dictionary cascade + roundtrip."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from privacy.reconstructor import reconstruct
from privacy.redactor import Redactor, load_redactor_from_yaml


@pytest.fixture
def empty_redactor() -> Redactor:
    return Redactor()


@pytest.fixture
def vip_redactor() -> Redactor:
    return Redactor(
        vip_people=("Piet de Vries", "Piet"),
        vip_emails=("piet@klant.nl",),
        vip_orgs=("Heineken B.V.", "Heineken"),
        vip_projects=("DST-INC-2026-04",),
    )


# ---- regex-only tests -------------------------------------------------------

def test_email_redacted(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Mail aan piet@klant.nl over morgen.")
    assert "[EMAIL_001]" in r.text
    assert r.mapping["[EMAIL_001]"] == "piet@klant.nl"


def test_iban_redacted(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Boeking op NL91ABNA0417164300 graag.")
    assert "[IBAN_001]" in r.text
    assert r.mapping["[IBAN_001]"] == "NL91ABNA0417164300"


def test_url_redacted(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Login: https://app.example.com/?token=abc123 — daarna actie.")
    assert "[URL_001]" in r.text
    assert "token=abc123" in r.mapping["[URL_001]"]


def test_phone_dutch_e164(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Bel +31 6 12345678 als je vragen hebt.")
    assert any(k.startswith("[PHONE_") for k in r.mapping)


def test_amount_above_threshold_is_redacted(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Offerte: € 45.000 inclusief BTW.")
    assert any(k.startswith("[AMOUNT_") for k in r.mapping)
    placeholder = next(k for k in r.mapping if k.startswith("[AMOUNT_"))
    assert "45.000" in r.mapping[placeholder]


def test_amount_below_threshold_is_kept(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Lunch was € 35,50 per persoon.")
    assert "€ 35,50" in r.text
    assert not any(k.startswith("[AMOUNT_") for k in r.mapping)


# ---- BSN (Burgerservicenummer) — Cascade-3 -----------------------------

def test_bsn_valid_is_redacted(empty_redactor: Redactor) -> None:
    # 111222333 passes 11-proof: 9+8+7+12+10+8+9+6-3 = 66, mod 11 = 0
    r = empty_redactor.redact("BSN op contract: 111222333.")
    assert "[BSN_001]" in r.text
    assert r.mapping["[BSN_001]"] == "111222333"


def test_bsn_valid_dot_separated(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("BSN 111.222.333 graag.")
    assert any(k.startswith("[BSN_") for k in r.mapping)


def test_bsn_valid_space_separated(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("BSN 111 222 333 graag.")
    assert any(k.startswith("[BSN_") for k in r.mapping)


def test_bsn_mixed_separator_rejected(empty_redactor: Redactor) -> None:
    """Mixed separator '111.222 333' is geen reëel BSN-formaat —
    bewust geweigerd om false positives op losse 3-digit groepen te
    voorkomen."""
    r = empty_redactor.redact("ID 111.222 333 hier.")
    assert not any(k.startswith("[BSN_") for k in r.mapping)


def test_bsn_invalid_not_redacted_as_bsn(empty_redactor: Redactor) -> None:
    """123456789 faalt 11-proof — moet niet als BSN gemarkeerd (mag wel
    als phone door de telefoon-regex worden opgeslokt)."""
    r = empty_redactor.redact("Random 9-digit: 123456789.")
    assert not any(k.startswith("[BSN_") for k in r.mapping)


def test_bsn_zero_only_rejected(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Test 000000000 hier.")
    assert not any(k.startswith("[BSN_") for k in r.mapping)


# ---- Creditcard (Luhn) — Cascade-3 -------------------------------------

def test_cc_valid_visa_redacted(empty_redactor: Redactor) -> None:
    # Visa test number, Luhn-valid
    r = empty_redactor.redact("Card: 4111111111111111 voor abonnement.")
    assert any(k.startswith("[CC_") for k in r.mapping)
    placeholder = next(k for k in r.mapping if k.startswith("[CC_"))
    assert r.mapping[placeholder] == "4111111111111111"


def test_cc_valid_with_spaces_redacted(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Pin: 4111 1111 1111 1111 ok.")
    assert any(k.startswith("[CC_") for k in r.mapping)


def test_cc_valid_with_dashes_redacted(empty_redactor: Redactor) -> None:
    r = empty_redactor.redact("Card 4111-1111-1111-1111 noted.")
    assert any(k.startswith("[CC_") for k in r.mapping)


def test_cc_luhn_fail_not_redacted_as_cc(empty_redactor: Redactor) -> None:
    """1234567890123456 faalt Luhn — geen CC-placeholder."""
    r = empty_redactor.redact("Random 16-digit: 1234567890123456.")
    assert not any(k.startswith("[CC_") for k in r.mapping)


def test_cc_all_same_digit_rejected(empty_redactor: Redactor) -> None:
    """Sixteen 4's would technically satisfy Luhn but is obvious test
    junk — reject to avoid false positives on '0000000000000000'."""
    r = empty_redactor.redact("Test 4444444444444444 string.")
    assert not any(k.startswith("[CC_") for k in r.mapping)


def test_cc_amex_15_digit_redacted(empty_redactor: Redactor) -> None:
    # Amex test number 378282246310005, Luhn-valid, 15 digits
    r = empty_redactor.redact("Amex: 378282246310005 op file.")
    assert any(k.startswith("[CC_") for k in r.mapping)


# ---- dictionary tests -------------------------------------------------------

def test_vip_person_consistent(vip_redactor: Redactor) -> None:
    r = vip_redactor.redact("Piet belt morgen, Piet stuurt ook een mail.")
    placeholders = [k for k in r.mapping if k.startswith("[PERSON_")]
    assert len(placeholders) == 1, "same person should reuse placeholder"
    assert r.text.count(placeholders[0]) == 2


def test_vip_org_redacted_word_boundary(vip_redactor: Redactor) -> None:
    r = vip_redactor.redact("Heineken vraagt om uitbreiding van de offerte.")
    assert "[ORG_001]" in r.text
    assert r.mapping["[ORG_001]"] == "Heineken"


def test_dictionary_runs_before_regex(vip_redactor: Redactor) -> None:
    """If the email is in the VIP list AND matches the regex, dictionary
    should win so we don't double-allocate placeholders for the same value."""
    r = vip_redactor.redact("Schrijf piet@klant.nl een korte bevestiging.")
    placeholders = [k for k in r.mapping if k.startswith("[EMAIL_")]
    assert len(placeholders) == 1
    assert r.mapping[placeholders[0]] == "piet@klant.nl"


def test_existing_mapping_continues_numbering(vip_redactor: Redactor) -> None:
    """Cross-call coreference: same entity in turn 2 keeps placeholder from turn 1."""
    first = vip_redactor.redact("Piet stuurde een mail.")
    second = vip_redactor.redact(
        "Antwoord aan Piet morgen.",
        existing_mapping=first.mapping,
    )
    person_first = next(k for k, v in first.mapping.items() if v == "Piet")
    person_second = next(k for k, v in second.mapping.items() if v == "Piet")
    assert person_first == person_second


# ---- reconstructor + roundtrip ---------------------------------------------

def test_reconstruct_returns_original(vip_redactor: Redactor) -> None:
    src = "Heineken (contact: Piet, mail: piet@klant.nl, IBAN NL91ABNA0417164300) — €45.000."
    r = vip_redactor.redact(src)
    back = reconstruct(r.text, r.mapping)
    assert back == src


def test_reconstruct_handles_long_id_first() -> None:
    """Sort longest-first so [PERSON_10] isn't broken by [PERSON_1]'s replacement."""
    text = "answer involves [PERSON_1] and [PERSON_10]"
    mapping = {"[PERSON_1]": "Anna", "[PERSON_10]": "Lisa"}
    out = reconstruct(text, mapping)
    assert out == "answer involves Anna and Lisa"


# ---- yaml loader ------------------------------------------------------------

def test_load_from_yaml(tmp_path: Path) -> None:
    yml = tmp_path / "vip.yaml"
    yml.write_text(
        yaml.safe_dump({
            "people": [
                {
                    "name": "Piet de Vries",
                    "aliases": ["Piet", "P. de Vries"],
                    "emails": ["piet@klant.nl"],
                    "phones": ["+31612345678"],
                },
            ],
            "organizations": [
                {"name": "Heineken B.V.", "aliases": ["Heineken"], "domains": ["heineken.com"]},
            ],
            "projects": [
                {"code": "DST-INC-2026-04", "name": "Incident-onderzoek april 2026"},
            ],
        }),
        encoding="utf-8",
    )
    rd = load_redactor_from_yaml(vip_path=yml)
    r = rd.redact("Piet (Heineken) over DST-INC-2026-04, mail: piet@klant.nl, tel +31612345678.")
    originals = set(r.mapping.values())
    assert "Piet" in originals
    assert "Heineken" in originals
    assert "DST-INC-2026-04" in originals
    assert "piet@klant.nl" in originals
    assert reconstruct(r.text, r.mapping).startswith("Piet (Heineken)")


# --- regression: reconstructor handles Claude's syntax variants ----------

def test_reconstruct_keeps_outer_parens_around_placeholder() -> None:
    """`([PERSON_001])` blijft `(Michelle)` — buiten-parens NIET wegnemen."""
    out = reconstruct("Spreker ([PERSON_001]) komt morgen", {"[PERSON_001]": "Michelle"})
    assert "(Michelle)" in out


def test_reconstruct_handles_bare_parens_placeholder() -> None:
    """Claude schrijft soms (PERSON_001) — reconstructor moet die ook pakken."""
    out = reconstruct("Bevestiging van (PERSON_001) ontvangen", {"[PERSON_001]": "Michelle"})
    assert "(PERSON_001)" not in out
    assert "Michelle" in out


def test_reconstruct_matches_bare_token_in_narrative() -> None:
    """Sinds 30/5: bare-zonder-brackets WORDT vervangen.

    Achtergrond: Hendrik kreeg in zijn ochtend-briefing
    `[A] PERSON_021 — 353d stil` — Claude formatteerde
    `[A] [PERSON_021]` als `[A] PERSON_021` (inner brackets weg). Het
    oude defensieve gedrag liet dit doorslippen omdat 'Claude kon
    erover narratief schrijven'. In praktijk schrijft Claude in
    briefings/meeting-preps GEEN meta-narratief over zijn eigen
    placeholders — Hendrik's UX-bug is reëel, het theoretische
    false-positive-risico is niet. Word-boundary regex voorkomt
    substring-matches binnen code-identifiers (zie
    tests/test_reconstructor.py)."""
    out = reconstruct("er was geen PERSON_001 zichtbaar", {"[PERSON_001]": "Michelle"})
    assert "Michelle" in out
    assert "PERSON_001" not in out


# --- safe-terms whitelist (NER skip) -------------------------------------

def test_apply_ner_skips_safe_terms() -> None:
    """spaCy markeert 'Gilze' als GPE, maar safe_terms moet 'm laten staan."""
    from privacy.redactor import _apply_ner

    class FakeEnt:
        def __init__(self, text: str, label: str, start: int, end: int) -> None:
            self.text = text
            self.label_ = label
            self.start_char = start
            self.end_char = end

    class FakeDoc:
        def __init__(self, ents: list) -> None:
            self.ents = ents

    class FakeNlp:
        def __init__(self, ents: list) -> None:
            self._ents = ents
        def __call__(self, _: str) -> FakeDoc:
            return FakeDoc(self._ents)

    text = "Morgen 21 graden in Gilze, mooie dag"
    fake_nlp = FakeNlp([FakeEnt("Gilze", "GPE", 20, 25)])
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}

    def alloc(cat: str, orig: str) -> str:
        counters[cat] = counters.get(cat, 0) + 1
        ph = f"[{cat}_{counters[cat]:03d}]"
        mapping[ph] = orig
        return ph

    out = _apply_ner(text, fake_nlp, alloc,
                      safe_terms_lower=frozenset({"gilze"}))
    assert out == text  # niets verandered
    assert mapping == {}  # geen placeholder gemaakt


def test_apply_ner_redacts_when_not_in_safe_terms() -> None:
    """Tegenpool: zonder safe-terms wordt Gilze WEL geredacteerd."""
    from privacy.redactor import _apply_ner

    class FakeEnt:
        def __init__(self, text: str, label: str, start: int, end: int) -> None:
            self.text = text; self.label_ = label
            self.start_char = start; self.end_char = end
    class FakeDoc:
        def __init__(self, ents): self.ents = ents
    class FakeNlp:
        def __init__(self, ents): self._ents = ents
        def __call__(self, _): return FakeDoc(self._ents)

    text = "Morgen 21 graden in Gilze"
    fake_nlp = FakeNlp([FakeEnt("Gilze", "GPE", 20, 25)])
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}
    def alloc(cat: str, orig: str) -> str:
        counters[cat] = counters.get(cat, 0) + 1
        ph = f"[{cat}_{counters[cat]:03d}]"
        mapping[ph] = orig
        return ph

    out = _apply_ner(text, fake_nlp, alloc, safe_terms_lower=frozenset())
    assert "[ADDRESS_001]" in out
    assert mapping["[ADDRESS_001]"] == "Gilze"


def test_load_redactor_yaml_parses_safe_terms(tmp_path: Path) -> None:
    yaml_file = tmp_path / "vips.yaml"
    yaml_file.write_text(
        "safe_terms:\n"
        "  - Gilze\n"
        "  - Tilburg\n"
        "  - Nederland\n",
        encoding="utf-8",
    )
    r = load_redactor_from_yaml(vip_path=yaml_file)
    assert "gilze" in r._safe_terms_lower  # type: ignore[attr-defined]
    assert "tilburg" in r._safe_terms_lower  # type: ignore[attr-defined]
