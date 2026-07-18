"""Tests voor extensions.comm_intel.summarize — JSON-parser robustness +
end-to-end summarize() met een gefaketde Ollama."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from extensions.comm_intel.schema import CommItem
from extensions.comm_intel.summarize import (
    INTENTS, SENTIMENTS, Summary, _parse_loose_json, summarize,
)


# --- _parse_loose_json -----------------------------------------------------

def test_parses_clean_json() -> None:
    s = _parse_loose_json('{"summary":"x","intent":"task","sentiment":"urgent"}')
    assert s == Summary("x", "task", "urgent")


def test_parses_json_with_code_fence() -> None:
    raw = '```json\n{"summary":"x","intent":"fyi","sentiment":"neutral"}\n```'
    s = _parse_loose_json(raw)
    assert s.summary == "x" and s.intent == "fyi"


def test_parses_json_with_prefix_text() -> None:
    raw = 'Hier is de JSON:\n{"summary":"y","intent":"question","sentiment":"positive"}'
    s = _parse_loose_json(raw)
    assert s.intent == "question" and s.sentiment == "positive"


def test_invalid_intent_falls_back_to_other() -> None:
    raw = '{"summary":"x","intent":"verzonnen","sentiment":"neutral"}'
    assert _parse_loose_json(raw).intent == "other"


def test_invalid_sentiment_falls_back_to_neutral() -> None:
    raw = '{"summary":"x","intent":"task","sentiment":"euforisch"}'
    assert _parse_loose_json(raw).sentiment == "neutral"


def test_complete_garbage_returns_safe_fallback() -> None:
    s = _parse_loose_json("hello world, no json here at all")
    assert s.intent == "other" and s.sentiment == "neutral"
    assert "onleesbaar" in s.summary


def test_summary_truncated() -> None:
    long = "a" * 1000
    s = _parse_loose_json(f'{{"summary":"{long}","intent":"task","sentiment":"neutral"}}')
    assert len(s.summary) <= 500


# --- summarize() with fake Ollama ------------------------------------------

@dataclass
class _FakeBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _FakeResponse:
    content: list[Any] = field(default_factory=list)


@dataclass
class _FakeOllama:
    response_text: str = '{"summary":"Piet wil bellen","intent":"task","sentiment":"neutral"}'
    last_prompt: dict[str, Any] | None = None

    def chat(self, **kwargs: Any) -> _FakeResponse:
        self.last_prompt = kwargs
        return _FakeResponse(content=[_FakeBlock(text=self.response_text)])


def _item() -> CommItem:
    return CommItem(
        source="gmail", account="gmail", external_id="m1", direction="in",
        occurred_at=1, body_full="Hi Hendrik, bel je morgen even?",
        from_addr="piet@klant.nl", subject="Even bellen?",
    )


def test_summarize_round_trips_to_summary() -> None:
    s = summarize(_item(), _FakeOllama())
    assert s == Summary("Piet wil bellen", "task", "neutral")


def test_summarize_passes_metadata_to_ollama() -> None:
    fake = _FakeOllama()
    summarize(_item(), fake)
    user_msg = fake.last_prompt["messages"][0]["content"]
    assert "piet@klant.nl" in user_msg
    assert "Even bellen?" in user_msg
    assert "Hi Hendrik" in user_msg


def test_summarize_handles_empty_body() -> None:
    item = _item()
    item.body_full = "   "
    s = summarize(item, _FakeOllama())
    assert s.summary == "(leeg bericht)"
    assert s.intent == "other"


def test_summarize_handles_ollama_failure_with_emergency_excerpt() -> None:
    """Als Ollama kapot is: body-excerpt ipv unusable row."""
    class _Boom:
        def chat(self, **kwargs: Any) -> Any:
            raise RuntimeError("ollama down")
    s = summarize(_item(), _Boom())
    assert "auto-excerpt" in s.summary
    assert "Hi Hendrik" in s.summary   # fragment uit body_full
    assert s.intent == "other"


def test_parses_json_with_summary_as_list() -> None:
    """phi3:mini returns summary sometimes as ['zin 1', 'zin 2']."""
    raw = '{"summary":["Eerste zin.","Tweede zin."],"intent":"task","sentiment":"neutral"}'
    s = _parse_loose_json(raw)
    assert s.summary == "Eerste zin. Tweede zin."
    assert s.intent == "task"


# --- own outgoing invoice short-circuit ---------------------------------

def test_own_outgoing_invoice_marked_fyi() -> None:
    """Uitgaande factuur vanaf eigen domein → fyi (geen Llama-call)."""
    item = CommItem(
        source="gmail", account="gmail", external_id="m99", direction="out",
        occurred_at=1, body_full="Beste klant, hierbij uw factuur.",
        from_addr="invoice@digitalsignage-templates.com",
        subject="Factuur 2026-042 voor Klant X",
    )
    fake = _FakeOllama()
    s = summarize(item, fake,
                   own_email_domains=("digitalsignage-templates.com",))
    assert s.intent == "fyi"
    assert "Eigen uitgaande factuur" in s.summary
    # Geen Ollama-call gemaakt
    assert fake.last_prompt is None


def test_own_invoice_not_triggered_without_domain_match() -> None:
    """Mail van extern domein blijft normale flow gebruiken."""
    item = CommItem(
        source="gmail", account="gmail", external_id="m99", direction="in",
        occurred_at=1, body_full="Hi, hierbij onze factuur.",
        from_addr="invoice@externe-leverancier.com",
        subject="Factuur 12345",
    )
    fake = _FakeOllama()
    summarize(item, fake,
               own_email_domains=("digitalsignage-templates.com",))
    # Llama wel aangeroepen (normale flow)
    assert fake.last_prompt is not None


def test_own_invoice_subject_must_match() -> None:
    """Mail vanaf eigen domein zonder factuur-keyword → normale flow."""
    item = CommItem(
        source="gmail", account="gmail", external_id="m99", direction="out",
        occurred_at=1, body_full="Beste, hier je rapport.",
        from_addr="you@example.com",
        subject="Maandrapport april",
    )
    fake = _FakeOllama()
    summarize(item, fake,
               own_email_domains=("digitalsignage-templates.com",))
    assert fake.last_prompt is not None  # geen short-circuit
