# Sub-processors register

> Welke externe diensten verwerkt pa-agent persoonsgegevens namens
> you? Voor ISO 27001 (annex A.15) en GDPR art 28 (verwerker-
> overeenkomsten) moet dit register actueel zijn. Bijwerken bij elke
> nieuwe integration of feature die data extern stuurt.
>
> **Laatst bijgewerkt:** 2026-06-28 (na ISO/security audit — Slack +
> Ntfy + Plaud + Todoist push-flow nieuw; egress-wraps compleet).

---

## Actieve sub-processors

| Provider | Doel | Data die uitgaat | Frequentie | DPA | SCC | Datalocatie | Status |
|---|---|---|---|---|---|---|---|
| **Anthropic** (Claude) | LLM reasoning, briefings, tools, summaries | Geredacteerde tekst (placeholders), tool-arguments — nooit raw mail-body of audio | Per iMessage-turn + briefings (2-3×/dag) + market-intel scoring (~150/wk) | ❌ niet aangevraagd | ❓ — you verifiëren via console | US (Anthropic Inc) | active, default |
| **Google Workspace** (Gmail + Calendar) | Read mail + agenda, send mail, create events | Gmail OAuth scopes: `gmail.modify` + `gmail.send` + `calendar.events`. Outgoing mail-content gaat door Gmail. | Continuous polling (5 min); send on-demand | ✅ standaard via Workspace contract | ✅ via Workspace | EU/US shared (afhankelijk van you's tenant region) | active |
| **HERE Maps** | Travel-time + traffic voor "vertrek nu" alerts | GPS-coordinaten you (origin) + event-locatie (destination); geen identiteit | Per agenda-event met fysieke locatie + 1-2× geocoding/dag | ❌ niet aangevraagd | ❌ | Duitsland (HERE Global B.V., NL HQ) | active |
| **Todoist** (Doist Inc) | Bidirectional sync van reminders + open_loops | Task content (sender-namen, body-excerpts, due-times); pull leest task status terug | Push direct bij creatie + pull elke 5 min | ❌ niet aangevraagd | ❌ | EU (Doist heeft EU-DC) | active |
| **macOS** (Apple, Inc) | iMessage delivery, Spoken Content (Ava TTS), faster-whisper STT | iMessage gaat E2E-encrypted via Apple's IDS. Whisper draait 100% lokaal. `say`/Ava draait 100% lokaal. | Continuous (iMessage); on-demand (TTS/STT) | ✅ via Apple Business / iCloud agreement | ✅ | EU (Apple Distribution Int'l, Cork) | active |
| **Hosted IMAP** (DPM, HGE) | Inkomende mail lezen + uitgaande SMTP | Body's leesbaar door provider per definitie van IMAP | Polling per 5 min + send on-demand | ✅ via individuele hosting-overeenkomst | n.v.t. (NL) | NL | active |
| **Slack** (Salesforce) | Lezen van you's workspaces voor comm-intel | OAuth user-token (xoxp); reads via auth.test / conversations.list / conversations.history / users.list. Geen writes. | Polling per 5 min per workspace | ✅ via Slack Enterprise / SCC | ✅ | EU/US shared (Salesforce tenant) | active sinds 27/6 — egress nu in audit-stream (E-1 fix) |
| **Plaud** (cloud) | Voice-recordings via Plaud-app → tekstbestanden in `~/PlaudInbox/` | Audio transcripts arriveren al getranscribeerd; pa-agent leest alleen lokaal — geen API-call. Plaud zelf is wel een sub-processor van you's recordings. | n.v.t. (file-watch lokaal) | ❌ niet aangevraagd | ❌ | US (Plaud) | active (recording-stream is buiten pa-agent) |
| **Ntfy.sh** | Critical-priority push voor uptime-alerts die door iPhone-DND breken | Geredacteerde uptime-alert (URL + HTTP-status + downtime-duur). Geen klant-data. | On-incident (~zelden) | ❌ niet aangevraagd | ❌ (zelf-hostbaar) | DE/ES (heimdal.io) | active — topic-string is shared secret, niet rauw gelogd |

## Optionele / niet-default sub-processors

| Provider | Doel | Status |
|---|---|---|
| **ElevenLabs** | Cloud TTS voor voice-replies (Sarah-stem) | **Disabled by default sinds 24/4** — Ava (Enhanced) macOS lokaal is nu primary. Code blijft als fallback; setting `tts.engine` moet expliciet op `elevenlabs` voor activatie. **Aanbeveling:** uitfaseren tenzij you specifiek de NL-stem wil. |
| **Google News RSS** | Press/mentions monitoring (your Company, HGE, you Geerts) | Anonieme reads van publieke RSS — geen account/auth, geen data uit. Niet écht een sub-processor in GDPR-zin. |
| **Hugging Face** | Whisper-model download + RSS van papers | Eenmalige model-download (geanonimiseerd). RSS-papers anoniem. |
| **Various RSS** (Invidis, TechCrunch, etc.) | Market-intel feeds | Anonieme HTTP GETs. Geen data uit. |

## Lokale verwerking (geen sub-processor)

Volgende componenten draaien volledig op you's Mac — geen externe data-overdracht:

- **Ollama (Llama 3.1 8B + nomic-embed)** — confidential routing + market-intel scoring + (binnenkort) Drive vector-index. Alle modellen on-device, geen telemetry.
- **faster-whisper** — voice-message transcriptie. Model draait lokaal na eenmalige download.
- **macOS Spotlight/mdfind** — geplande Drive Level 1 lookup, 100% lokaal index.
- **SQLite** — `data/memory.db` is enige opslag. 0600 perms. Geen replicatie.
- **spaCy NER** (nl_core_news_md) — naam-detectie voor de redactor.

---

## Acties per provider (TODO you)

### Anthropic — secundaire sub-processor: web_search backend

Sinds 29/6 staat de Anthropic **server-side `web_search`-tool** aan
(`max_uses: 3` per turn) zodat Rosa actuele info kan ophalen
(openingstijden, KvK, nieuws). Anthropic gebruikt hiervoor een eigen
search-sub-processor (per hun documentatie: Brave Search). Wij geven
de query in `tools=[…]` mee aan Claude; Anthropic voert de search uit
en levert resultaten als `web_search_tool_result`-blocks terug.

- **Wat verlaat onze redaction-laag**: de query-string die Claude
  genereert. SYSTEM_PROMPT instrueert Rosa GEEN volledige namen /
  e-mailadressen / KvK-nummers in queries op te nemen, maar dit is
  best-effort.
- **Wat we wel kunnen aantonen**: per-turn counter in `egress-*.jsonl`
  als `service=anthropic_web_search, endpoint=server_tool, note=count=N`
  (sinds c8562d7+review-fix). Geen query-content gelogd.
- **Aanbeveling**: opname in jouw `verwerker-overeenkomst` met
  Anthropic verifiëren (DPA dekt server-tools impliciet als deel van
  hun service).

### Anthropic
- Aanvragen DPA via console → support → Data Processing Agreement
- Verifieer of huidige API-key onder Workspace zit (DPA dekt automatisch) of personal account (geen DPA)

### HERE Maps
- Free-tier heeft géén DPA. Voor productie: Business-tier upgrade (~€50/mnd) met DPA + SCC
- Of: data-minimalisatie evalueren — kunnen we elders een DPA-vrije provider vinden?

### Todoist
- Doist biedt DPA aan via support@doist.com
- Aanvragen + opslaan in you's compliance-folder

### ElevenLabs
- **Beslissing nodig:** uitfaseren of upgraden?
- Default is al `say`/Ava lokaal (geen exposure)
- Als you de cloud-stem nooit gebruikt: code intact laten als fallback maar in dit register als "disabled" houden

### Google Workspace
- Reeds gedekt via Workspace-contract (zou automatisch DPA hebben)
- Verifieer dat huidige tenant onder een betaalde Workspace-licentie valt

---

## Audit-trail

Elke external HTTP-call wordt sinds commit `c1e1b37` gelogd in
`data/audit/egress-YYYY-MM-DD.jsonl` met event `external_call`:

```json
{"ts": "...", "event": "external_call", "service": "todoist",
 "endpoint": "GET /projects", "status": 200,
 "bytes_out": 0, "bytes_in": 5164, "latency_ms": 762}
```

Geen content — alleen metadata voor ISO-aantoonbaarheid van wat-wanneer-
naar-waar. Audit-rotatie via `audit_retention_days` (default 90) gewaarborgd.

Sinds 28/6 zijn ook Slack-API-calls (auth.test / conversations.list /
conversations.history / users.list) door dezelfde audit-wrap. Plaud
(lokale file-watch) heeft geen egress. Gmail + Calendar gaan door
`audit_googleapi_execute` (vorige ronde gefixt).

## GDPR art 17 — right to erasure

Voor verwijderverzoeken: `scripts/purge_person.py --identifier <…>
--force`. Default is dry-run; --force schrijft een audit-event in
`data/audit/admin-*.jsonl` met counts (geen content) voor ISO A.18.1.1.
Scant comm_items, conversation_turns, processed_messages, open_loops,
reminders, memories, sales_accounts.
