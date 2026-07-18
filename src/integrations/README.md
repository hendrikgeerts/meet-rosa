# integrations

## Doel
Adapters naar de externe systemen waar Hendrik's data leeft of vandaan komt.
Elke integratie is een dunne facade rond een SDK of OS-API — geen
business-logica.

## Modules
- `gmail.py` — `GmailClient` (lijst recent, zoeken, threaden, sturen,
  markeren als gelezen) bovenop `google-api-python-client`.
- `gcal.py` — `CalendarClient` (events listen, vrije slots zoeken, events
  CRUDen) — alle tijden Europe/Amsterdam.
- `google_auth.py` — `get_credentials()` met OAuth installed-app flow op
  `localhost:8765`. Token wordt gepersisteerd in `data/google_token.json`.
- `imessage.py` — leest nieuwe berichten uit `~/Library/Messages/chat.db`
  (snapshot-kopie + `attributedBody` extractor voor Ventura+) en stuurt
  uitgaande berichten via `osascript`. Vereist Full Disk Access voor
  Terminal/Python.
- `plaud.py` — watch-folder ingestor voor Plaud-transcripts. Plaud heeft
  geen publieke API, dus we lezen `~/PlaudInbox/*.txt` en dedupliceren op
  sha256 van de body.
- `voice.py` — `faster-whisper` (model `base`, int8 op CPU) voor lokale
  transcriptie van iMessage audio-attachments. Model is lazy-geladen.

## Public interface
- `GmailClient`, `CalendarClient`, `get_credentials`
- `imessage.fetch_new_messages`, `imessage.send_imessage`,
  `imessage.IncomingMessage`
- `plaud.scan_inbox`, `plaud.init_plaud_schema`
- `voice.transcribe_caf`, `voice.attachments_for_message`,
  `voice.snapshot_chat_db_for_attachments`, `voice.resolve_attachment_path`

## Config-keys
- `paths.google_credentials`, `paths.google_token` — OAuth-flow
- `paths.messages_db` — pad naar chat.db (default `~/Library/Messages/chat.db`)
- `paths.plaud_inbox` — watch-folder
- `runtime.whisper_model` — modelgrootte voor `voice.py`
  (`base`/`small`/`medium`/`large-v3`)
- `imessage.poll_interval_seconds` — chat.db polling-frequentie

## Privacy-implicaties
- Mail- en agendadata: maximaal gevoelig. Mag alleen via
  `privacy.gateway.complete(...)` naar Claude.
- Audio-attachments worden lokaal getranscribeerd; bestanden mogen volgens
  `AGENT_SPEC_EXTENSIONS §1` na X dagen gepurged (configureerbaar — nog
  niet geïmplementeerd, TODO).
- iMessage gaat E2E-encrypted tussen Apple-apparaten maar wordt door Apple
  gerouteerd — `confidential`-categorie briefings horen daarom (later) via
  het lokale dashboard te lopen i.p.v. iMessage (`HYBRID_ARCHITECTURE §6`).
- OAuth-tokens worden vandaag in `data/google_token.json` (mode 0600)
  bewaard. `CLAUDE.md` schrijft Keychain-opslag voor — TODO.

## Testscenario's
Geen unit-tests vandaag (alle integraties raken externe APIs). De spec
schrijft `respx`-mocking voor; te implementeren wanneer we de
gmail/calendar paden refactoren voor concept-mails (Fase 2).
