"""Tests for privacy.gateway — pass-through behaviour + audit emission.

Gateway constructs a real ClaudeClient (which needs the anthropic SDK), so
we monkeypatch ClaudeClient itself with a fake before importing-via-Gateway,
or — simpler — use Gateway.__init__ to install our fake post-construction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.audit import AuditLogger
from privacy.classifier import Classification, Classifier
from privacy.gateway import Gateway
from privacy.redactor import Redactor


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _FakeResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _FakeUsage = field(default_factory=_FakeUsage)


@dataclass
class _FakeClaude:
    model: str = "claude-fake-1"
    last_call: dict[str, Any] | None = None

    def reply(self, **kwargs: Any) -> _FakeResponse:
        self.last_call = kwargs
        return _FakeResponse()


def _build_gateway(
    tmp_path: Path,
    *,
    classifier: Classifier | None = None,
    redactor: Redactor | None = None,
    local_client: Any | None = None,
) -> tuple[Gateway, _FakeClaude, AuditLogger]:
    audit = AuditLogger(tmp_path)
    gw = Gateway(
        api_key="dummy", model="claude-fake-1", audit=audit,
        classifier=classifier, redactor=redactor, local_client=local_client,
    )
    fake = _FakeClaude(model="claude-fake-1")
    gw._claude = fake  # type: ignore[assignment]   # test-only injection
    return gw, fake, audit


@dataclass
class _FakeLocalUsage:
    input_tokens: int = 200
    output_tokens: int = 60


@dataclass
class _FakeLocalResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _FakeLocalUsage = field(default_factory=_FakeLocalUsage)


@dataclass
class _FakeLocal:
    model: str = "llama-fake"
    last_call: dict[str, Any] | None = None
    def chat(self, **kwargs: Any) -> _FakeLocalResponse:
        self.last_call = kwargs
        return _FakeLocalResponse()


def _read_today(tmp_path: Path) -> list[dict[str, Any]]:
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d")
    file = tmp_path / f"egress-{today}.jsonl"
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]


def test_gateway_forwards_to_claude_and_returns_response(tmp_path: Path) -> None:
    gw, fake, _ = _build_gateway(tmp_path)
    resp = gw.complete(
        task="morning_briefing",
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=512,
    )
    assert resp.stop_reason == "end_turn"
    assert fake.last_call == {
        "system": "be brief",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": None,
        "max_tokens": 512,
    }


def test_gateway_writes_audit_record(tmp_path: Path) -> None:
    gw, _, _ = _build_gateway(tmp_path)
    gw.complete(
        task="tool_use_turn",
        system="x",
        messages=[{"role": "user", "content": "y"}],
        tools=[{"name": "t1"}, {"name": "t2"}],
    )
    records = _read_today(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "claude_call"
    assert rec["task"] == "tool_use_turn"
    assert rec["label"] == "tool_use"   # tool-loops always go to Claude, label='tool_use'
    assert rec["model"] == "claude-fake-1"
    assert rec["stop_reason"] == "end_turn"
    assert rec["input_tokens"] == 100
    assert rec["output_tokens"] == 50
    assert rec["tools_offered"] == 2


def test_audit_does_not_leak_payload(tmp_path: Path) -> None:
    """Even if someone passes a juicy system prompt, audit must not record it."""
    gw, _, _ = _build_gateway(tmp_path)
    gw.complete(
        task="x",
        system="SECRET prompt with passwords",
        messages=[{"role": "user", "content": "leaky body"}],
    )
    raw = (tmp_path / sorted(p.name for p in tmp_path.iterdir())[0]).read_text(encoding="utf-8")
    assert "SECRET" not in raw
    assert "leaky" not in raw
    assert "passwords" not in raw


# --- routing tests ---------------------------------------------------------

def test_confidential_routes_to_local(tmp_path: Path) -> None:
    classifier = Classifier(
        confidential_keywords=("vertrouwelijk",),
        default_label="internal",
    )
    local = _FakeLocal()
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, local_client=local)

    resp = gw.complete(
        task="briefing",
        system="be brief",
        messages=[{"role": "user", "content": "Strikt vertrouwelijk: bekijk dit."}],
    )
    assert isinstance(resp, _FakeLocalResponse)
    assert local.last_call is not None
    assert fake_claude.last_call is None  # Claude was NIET aangeroepen

    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "local_call"
    assert rec["label"] == "confidential"
    assert rec["model"] == "llama-fake"
    assert rec["classifier_reason"] == "keyword_match"


def test_internal_label_still_uses_claude(tmp_path: Path) -> None:
    classifier = Classifier(default_label="internal")
    local = _FakeLocal()
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, local_client=local)

    gw.complete(task="t", system="s", messages=[{"role": "user", "content": "iets gewoons"}])
    assert fake_claude.last_call is not None
    assert local.last_call is None

    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "claude_call"
    assert rec["label"] == "internal"
    assert rec["classifier_reason"] == "default"


def test_force_label_public_bypasses_classifier_and_redactor(tmp_path: Path) -> None:
    """force_label='public' → no classify, no redact, raw to Claude.
    Use case: market-intel digest van publieke RSS-headlines."""
    from privacy.redactor import Redactor
    classifier = Classifier(
        confidential_keywords=("salaris",),  # zou normaal triggeren
        default_label="internal",
    )
    redactor = Redactor()  # also wired
    gw, fake_claude, _ = _build_gateway(
        tmp_path, classifier=classifier, redactor=redactor,
    )

    payload = "Headlines: salaris bij big tech AI engineers stijgt 20%"
    gw.complete(
        task="market_intel_score",   # in _FORCE_PUBLIC_ALLOWED_TASKS
        system="news synth",
        messages=[{"role": "user", "content": payload}],
        force_label="public",
    )
    assert fake_claude.last_call is not None
    # Geen redactie: het systeem + messages komen ongewijzigd binnen.
    sent_user = fake_claude.last_call["messages"][0]["content"]
    assert sent_user == payload  # exact dezelfde string, dus geen redact

    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "claude_call"
    assert rec["label"] == "public_forced"


def test_force_label_public_rejected_for_unlisted_task(tmp_path: Path) -> None:
    """Audit P-2 (28/6): force_label='public' is een whitelist; tasks
    buiten _FORCE_PUBLIC_ALLOWED_TASKS vallen terug op classifier zodat
    een caller niet per ongeluk redactor kan skippen."""
    classifier = Classifier(
        confidential_keywords=("salaris",),
        default_label="internal",
    )
    redactor = Redactor()
    gw, fake_claude, _ = _build_gateway(
        tmp_path, classifier=classifier, redactor=redactor,
    )
    payload = "Salaris bij Hendrik is X"
    gw.complete(
        task="random_unlisted_task",
        system="something",
        messages=[{"role": "user", "content": payload}],
        force_label="public",
    )
    rec = _read_today(tmp_path)[0]
    # Niet als 'public_forced' gelabeld — de fallback heeft 'em via
    # de normale internal-route door de redactor gestuurd.
    assert rec["label"] != "public_forced"


def test_force_label_confidential_routes_to_local(tmp_path: Path) -> None:
    classifier = Classifier(default_label="internal")  # zou Claude kiezen
    local = _FakeLocal()
    gw, fake_claude, _ = _build_gateway(
        tmp_path, classifier=classifier, local_client=local,
    )

    gw.complete(
        task="t", system="s",
        messages=[{"role": "user", "content": "iets neutraals"}],
        force_label="confidential",
    )
    assert local.last_call is not None
    assert fake_claude.last_call is None


def test_force_label_confidential_without_local_raises(tmp_path: Path) -> None:
    """Geen local_client + force confidential → raise (vs. silent fallback
    bij classifier-route). Caller heeft expliciet gevraagd om local."""
    import pytest as _pytest
    classifier = Classifier(default_label="internal")
    gw, _, _ = _build_gateway(tmp_path, classifier=classifier)
    with _pytest.raises(RuntimeError, match="force_label='confidential'"):
        gw.complete(
            task="t", system="s",
            messages=[{"role": "user", "content": "x"}],
            force_label="confidential",
        )


def test_tool_use_skips_classifier_and_goes_to_claude(tmp_path: Path) -> None:
    """Even bij confidential keywords gaat tool_use altijd naar Claude
    omdat Ollama geen tool-use heeft. label='tool_use' in audit."""
    classifier = Classifier(confidential_keywords=("vertrouwelijk",))
    local = _FakeLocal()
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, local_client=local)

    gw.complete(
        task="tool_use_turn",
        system="s",
        messages=[{"role": "user", "content": "vertrouwelijk: tool me iets"}],
        tools=[{"name": "x"}],
    )
    assert fake_claude.last_call is not None
    assert local.last_call is None

    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "claude_call"
    assert rec["label"] == "tool_use"


def test_confidential_without_local_falls_back_to_claude_with_warning(tmp_path: Path) -> None:
    """No local client configured + confidential payload → val terug naar Claude
    maar log nog steeds met label='confidential' zodat het zichtbaar is."""
    classifier = Classifier(confidential_keywords=("geheim",))
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, local_client=None)

    gw.complete(task="t", system="s", messages=[{"role": "user", "content": "iets geheim"}])

    assert fake_claude.last_call is not None  # Claude wel aangeroepen
    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "claude_call"
    assert rec["label"] == "confidential"
    assert rec["classifier_reason"] == "keyword_match"


def test_no_classifier_keeps_legacy_behaviour(tmp_path: Path) -> None:
    """Backwards compat: zonder classifier blijft gateway pure pass-through."""
    gw, fake_claude, _ = _build_gateway(tmp_path)
    gw.complete(task="t", system="s", messages=[{"role": "user", "content": "wat dan ook"}])
    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "claude_call"
    assert rec["label"] == "unclassified"
    assert rec["classifier_reason"] is None


# --- redaction path tests --------------------------------------------------

def _claude_returns(text: str) -> _FakeClaude:
    fake = _FakeClaude(model="claude-fake-1")
    # We monkeypatch reply so it returns a response with a single text block
    # carrying the placeholders Claude "saw" — to verify reconstruction.
    def _reply(**kwargs: Any) -> _FakeResponse:
        fake.last_call = kwargs
        # Build a response that echoes back the (redacted) user message text
        # so we can assert the gateway reconstructs it correctly.
        block = type("B", (), {"type": "text", "text": text})
        resp = _FakeResponse()
        resp.content = [block]
        return resp
    fake.reply = _reply  # type: ignore[assignment]
    return fake


def test_redactor_path_anonymises_to_claude_and_reconstructs_response(tmp_path: Path) -> None:
    classifier = Classifier(default_label="internal")
    redactor = Redactor(vip_people=("Piet",), vip_orgs=("Heineken",))
    audit = AuditLogger(tmp_path)
    gw = Gateway(api_key="dummy", model="claude-fake-1", audit=audit,
                 classifier=classifier, redactor=redactor)
    # Fake claude that echoes back the redacted message text it received,
    # mirroring "Claude only sees placeholders".
    fake = _claude_returns("Doe het zo: schrijf [PERSON_001] over [ORG_001].")
    gw._claude = fake  # type: ignore[assignment]

    resp = gw.complete(
        task="briefing",
        system="Be brief.",
        messages=[{"role": "user", "content": "Stel een mail aan Piet voor over Heineken."}],
    )

    # Claude saw placeholders, never the real names
    sent_messages = fake.last_call["messages"]
    assert "Piet" not in sent_messages[0]["content"]
    assert "Heineken" not in sent_messages[0]["content"]
    assert "[PERSON_001]" in sent_messages[0]["content"]
    assert "[ORG_001]" in sent_messages[0]["content"]

    # Caller (briefings/main) gets real names back
    out = resp.content[0].text
    assert "Piet" in out
    assert "Heineken" in out
    assert "[PERSON_001]" not in out

    # Audit shows redactions_applied > 0
    rec = _read_today(tmp_path)[0]
    assert rec["event"] == "claude_call"
    assert rec["label"] == "internal"
    assert rec["redactions_applied"] >= 2


def test_redactor_consistent_mapping_across_system_and_messages(tmp_path: Path) -> None:
    """Same entity in system AND in a user message → same placeholder."""
    classifier = Classifier(default_label="internal")
    redactor = Redactor(vip_people=("Piet",))
    audit = AuditLogger(tmp_path)
    gw = Gateway(api_key="dummy", model="claude-fake-1", audit=audit,
                 classifier=classifier, redactor=redactor)
    fake = _claude_returns("ok")
    gw._claude = fake  # type: ignore[assignment]

    gw.complete(
        task="t",
        system="Context: Piet is een klant.",
        messages=[{"role": "user", "content": "Reageer op Piet."}],
    )

    sys_text = fake.last_call["system"]
    msg_text = fake.last_call["messages"][0]["content"]
    # Same Piet → same placeholder ID
    assert "[PERSON_001]" in sys_text
    assert "[PERSON_001]" in msg_text
    assert "[PERSON_002]" not in sys_text
    assert "[PERSON_002]" not in msg_text


def test_preflight_failure_falls_back_to_local(tmp_path: Path) -> None:
    """Construct a redactor that DOESN'T cover IBAN, so it leaks through →
    pre-flight scan trips → fall back to local model with original (real) data."""
    classifier = Classifier(default_label="internal")
    # Empty redactor — no dictionary, but our redactor.regex layer DOES catch
    # IBAN. To force a preflight failure we have to bypass our own redactor;
    # cleanest test: monkeypatch redactor.redact to a no-op identity.
    class _NoOpRedactor:
        def redact(self, text: str, *, existing_mapping: dict[str, str] | None = None) -> Any:
            from privacy.redactor import Redaction
            return Redaction(text=text, mapping=existing_mapping or {})

    audit = AuditLogger(tmp_path)
    local = _FakeLocal()
    gw = Gateway(api_key="dummy", model="claude-fake-1", audit=audit,
                 classifier=classifier, redactor=_NoOpRedactor(),  # type: ignore[arg-type]
                 local_client=local)
    fake_claude = _FakeClaude(model="claude-fake-1")
    gw._claude = fake_claude  # type: ignore[assignment]

    # Payload met email survives de no-op redactor → preflight trips.
    # (IBAN gebruiken zou de classifier zelf 'confidential' laten labelen,
    # waardoor we direct naar local gaan vóór preflight ooit draait. Email
    # triggert preflight wel maar valt niet op in de classifier-regels.)
    resp = gw.complete(
        task="briefing",
        system="Antwoord beknopt.",
        messages=[{"role": "user", "content": "Stuur naar piet@klant.nl graag."}],
    )

    # Claude was NOT called; local was
    assert fake_claude.last_call is None
    assert local.last_call is not None
    # Local got the ORIGINAL data (it's local, allowed)
    assert "piet@klant.nl" in local.last_call["messages"][0]["content"]

    records = _read_today(tmp_path)
    assert any(r["event"] == "preflight_fallback" for r in records)
    # Followed by the local_call record
    assert any(r["event"] == "local_call" for r in records)


def test_tool_use_with_redactor_gets_redacted(tmp_path: Path) -> None:
    """Niveau B: tool-use loops gaan via redactor wanneer die wired is.
    Claude ziet placeholders, audit-label = 'tool_use_redacted'."""
    classifier = Classifier(default_label="internal")
    redactor = Redactor(vip_people=("Piet",))
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, redactor=redactor)

    gw.complete(
        task="tool_use_turn", system="s",
        messages=[{"role": "user", "content": "Stuur Piet een bericht."}],
        tools=[{"name": "x"}],
    )
    assert fake_claude.last_call is not None
    assert "Piet" not in fake_claude.last_call["messages"][0]["content"]
    assert "[PERSON_001]" in fake_claude.last_call["messages"][0]["content"]

    rec = _read_today(tmp_path)[0]
    assert rec["label"] == "tool_use_redacted"
    assert rec["redactions_applied"] >= 1


def test_complete_tool_turn_returns_response_and_mapping(tmp_path: Path) -> None:
    """Orchestrator-facing: complete_tool_turn yields (response, mapping)
    so the caller can reconstruct tool inputs locally."""
    classifier = Classifier(default_label="internal")
    redactor = Redactor(vip_people=("Piet",), vip_orgs=("Heineken",))
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, redactor=redactor)

    response, mapping = gw.complete_tool_turn(
        task="tool_use_turn", system="s",
        messages=[{"role": "user", "content": "Stuur Piet bij Heineken een mail."}],
        tools=[{"name": "x"}],
    )
    # Mapping covers both entities, with stable [CAT_NNN] keys
    assert "Piet" in mapping.values()
    assert "Heineken" in mapping.values()
    person_ph = next(k for k, v in mapping.items() if v == "Piet")
    assert person_ph.startswith("[PERSON_")
    # Response object is the raw Claude response (placeholders intact)
    assert fake_claude.last_call is not None


def test_complete_tool_turn_continues_mapping_across_calls(tmp_path: Path) -> None:
    """Same Piet in turn 2 keeps placeholder from turn 1."""
    classifier = Classifier(default_label="internal")
    redactor = Redactor(vip_people=("Piet",))
    gw, fake_claude, _ = _build_gateway(tmp_path, classifier=classifier, redactor=redactor)

    _, mapping = gw.complete_tool_turn(
        task="t", system="s",
        messages=[{"role": "user", "content": "Piet stuurt iets."}],
        tools=[{"name": "x"}],
    )
    person_ph_first = next(k for k, v in mapping.items() if v == "Piet")

    _, mapping = gw.complete_tool_turn(
        task="t", system="s",
        messages=[
            {"role": "user", "content": "Eerste bericht."},
            {"role": "user", "content": "Wat zei Piet?"},
        ],
        tools=[{"name": "x"}],
        mapping=mapping,
    )
    person_ph_second = next(k for k, v in mapping.items() if v == "Piet")
    assert person_ph_first == person_ph_second


# --- H2: hallucinated-placeholder audit contract ------------------------

def _build_with_redactor(tmp_path: Path):
    """Helper: gateway met redactor + classifier zodat _call_claude_redacted
    daadwerkelijk wordt aangeroepen (anders skipt complete() de reconstruct-
    pas en is er geen audit-veld om te checken)."""
    redactor = Redactor(
        vip_people=("Marc",), vip_emails=(), vip_orgs=(),
        vip_projects=(), safe_terms=(), amount_threshold=1000.0,
        ner_model=None,
    )
    classifier = Classifier()
    gw, fake, audit = _build_gateway(
        tmp_path, redactor=redactor, classifier=classifier,
    )
    return gw, fake, audit


def test_audit_records_hallucinated_placeholders_count(tmp_path: Path) -> None:
    """Als Claude een placeholder uitspuugt die NIET in de redactor-mapping
    zat, moet het audit-record `hallucinated_placeholders: N` bevatten
    zodat `grep` over egress-*.jsonl monitoring mogelijk maakt.

    Truc: Claude verzint [URL_001] (geen URL in input → niet in mapping)
    en (ORG_001) parens-bare (mapping kent geen ORG). [PERSON_001] is
    bewust NIET hallucinated hier — Marc zit in vip_people en `Marc`
    staat in het bericht, dus mapping heeft 'em.
    """
    from privacy.gateway import _TextBlock

    gw, fake, _ = _build_with_redactor(tmp_path)
    fake.reply = lambda **kw: _FakeResponse(  # type: ignore[assignment]
        content=[_TextBlock(text="Meet: [URL_001] over (ORG_001) met [PERSON_001].")],
    )
    out = gw.complete(
        task="morning_briefing",
        system="Schrijf een korte test.",
        messages=[{"role": "user", "content": "Marc komt langs."}],
    )

    text = "".join(b.text for b in out.content if getattr(b, "type", None) == "text")
    # Hallucinaties → fallbacks
    assert "[URL_001]" not in text
    assert "(ORG_001)" not in text  # H1: parens-bare leftover wordt ook gestript
    assert "a link" in text
    assert "an organization" in text
    # Legit mapping → echte waarde
    assert "Marc" in text

    rec = _read_today(tmp_path)[0]
    assert rec.get("hallucinated_placeholders") == 2
    # M1: altijd list, ook bij ≥1; sorted+gededupliceerd
    assert rec.get("hallucinated_categories") == ["ORG", "URL"]


def test_audit_hallucinated_categories_is_empty_list_when_none(
    tmp_path: Path,
) -> None:
    """M1 review-finding: bij 0 hallucinaties moet het veld een lege
    list zijn (niet None) — anders heeft de log-stream een inconsistente
    shape per regel."""
    from privacy.gateway import _TextBlock

    gw, fake, _ = _build_with_redactor(tmp_path)
    # Claude antwoordt zonder verzonnen placeholders
    fake.reply = lambda **kw: _FakeResponse(  # type: ignore[assignment]
        content=[_TextBlock(text="Schone tekst zonder placeholders.")],
    )
    gw.complete(
        task="morning_briefing",
        system="Schrijf een korte test.",
        messages=[{"role": "user", "content": "Marc komt langs."}],
    )

    rec = _read_today(tmp_path)[0]
    assert rec.get("hallucinated_placeholders") == 0
    assert rec.get("hallucinated_categories") == []
