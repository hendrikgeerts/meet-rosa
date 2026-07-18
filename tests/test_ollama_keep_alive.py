"""Test voor _normalize_keep_alive coerce-laag.

Productie-bug 30/4 - 4/6 2026: `keep_alive="-1"` (string) werd door
nieuwere Ollama-versies geweigerd met `time: missing unit in duration
"-1"`. Effect: alle summarize-calls faalden → intent-classifier viel
terug op 'other' → geen task/question labels → geen open_loops
gegenereerd voor 5 weken.

De fix is een normaliserende coerce-laag in OllamaClient.__init__.
Deze tests beschermen tegen regressie.
"""
from __future__ import annotations

from models.ollama import OllamaClient, _normalize_keep_alive


def test_int_negative_one_passes_through() -> None:
    """Integer -1 is wat Ollama wil — pass through."""
    assert _normalize_keep_alive(-1) == -1


def test_string_negative_one_coerced_to_int() -> None:
    """De rauwe bug: '-1' string werd 400'd door Ollama."""
    assert _normalize_keep_alive("-1") == -1
    assert isinstance(_normalize_keep_alive("-1"), int)


def test_string_negative_one_with_whitespace() -> None:
    assert _normalize_keep_alive(" -1 ") == -1


def test_minus_one_seconds_also_coerced() -> None:
    """`-1s` is ook een soms-geziene variant; map naar int."""
    assert _normalize_keep_alive("-1s") == -1


def test_duration_strings_pass_through() -> None:
    """Geldige duration-strings ('30m', '1h', '24h') worden niet aangeraakt."""
    for d in ("30m", "1h", "24h", "5m", "0s"):
        assert _normalize_keep_alive(d) == d


def test_positive_int_passes_through() -> None:
    """Een gewone integer (Ollama interpreteert als seconden) passeert."""
    assert _normalize_keep_alive(300) == 300
    assert _normalize_keep_alive(0) == 0


def test_unknown_string_passes_through() -> None:
    """Onbekende strings laten we Ollama afkeuren (transparant); we
    voegen geen verzonnen defaults toe."""
    assert _normalize_keep_alive("invalid-format") == "invalid-format"


def test_ollama_client_init_coerces_string_negative_one() -> None:
    """Echte client-construction: stringvorm wordt int -1 in body-payload."""
    client = OllamaClient(model="llama3.1:8b", keep_alive="-1")
    assert client._keep_alive == -1


def test_ollama_client_init_passes_through_duration() -> None:
    client = OllamaClient(model="llama3.1:8b", keep_alive="30m")
    assert client._keep_alive == "30m"


def test_ollama_client_default_unchanged() -> None:
    """Default '30m' moet ongewijzigd blijven na de coerce-laag."""
    client = OllamaClient(model="llama3.1:8b")
    assert client._keep_alive == "30m"
