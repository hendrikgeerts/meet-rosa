"""Tests voor privacy.preflight — laatste-regel sanity check op een
redacted payload voor egress."""
from __future__ import annotations

import pytest

from privacy.preflight import PreflightFailure, scan


def test_clean_text_passes() -> None:
    scan("Beste [PERSON_001], graag bel ik je morgen om [TIME_001].")  # geen exception


def test_iban_caught() -> None:
    with pytest.raises(PreflightFailure) as exc:
        scan("Even sturen naar NL91ABNA0417164300 graag.")
    assert exc.value.hit.category == "iban"


def test_email_caught() -> None:
    with pytest.raises(PreflightFailure) as exc:
        scan("Mail aan piet@klant.nl over de zaak.")
    assert exc.value.hit.category == "email"


def test_url_caught() -> None:
    with pytest.raises(PreflightFailure) as exc:
        scan("Klik op https://app.example.com/?token=abc om verder te gaan.")
    assert exc.value.hit.category == "url"


def test_first_match_wins() -> None:
    """Volgorde van categorieën in `_PATTERNS` (iban → email → url) bepaalt
    welke fout het eerst opduikt; we asserten alleen dat ÉR een failure is."""
    with pytest.raises(PreflightFailure):
        scan("NL91ABNA0417164300 + piet@klant.nl + https://x.nl")
