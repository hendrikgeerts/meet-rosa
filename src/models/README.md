# models

## Doel
Wrappers rond de LLM-providers. Houdt SDK-specifieke code geïsoleerd zodat
we van model kunnen wisselen zonder elke caller te raken.

## Modules
- `claude.py` — `ClaudeClient`: dunne wrapper rond `anthropic.Anthropic`.
  Stuurt het system-block als ephemeral cache-block (5-minuten TTL prompt
  cache).

## Niet aanwezig (komt later)
- `ollama.py` — lokale model-client voor classifier-tiebreaker, redactie-
  vangnet en `confidential`-routering.
- `whisper.py` — wrapper rond `faster-whisper` (vandaag in `integrations/voice.py`,
  hoort hier).
- `embedding.py` — `nomic-embed-text` via Ollama voor RAG / vector-zoek.

## Public interface
- `ClaudeClient(api_key, model)` — alléén `privacy.gateway` mag deze importeren.

## Config-keys
- `runtime.claude_model` — modelnaam (bv. `claude-sonnet-4-6`)
- `runtime.local_model_small`, `runtime.local_model_main`,
  `runtime.embedding_model`, `runtime.whisper_model` — voor toekomstige
  modules

## Privacy-implicaties
`claude.py` doet de daadwerkelijke externe HTTPS-call. Het is achter
`privacy.gateway` gezet — directe import elders is een bug.

## Testscenario's
Geen direct, want testen tegen de Anthropic API kost geld en is flaky.
`tests/test_gateway.py` injecteert een fake `ClaudeClient`-achtige om de
gateway te testen zonder de echte SDK te raken.
