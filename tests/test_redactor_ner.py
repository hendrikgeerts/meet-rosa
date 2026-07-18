"""Tests voor de spaCy-NER cascade-laag in privacy.redactor.

Skipped automatisch als spaCy of het Nederlandse model niet beschikbaar is —
zo werkt de suite ook op machines zonder de ~50 MB download (CI o.i.d.).
"""
from __future__ import annotations

import pytest

from privacy.redactor import Redactor

NER_MODEL = "nl_core_news_md"


def _spacy_can_load(model: str) -> bool:
    try:
        import spacy
        spacy.load(model)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _spacy_can_load(NER_MODEL),
    reason=f"spaCy or {NER_MODEL} not installed",
)


# --- core NER tests --------------------------------------------------------

def test_ner_catches_person_without_dictionary() -> None:
    r = Redactor(ner_model=NER_MODEL)
    res = r.redact("Mark Jansen kwam langs vandaag.")
    assert "Mark Jansen" not in res.text
    assert any(v == "Mark Jansen" for v in res.mapping.values())


def test_ner_skips_when_model_not_loaded() -> None:
    """Geen ner_model → geen NER laag, alleen dict+regex (back-compat)."""
    r = Redactor()  # ner_model defaults to None
    res = r.redact("Mark Jansen kwam langs.")
    # NER off → name survives (would only be caught by NER)
    assert "Mark Jansen" in res.text


def test_ner_idempotent_on_already_redacted_text() -> None:
    """Tweede call op gereduceerde tekst — placeholders mogen niet opnieuw
    door NER worden vervangen."""
    r = Redactor(ner_model=NER_MODEL)
    first = r.redact("Anouk Vermeulen kwam langs.")
    second = r.redact(first.text, existing_mapping=first.mapping)
    # Mapping should be stable (no nieuwe placeholder voor de oude placeholder)
    assert first.mapping == second.mapping
    assert second.text == first.text


def test_ner_respects_dictionary_layer() -> None:
    """Dictionary draait eerst; NER zou daarna geen nieuw PERSON moeten
    detecteren in een al-vervangen placeholder."""
    r = Redactor(vip_people=("Mark Jansen",), ner_model=NER_MODEL)
    res = r.redact("Mark Jansen sprak met Anouk Vermeulen.")
    # Both should be redacted — Mark via dict, Anouk via NER
    assert "Mark Jansen" not in res.text
    assert "Anouk Vermeulen" not in res.text
    originals = set(res.mapping.values())
    assert "Mark Jansen" in originals
    assert "Anouk Vermeulen" in originals
    # Mark only once (no double-allocation)
    mark_phs = [k for k, v in res.mapping.items() if v == "Mark Jansen"]
    assert len(mark_phs) == 1


def test_ner_continues_mapping_across_calls() -> None:
    r = Redactor(ner_model=NER_MODEL)
    first = r.redact("Mark Jansen belt.")
    mark_ph_first = next(k for k, v in first.mapping.items() if v == "Mark Jansen")

    second = r.redact("Mark Jansen mailde nog.", existing_mapping=first.mapping)
    mark_ph_second = next(k for k, v in second.mapping.items() if v == "Mark Jansen")
    assert mark_ph_first == mark_ph_second
