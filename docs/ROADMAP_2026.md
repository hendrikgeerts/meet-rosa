# Rosa — Roadmap 2026

> **Canonieke roadmap-bron.** Bundelt (a) formele fases uit `AGENT_SPEC.md`,
> (b) extensies uit `AGENT_SPEC_EXTENSIONS.md`, en (c) ideeën uit werk-
> sessies die nog niet formeel gedocumenteerd waren. Elke feature krijgt
> hier een status en (waar zinvol) prioritering.
>
> **Bijwerken bij:** elke nieuwe feature-commit óf elke sessie waarin
> een nieuw idee ontstaat. Status verandert alleen als er code + test
> voor bestaan.
>
> **Laatst bijgewerkt:** 2026-07-14

---

## Legenda

| Status | Betekenis |
|---|---|
| ✅ | Gebouwd, in productie, tests groen |
| 🟡 | Gedeeltelijk gebouwd of basis werkt maar spec incompleet |
| ⏳ | Gepland, nog niet begonnen |
| ❌ | Bewust niet gedaan (uitgesloten of niet meer relevant) |

**Prioriteit-labels:**
- **[MUST]** Zonder dit is de MVP niet compleet voor you's gebruik.
- **[SHOULD]** Waardevol, ingepland voor komende maanden.
- **[NICE]** Aardig, gebeurt als er een venster is.
- **[M4]** Vereist Mac mini M4 Pro upgrade (huidige Intel MBP zit vast).

---

## Deel A — Kernfundament (`AGENT_SPEC.md` §8)

### Fase 1 — Foundation *(week 1-2)* ✅

| # | Feature | Status |
|---|---|---|
| 1.1 | Project skeleton, config-laag, secrets management | ✅ |
| 1.2 | Gmail + Google Calendar read-only integratie | ✅ |
| 1.3 | iMessage-bridge: ontvangen + sturen | ✅ |
| 1.4 | Minimale orchestrator-loop met Claude Sonnet | ✅ |

### Fase 2 — Triage & memory *(week 3-4)* ✅

| # | Feature | Status |
|---|---|---|
| 2.1 | Mail-classificatie + draft-generatie (Gmail write) | ✅ |
| 2.2 | Memory-laag (SQLite + sqlite-vec) + fact-extractie review-queue | ✅ |
| 2.3 | Dagopstart-briefing via iMessage | ✅ |

### Fase 3 — Plaud + meetings *(week 5-6)* ✅

| # | Feature | Status |
|---|---|---|
| 3.1 | Plaud-integratie: transcripts + agenda-koppeling | ✅ |
| 3.2 | Post-meeting verwerking end-to-end | ✅ |
| 3.3 | Meeting-prep brief 30 min vóór event | ✅ |

### Fase 4 — Outlook + IMAP + polish *(week 7-8)*

| # | Feature | Status | Notes |
|---|---|---|---|
| 4.1 | Outlook via Graph API | ❌ [NICE] | Jouw setup gebruikt IMAP/Gmail — bouw pas als Outlook-tenant erbij komt |
| 4.2 | Generieke IMAP-client | ✅ | Multi-account: invoicing/general/mymail/personal/procurement |
| 4.3 | Follow-up radar, reminder-engine | ✅ | Reminders + Todoist-sync |
| 4.4 | Webdashboard voor review + instellingen | ✅ | localhost:8080 |

### Fase 5 — Autonomie *(doorlopend)*

| # | Feature | Status | Notes |
|---|---|---|---|
| 5.1 | Gecontroleerd uitbreiden zelfstandig-uitvoeren-scope | ✅ | 100+ tools live |
| 5.2 | Evaluatie-metriek: hoeveel tijd bespaart het écht? | ⏳ [SHOULD] | Nog geen gestructureerde meting |

---

## Deel B — Extensies (`AGENT_SPEC_EXTENSIONS.md`)

### §1 — Voice-in via iMessage 🟡

| # | Feature | Status |
|---|---|---|
| B1.1 | iMessage bridge detecteert audio-attachment | ✅ |
| B1.2 | Whisper lokaal (`faster-whisper` medium) | ✅ |
| B1.3 | Intent parsing → todo/event/memo/query | ✅ |
| B1.4 | Bevestiging via iMessage | ✅ |
| B1.5 | **Upgrade Whisper → `large-v3`** | ⏳ [SHOULD] [M4] |

### §2 — Foto's en screenshots als input ⏳ [SHOULD]

| # | Feature | Status |
|---|---|---|
| B2.1 | Apple Vision-wrapper (Swift helper of `osascript`) | ⏳ |
| B2.2 | Tesseract fallback | ⏳ |
| B2.3 | Content-type classifier (visitekaartje/whiteboard/factuur/screenshot) | ⏳ |
| B2.4 | Opt-in Claude Vision voor complexe foto's | ⏳ |

### §3 — Mobiele voice-memo via iOS Shortcut 🟡

| # | Feature | Status |
|---|---|---|
| B3.1 | iOS Shortcut installatie + docs | ✅ |
| B3.2 | Tailscale bridge naar Mac | ✅ |
| B3.3 | Lokale HTTP-endpoint (`/voice-memo`) | ✅ |
| B3.4 | **Uitgebreide voice-memo intent parser** | 🟡 [NICE] | Nu vooral locatie-updates |

### §4 — Eigen SQLite-todolist ✅

| # | Feature | Status |
|---|---|---|
| B4.1 | `reminders` + `open_loops` tabellen | ✅ |
| B4.2 | iMessage-interface (natural language) | ✅ |
| B4.3 | Todoist-sync bi-directioneel + review-queue | ✅ |
| B4.4 | Webdashboard bulk-review | ✅ |
| B4.5 | **Formele `project_id` koppeling** | ⏳ | Wordt onderdeel van §5 |

### §5 — Projectcontext (dossiers) ⏳ [SHOULD]

| # | Feature | Status |
|---|---|---|
| B5.1 | `projects` + `project_items` join-tabel | ⏳ |
| B5.2 | Auto-koppeling mails/meetings/tasks aan project | ⏳ |
| B5.3 | Voorstel-flow bij onzekere match ("hoort deze bij SSL-migratie?") | ⏳ |
| B5.4 | Query: "toon alles over project X" → tijdlijn | ⏳ |
| B5.5 | Vector-index per project | ⏳ |

### §6 — Beslissingslogboek 🟡

| # | Feature | Status |
|---|---|---|
| B6.1 | `decisions` extensie + schema | ✅ |
| B6.2 | Expliciet loggen via iMessage | ✅ |
| B6.3 | Semi-automatische detectie uit meetings/mail | ✅ |
| B6.4 | `superseded_by` heroverwegings-flow | ⏳ [NICE] |

### §7 — Kennisbank / interne RAG ⏳ [MUST]

| # | Feature | Status |
|---|---|---|
| B7.1 | `~/rosa-knowledge/` map-watcher | ⏳ |
| B7.2 | Lokale embeddings via `nomic-embed-text` | ⏳ (infrastructuur bestaat) |
| B7.3 | Vector store voor documenten | ⏳ |
| B7.4 | Cross-encoder re-rank (optioneel) | ⏳ [NICE] |
| B7.5 | Top-N snippet-retrieval in draft-mails | ⏳ |
| B7.6 | iCloud/Dropbox read-only mount | ⏳ [NICE] |

### §8 — Patroonherkenning + energie-management 🟡

| # | Feature | Status |
|---|---|---|
| B8.1 | `patterns` extensie (mail-volume, VIP-stiltes) | ✅ |
| B8.2 | Response-time analytics per sender | ✅ |
| B8.3 | Focus-block detectie in briefings | ✅ |
| B8.4 | **Health.app slaap/hartslag ↔ productiviteit** | ⏳ [SHOULD] |
| B8.5 | Zelf-gerapporteerde dagscore (1-5) in dayclose | ⏳ [NICE] |
| B8.6 | Maandelijks patroonrapport | ⏳ [NICE] |

### §9 — Cognitive debt bewaking ⏳ [SHOULD]

| # | Feature | Status |
|---|---|---|
| B9.1 | Metrics: open-mails >3d, todos zonder deadline, overdue, follow-up-radar | 🟡 | `whats_open` doet het meeste maar niet met drempels |
| B9.2 | Drempels 0-15 / 15-30 / >30 met signalering | ⏳ |
| B9.3 | Opschonings-assistent: batches van 5 met archiveren/delegeren/deadline-verzetten | ⏳ |

### §10 — Zaterdag-weekreview ✅

| # | Feature | Status |
|---|---|---|
| B10.1 | `weekly_retro` extensie (gebouwd 29/6) | ✅ |
| B10.2 | Gelukt / niet-gelukt / patronen / volgende week | ✅ |
| B10.3 | **Interactieve reacties verwerken tot acties** | ⏳ [SHOULD] |

### §11 — Voorspellend: terugkerende patronen ⏳ [NICE]

| # | Feature | Status |
|---|---|---|
| B11.1 | Wekelijkse scan op periodieke events/mails | ⏳ |
| B11.2 | `patterns` tabel met periodiciteit + volgende-verwachte-datum | ⏳ |
| B11.3 | Proactieve briefing 1-2 wk vóór verwacht voorkomen | ⏳ |

### §12 — Research-taken (deep search) 🟡

| # | Feature | Status |
|---|---|---|
| B12.1 | `web_search` server-tool basis | ✅ |
| B12.2 | **`research: <vraag>` trigger + multi-step plan** | ⏳ [SHOULD] |
| B12.3 | Parallel sub-queries → synthese | ⏳ |
| B12.4 | Markdown-rapport in `~/pa-agent/research/` | ⏳ |
| B12.5 | Bronvermelding per claim | ⏳ |

### §13 — Delegeer-tracker 🟡

| # | Feature | Status |
|---|---|---|
| B13.1 | `outgoing_request` + `meeting_action_other` als open_loops | ✅ |
| B13.2 | 7d auto-followup ping via scheduler | ✅ |
| B13.3 | `delegations_list` + `delegation_extend_followup` tools | ✅ |
| B13.4 | **Auto-close op inkomende mail-match** | ⏳ [SHOULD] |
| B13.5 | Per-persoon SLA-cadence (nu uniform 7d) | ⏳ [NICE] |

---

## Deel C — Backlog uit sessies (niet in eerdere docs)

Deze items zijn ontstaan in werk-sessies met you en zijn per juli 2026
in deze canonieke roadmap opgenomen. Prioriteit is initieel; te wijzigen.

### C1 — Realtime voice-call met Rosa ⏳ [SHOULD] [M4]

Live tweerichtings-gesprek in plaats van batch iMessage.

| # | Feature | Status |
|---|---|---|
| C1.1 | Streaming Whisper (whisper.cpp chunks 1-2s) | ⏳ |
| C1.2 | VAD (Silero of WebRTC-VAD) | ⏳ |
| C1.3 | Streaming LLM-response via bestaande gateway | ⏳ |
| C1.4 | Streaming TTS (Piper of Apple `say`) | ⏳ |
| C1.5 | Interrupt-handling / full-duplex state machine | ⏳ |
| C1.6 | Audio-input capture (PyAudio / sounddevice) | ⏳ |

**Schatting:** 5-7 dagen werk. **Hardware-blocker** tot M4 upgrade.

### C2 — WhatsApp Business API integratie ⏳ [MUST]

Jouw #1 B2B-NL kanaal zit compleet buiten Rosa's view.

| # | Feature | Status |
|---|---|---|
| C2.1 | WhatsApp Business API sub-processor + DPA | ⏳ |
| C2.2 | Read-only ingest naar `comm_items` | ⏳ |
| C2.3 | Reply-drafts + send met approval-flow | ⏳ |
| C2.4 | Cross-channel VIP-mapping (mail + Slack + WA) | ⏳ |

**Schatting:** 2-3 dagen werk + DPA-actie bij Meta.

### C3 — Proactieve concept-replies ⏳ [SHOULD]

Bij mails >24u zonder antwoord: Rosa draft, jij review, tap → verstuur.

| # | Feature | Status |
|---|---|---|
| C3.1 | Detectie: `response_time_overdue` triggert | 🟡 tool bestaat |
| C3.2 | Llama intent-extract uit thread | ⏳ |
| C3.3 | Claude-draft in jouw stijl (tone per sender-cluster, zie C6) | ⏳ |
| C3.4 | iMessage preview → tik akkoord → `gmail_send` | ⏳ |

### C4 — Meeting-listener realtime ⏳ [NICE] [M4]

Rosa luistert live mee (Plaud-stream of macOS audio) en haalt tijdens
meeting al context op.

| # | Feature | Status |
|---|---|---|
| C4.1 | Live audio-stream ingest | ⏳ |
| C4.2 | Real-time transcript | ⏳ |
| C4.3 | Contextuele suggesties tijdens meeting | ⏳ |

### C5 — Vergaderprep-agent voor externe deelnemers ⏳ [SHOULD]

Vóór een call: web_search naar bedrijf + persoon, 1-pager sturen.

| # | Feature | Status |
|---|---|---|
| C5.1 | Trigger 15 min vóór meeting met externe attendees | ⏳ |
| C5.2 | `web_search` naar KvK + LinkedIn + laatste nieuws | ⏳ |
| C5.3 | 1-pager gerenderd + verzonden via iMessage | ⏳ |

### C6 — Auto-response templates per sender-cluster ⏳ [NICE]

Leer welke tone/lengte je gebruikt bij welk type klant.

| # | Feature | Status |
|---|---|---|
| C6.1 | Cluster VIP's op response-stijl (embedding of manual) | ⏳ |
| C6.2 | Drafts in de juiste "voice" | ⏳ |

### C7 — Topic-clustering ✅

| # | Feature | Status |
|---|---|---|
| C7.1 | `comm_topics_active` + `comm_topic_items` tools | ✅ (29/6) |

### C8 — Response-time analytics ✅

| # | Feature | Status |
|---|---|---|
| C8.1 | `response_time_stats` per sender-baseline | ✅ (29/6) |
| C8.2 | `response_time_overdue` — wie wacht langer dan baseline | ✅ (29/6) |

### C9 — Duplicate-detection op reminders ✅

| # | Feature | Status |
|---|---|---|
| C9.1 | Preventieve check bij `set_reminder` (SequenceMatcher + Jaccard) | ✅ (30/6) |
| C9.2 | Weekly scan (zaterdag, skip als 0) | ✅ (30/6) |

### C10 — Rich person-profile ⏳ [NICE]

Verrijken VIP-config met LinkedIn/KvK/recente touchpoints.

| # | Feature | Status |
|---|---|---|
| C10.1 | LinkedIn-snippet ingest | ⏳ |
| C10.2 | KvK-koppeling | ⏳ |
| C10.3 | Rendering in `person_brief` | ⏳ |

### C11 — Zelf-lerende classifier ⏳ [SHOULD]

Wanneer je een mail-label corrigeert: Rosa leert het.

| # | Feature | Status |
|---|---|---|
| C11.1 | Feedback-tool ("dit was toch geen actie") | ⏳ |
| C11.2 | Learning-loop op comm_intel intent-classifier | ⏳ |

### C12 — Slack scheduled-send + reminder ⏳ [NICE]

"Reageer maandag" → Rosa schedulet reply.

| # | Feature | Status |
|---|---|---|
| C12.1 | Slack outbound met scheduled_at | ⏳ |
| C12.2 | Reminder-integratie op Slack-thread | ⏳ |

### C13 — Mobile web-dashboard 🟡

| # | Feature | Status |
|---|---|---|
| C13.1 | Localhost dashboard werkt | ✅ |
| C13.2 | Responsive voor iPhone via Tailscale | ⏳ [NICE] |
| C13.3 | iOS-shortcuts naar dashboard-actions | ⏳ [NICE] |

---

## Deel D — Doorlopende onderhouds-loops

Deze staan niet in de docs maar zijn bewust doorlopend:

| # | Loop | Cadans |
|---|---|---|
| D1 | Code-review na feature-rondes (Plan-agent) | Per 3+ commits |
| D2 | ISO / security audit (Plan-agent) | Per grote release |
| D3 | STATUS.md bijwerken | Elke werksessie |
| D4 | Memory-updates over you's context | Continu |
| D5 | Auto-restart daemon na code-wijziging | Per commit met src/-change |

---

## Deel E — Bewust NIET gebouwd

- **Outlook Graph API** — you gebruikt IMAP/Gmail
- **ElevenLabs default TTS** — vervangen door lokaal `say` (Ava Enhanced) sinds 24/4 ivm ISO 27001 (`feedback_pa_agent_local_tts.md`)
- **LangChain / LlamaIndex** — CLAUDE.md verbiedt zware frameworks
- **Cloud-services buiten geregistreerde sub-processors** — geen Supabase, Vercel, OpenAI als fallback
- **Reseller-support tooling** — you doet dat zelf, geen agent-feature
- **Telemetry naar derden** — lokale logs, punt

---

## Prioritering — komende 3-6 maanden

### Must-have (grootste operationele impact)

1. **B7 — Interne RAG** — jouw eigen documenten searchable via Rosa. Grootste kennisbank-verbetering.
2. **C2 — WhatsApp Business** — B2B-NL kanaal in Rosa's view. Sluit een grote blind spot.

### Should-have (kwaliteit + efficiency)

3. **C3 — Proactieve concept-replies** — bouwt op bestaande response-time analytics.
4. **B5 — Projectcontext** — cross-source aggregatie per initiatief.
5. **B9 — Cognitive debt met drempels** — uitbreiding van bestaande whats_open.
6. **B12 — Research-workflow deep** — meer dan enkele `web_search`-call.
7. **B8.4 — Health.app energie-koppeling** — meetbaar patroon-inzicht.
8. **C5 — Vergaderprep-agent** — automatische 1-pager vóór externe calls.
9. **C11 — Zelf-lerende classifier** — feedback-loop op mail-classificatie.
10. **B10.3 — Interactieve weekreview** — reacties tot acties verwerken.
11. **B13.4 — Auto-close delegations bij mail-match** — huidige is handmatig.

### Nice-to-have (bij venster)

12. **B2 — Foto/screenshot input** — factuur/whiteboard OCR.
13. **B6.4 — Superseded decisions** — heroverwegings-flow.
14. **B11 — Voorspellend patronen** — kwartaal-cycli anticiperen.
15. **C6 — Auto-response templates per cluster** — tone-matching.
16. **C10 — Rich person-profile** — LinkedIn/KvK verrijking.
17. **C12 — Slack scheduled-send** — reageer-maandag-flow.
18. **C13 — Mobile dashboard** — iPhone-responsive UI.

### M4-gated (wacht op hardware)

19. **B1.5 — Whisper `large-v3` upgrade**
20. **C1 — Realtime voice-call met Rosa** — 5-7 dagen werk zodra hardware er is.
21. **C4 — Meeting-listener realtime** — live audio-stream ingest.

---

## Score

| Categorie | ✅ Klaar | 🟡 Deels | ⏳ Open | ❌ Uit | Totaal items |
|---|---|---|---|---|---|
| A — Fase 1-5 | 15 | 0 | 1 | 1 | 17 |
| B — Extensies §1-13 | 12 | 5 | 27 | 0 | 44 |
| C — Sessie-backlog | 6 | 2 | 14 | 0 | 22 |
| **Totaal** | **33** | **7** | **42** | **1** | **83** |

**~40% van gedocumenteerde items klaar.** De 42 open items zijn de basis voor
de komende maanden — geordend op prioriteit hierboven.

---

## Bron-docs

- `docs/AGENT_SPEC.md` — Fase 1-5 (kern)
- `docs/AGENT_SPEC_EXTENSIONS.md` — Fase 6-10 / extensies §1-13
- `docs/STATUS.md` — actuele stand per sessie
- `docs/CHANGELOG.md` — chronologie van commits
- `docs/COMMERCIAL_PLAN.md` — commerciële/productisatie roadmap (aparte spoor)
- `docs/HYBRID_ARCHITECTURE.md` — hybrid Claude + local model architectuur
- `docs/PRIVACY_LAYER.md` — privacy-gateway design
- `docs/SUB_PROCESSORS.md` — sub-processor register (ISO A.15 / GDPR art 28)

Wanneer een item hier ✅ wordt: pointer in bron-doc bijwerken, plus
STATUS.md entry.
