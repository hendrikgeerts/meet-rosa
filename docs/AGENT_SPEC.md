# Personal Assistant Agent — Specificatie

**Eigenaar:** you Geerts
**Doel:** Een persoonlijke AI-agent die als volwaardige Personal Assistant fungeert — proactief, betrouwbaar en geïntegreerd met mijn dagelijkse tooling.
**Taal:** Nederlands (primair), Engels waar technisch gebruikelijk.
**Deployment context:** Lokaal/self-hosted waar mogelijk, met oog voor privacy en GDPR (ik ben Information Security Officer — privacy is geen afterthought).

---

## 1. Kernfilosofie

De agent is **geen chatbot die wacht op commando's**, maar een proactieve assistent die:

1. **Context vasthoudt** over dagen, weken en projecten heen (persistent memory).
2. **Initiatief neemt** — stuurt ongevraagd briefings, reminders, signaleert conflicten.
3. **Meelezer is** in mijn mail, agenda en gespreksopnames — destilleert eruit wat actie vereist.
4. **Vertrouwelijk werkt** — geen data naar externe LLM's zonder expliciete whitelist; gevoelige klantdata blijft lokaal.
5. **Mij niet in de weg zit** — interrupties zijn gebundeld (digest), niet real-time spam.

---

## 2. Rol en persona

- Spreekt mij aan met "you", tutoyeert, nuchtere toon.
- Geen overdreven enthousiasme, geen disclaimers, geen "ik ben maar een AI".
- Denkt mee als een stafmedewerker zou doen: stelt vragen terug als iets onduidelijk is, weigert onhaalbare verzoeken met alternatief.
- Vertrouwd met mijn werkcontext: YourClient / your industry / your product, digital signage, ISO 27001, Dutch B2B enterprise markt.

---

## 3. Data-integraties (inputs)

### 3.1 E-mail
- **Gmail** (OAuth2, Google API)
- **Outlook / Microsoft 365** (Microsoft Graph API)
- **Generiek IMAP** (voor overige accounts — YourClient mailbox, reseller-aliassen, etc.)

**Functionaliteit per mailbron:**
- Ongelezen mails ophalen, threaden, bijlagen indexeren.
- Per mail classificeren: `actie_vereist` / `informatief` / `nieuwsbrief` / `spam-achtig` / `persoonlijk`.
- VIP-afzenders markeren (klanten, partners, directe collega's — whitelist).
- Concept-antwoorden genereren in mijn stijl (zakelijk, beknopt, NL).
- "Follow-up nodig" flaggen — mails waar ik 3+ dagen geen antwoord op gaf.

### 3.2 Agenda
- **Google Calendar** (primaire agenda).
- Event-metadata begrijpen: locatie, deelnemers, Teams/Meet-links, bijgevoegde docs.
- Conflictdetectie, reistijd-inschatting (vanuit Groningen/NL context), voorbereidingstijd blokken.

### 3.3 Gespreksopnames
- **Plaud Pro / Plaud Note** — transcripties en samenvattingen van gesprekken.
- Agent haalt transcripts op (via Plaud API/export), koppelt ze aan:
  - Agenda-event (match op tijdstip + deelnemers).
  - Contactpersoon / project.
  - Extracteert: **besluiten**, **actiepunten**, **toezeggingen van mij**, **toezeggingen van anderen**, **openstaande vragen**.

### 3.4 Taken / notities (optioneel, fase 2)
- Todoist / Things / Apple Reminders integratie — afhankelijk van wat ik gebruik.
- Markdown-notes (bijv. Obsidian-vault) read-only indexeren voor context.

---

## 4. Communicatiekanalen (outputs / interactie)

### 4.1 Primair: iMessage
- Agent stuurt en ontvangt iMessage berichten via een Mac-bridge (bijv. een lokale daemon op mijn Mac die via AppleScript/`osascript` + een SQLite-watch op `chat.db` werkt, of een kant-en-klare oplossing zoals BlueBubbles server).
- **Vereisten voor iMessage-laag:**
  - Inkomend: nieuwe berichten detecteren → doorsturen naar agent als user-message.
  - Uitgaand: agent stuurt naar mijn eigen nummer of een dedicated thread "PA".
  - Ondersteuning voor media (voice notes die de agent transcribeert → actiepunten).
- **Fallback kanaal:** e-mail (naar mezelf), voor als iMessage down is of voor lange briefings.

### 4.2 Secundair
- Webinterface (lokale dashboard) voor review van acties, memory-inzage, instellingen.
- Optioneel: Telegram bot als backup messaging-kanaal.

---

## 5. Kernfuncties

### 5.1 Dagopstart-briefing (ochtendritueel)
Elke werkdag rond **07:30** stuurt de agent een briefing via iMessage:

```
Goedemorgen you.

📅 Vandaag (3 items):
  09:00 — Call reseller Exertis (Teams, 30 min) — voorbereiding: prijslijst Q2 klaarzetten
  11:30 — Standup your product
  15:00 — Prospect NorthSea Signage (Groningen, kantoor) — reistijd 10 min

📧 Mail-prioriteit (4 items actie):
  • Klant Heineken — vraagt offerte-uitbreiding (3 dagen open)
  • Hiscox — polisrenewal, deadline vrijdag
  • Reseller DACH — technische vraag SignageOS
  • Microsoft — Graph API quota warning

⚠️ Openstaand uit gisteren:
  • Je zou Piet bellen over de SSL cert40 migratie
  • Concept verwerkersovereenkomst reseller-X wacht op jouw review

🌤 Weer: 8°C, bewolkt — regen rond 14:00
```

### 5.2 Dagafsluiting (avond)
Rond **18:00** of op iMessage-trigger "klaar":

```
Dagafsluiting you.

✅ Wat er gebeurde:
  • 7 mails beantwoord, 3 concepten voor jou klaar
  • Call Exertis: afspraak prijsherziening 1 mei (genotuleerd)
  • Plaud-opname 14:20 verwerkt → 4 actiepunten toegevoegd

📌 Voor morgen:
  • Offerte Heineken afmaken (2 uur geblokt 09:30)
  • Hiscox reviewen vóór vrijdag

🧠 Onthouden:
  • Exertis-contactpersoon heet nu Mark Jansen (was Ruud)
  • cert40 deadline verschoven naar 8 mei

Slaap lekker. 🌙
```

### 5.3 Proactieve signalering
- **Conflictdetectie:** dubbele meetings, te krappe reistijd, ontbrekende voorbereiding.
- **Deadline-bewaking:** mails/taken met impliciete deadlines ("graag vóór vrijdag") → reminder.
- **Follow-up radar:** "Je wacht 5 dagen op antwoord van X — zal ik een nudge sturen?"
- **Meeting prep:** 30 min vóór afspraak een korte brief met context, laatste mail-thread met deelnemer, gespreksnotities uit vorige ontmoeting.

### 5.4 Post-meeting verwerking
Wanneer Plaud een nieuwe opname oplevert:
1. Transcript ophalen → agent leest mee.
2. Samenvatting + actiepunten genereren.
3. Agenda-event enrichen (notitie bijvoegen).
4. Actiepunten van mij → takenlijst.
5. Actiepunten van anderen → follow-up radar (reminder na X dagen).
6. Mij samenvatting sturen via iMessage ("Call verwerkt, 3 actiepunten voor jou, 2 voor Piet — review?").

### 5.5 E-mail triage en concept-antwoorden
- Inbox classificeren zoals beschreven in §3.1.
- Voor elke "actie_vereist": conceptreactie genereren in mijn stijl.
- Concepten als **draft in Gmail/Outlook** opslaan (niet autoverzenden).
- Per iMessage-batch (2× daags): "5 drafts klaar, wil je ze zien?" → ik review via webinterface of iMessage quick-reply.

### 5.6 Memory & kennisbank
- **Korte termijn (context):** actieve gesprekken, open threads, huidige week.
- **Lange termijn (facts):** personen, bedrijven, projecten, voorkeuren, eerder genomen besluiten.
- Opslag: lokale vector-store (bijv. Chroma/Qdrant) + structured store (SQLite/Postgres) voor facts.
- **Promptbaar:** "Onthoud dat X" / "Vergeet Y" / "Wat weet je over prospect Z?"
- **Automatisch:** na elke meeting/mail destillatie van nieuwe feiten → review-queue voordat het in long-term memory landt.

### 5.7 Reminder-engine
- Natural language: "Herinner me morgen 10u aan Piet bellen."
- Context-gebonden: "Als ik in Amsterdam ben, herinner me aan X."
- Herhalend: "Elke maandag check cert-status."
- Levering via iMessage op het juiste moment.

### 5.8 Delegatie en uitvoering
De agent mag **zelfstandig uitvoeren** binnen een vooraf afgesproken scope:
- Afspraken inplannen waar beide partijen via mail onderhandelen (Calendly-achtig, maar inbox-native).
- Simpele bevestigingen sturen ("Ontvangen, kom er morgen op terug").
- Mails archiveren / labelen die duidelijk informatief zijn.

Alles daarbuiten: **draft + approval via iMessage** vóór verzenden.

---

## 6. Architectuur (voorstel)

```
┌─────────────────────────────────────────────────────────────┐
│                    PA Agent Core                            │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────┐   │
│  │ Orchestrator│  │   Memory    │  │  Scheduler / Cron  │   │
│  │  (LLM loop) │  │ (vec + sql) │  │ (briefings, polls) │   │
│  └─────────────┘  └─────────────┘  └────────────────────┘   │
└──────┬───────────────────┬────────────────────┬─────────────┘
       │                   │                    │
 ┌─────▼─────┐      ┌──────▼──────┐      ┌──────▼──────┐
 │  Inputs   │      │   Tools     │      │   Outputs   │
 │ Gmail     │      │ Draft email │      │ iMessage    │
 │ Outlook   │      │ Calendar op │      │ Email       │
 │ IMAP      │      │ Task create │      │ Web UI      │
 │ GCal      │      │ Web search  │      │             │
 │ Plaud     │      │ File ops    │      │             │
 │ iMessage  │      │             │      │             │
 └───────────┘      └─────────────┘      └─────────────┘
```

**Stacksuggestie (pas aan wat jou past):**
- **Runtime:** Python 3.12 (FastAPI als service-laag) of TypeScript (Node, als je Claude Code in TS draait).
- **LLM:** Claude Sonnet 4.x voor reasoning; lokaal model (bijv. via Ollama) voor simpele classificatietaken om kosten te drukken.
- **Orchestration:** eigen loop of LangGraph/Mastra; ik leun liever naar een simpele, leesbare eigen orchestrator dan een zwaar framework.
- **Memory:** SQLite + sqlite-vec (of Chroma) — lokaal, geen cloud.
- **Queues:** Redis of simpele SQLite-based job queue.
- **Secrets:** `.env` + OS keychain voor OAuth refresh tokens.
- **Deploy:** Docker Compose op een eigen VPS of NAS; iMessage-bridge blijft noodzakelijkerwijs op een Mac.

---

## 7. Security & privacy

- Alle OAuth-tokens versleuteld at rest.
- Geen mail-body's naar externe LLM zonder **on-device pre-filter** die PII/klantdata kan redacten als beleid dat vereist.
- Audit-log: elke outbound actie (verzonden mail, agenda-wijziging) loggen met timestamp + trigger.
- **Kill-switch:** één commando (`pa stop`) dat alle outbound-acties bevriest tot verdere instructie.
- Rechten-model: de agent heeft **read-only** op default; write-acties expliciet per tool whitelisten.
- GDPR: datasubject requests moeten afhandelbaar zijn — d.w.z. per persoon kunnen we alle memory-entries ophalen en verwijderen.

---

## 8. Fasering (roadmap voor Claude Code)

### Fase 1 — Foundation (week 1-2)
- [ ] Project skeleton, config-laag, secrets management.
- [ ] Gmail + Google Calendar read-only integratie, werkend dag-overzicht in CLI.
- [ ] iMessage-bridge proof-of-concept: ontvangen + sturen via eigen nummer.
- [ ] Minimale orchestrator-loop met Claude Sonnet.

### Fase 2 — Triage & memory (week 3-4)
- [ ] Mail-classificatie en draft-generatie (Gmail write).
- [ ] Memory-laag (SQLite + vec), fact-extractie met review-queue.
- [ ] Dagopstart-briefing via iMessage.

### Fase 3 — Plaud + meetings (week 5-6)
- [ ] Plaud-integratie: transcripts ophalen, koppelen aan agenda.
- [ ] Post-meeting verwerking end-to-end.
- [ ] Meeting-prep brief 30 min vóór event.

### Fase 4 — Outlook + IMAP + polish (week 7-8)
- [ ] Outlook via Graph API, generieke IMAP-client.
- [ ] Follow-up radar, reminder-engine.
- [ ] Webdashboard voor review en instellingen.

### Fase 5 — Autonomie (doorlopend)
- [ ] Gecontroleerd uitbreiden van zelfstandig-uitvoeren-scope.
- [ ] Evaluatie-metriek: hoeveel tijd bespaart het écht?

---

## 9. Definition of Done (MVP)

De agent is "in productie voor mezelf" wanneer:
1. Ik ontvang elke werkdag een ochtendbriefing die ik daadwerkelijk gebruik.
2. Minstens 50% van mijn mails krijgt een bruikbaar conceptantwoord.
3. Plaud-opnames worden binnen 15 min na upload automatisch verwerkt.
4. Ik heb in 7 dagen op rij geen gemiste meeting-voorbereiding gehad.
5. Ik kan via iMessage natuurlijk met de agent praten zonder commando-syntax.

---

## 10. Instructies voor Claude Code

- Begin met **Fase 1**. Lever werkende increments — geen grote upfront-ontwerpen.
- Schrijf code modulair: elke integratie (Gmail, GCal, Plaud, iMessage) als losse module met duidelijke interface.
- Gebruik **typed interfaces** — ik wil dat uitbreiden voorspelbaar is.
- Schrijf tests voor de orchestrator-logica; integraties mogen met mock-fixtures.
- Commit vaak, kleine PR-achtige commits met heldere messages in het Nederlands of Engels (consistent per project).
- Als iets onduidelijk is in deze spec: **vraag terug via een TODO-comment of chat**, verzin geen aannames over mijn tooling.
- Documenteer elke module met een korte `README.md`: waarvoor, hoe in te stellen, welke secrets nodig.

---

*Laatst bijgewerkt: 22 april 2026.*
