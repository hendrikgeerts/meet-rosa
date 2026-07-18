# Agent Extensions — Geselecteerde uitbreidingen

Dit document beschrijft de uitbreidingen die bovenop het kernfundament (`AGENT_SPEC.md` + `HYBRID_ARCHITECTURE.md` + `PRIVACY_LAYER.md`) komen. Alleen modules die you expliciet wil — de rest is bewust uitgesloten.

**Context:** start op 15" MacBook Pro 2018 (Intel i9, 32GB RAM) — "Pad A". Migreren naar Mac mini M4 later optioneel.

---

## 1. Voice-in via iMessage

**Doel:** inspreken via iMessage → automatisch verwerkt tot agenda-events, todo's of memo's.

### Flow
1. iMessage-bridge detecteert audio-attachment in inkomend bericht.
2. Audio lokaal opgeslagen in `~/pa-agent/data/voice-in/`.
3. **Whisper** (lokaal, `whisper.cpp` of `faster-whisper`) transcribeert. Model: `medium` als startpunt (goed voor Nederlands, ~1.5GB, draait op CPU met acceptabele snelheid), upgrade naar `large-v3` later.
4. Transcriptie → lokaal model voor **intent parsing**:
   - Is dit een taak? Een agenda-event? Een memo? Een vraag?
   - Eén item of meerdere? ("Morgen Piet bellen **en** vrijdag Hiscox reviewen" = 2 items)
   - Entity extraction (personen, datums, tijden).
5. Eventueel Claude voor complexe parsing (na redactie).
6. Uitvoering: agenda-event, todo, memo vastleggen.
7. Bevestiging via iMessage: "Gedaan — [samenvatting]. Ok?"

### Privacy
- Audio-bestanden **blijven lokaal**, worden na transcriptie en X dagen retentie verwijderd (configureerbaar, default 7 dagen).
- Whisper is 100% lokaal, geen cloud-transcriptie.
- Transcripties doorlopen de reguliere redactie-pipeline voordat Claude iets ziet.

### Intent-voorbeelden
| Gesproken | Geparseerde intent |
|---|---|
| "Morgen 10 uur Piet bellen over SSL" | event: morgen 10:00, 30min, titel: "Piet bellen over SSL" |
| "Zet op mijn lijst dat ik vrijdag Hiscox moet reviewen" | todo: vrijdag, "Hiscox reviewen" |
| "Onthoud dat de Exertis-prijzen 7.5% lager zijn dan AplusK" | memory-fact: persistent opslag |
| "Wat had ik ook alweer afgesproken met Jan vorige week?" | query: search memory + transcripts |

---

## 2. Foto's en screenshots als input

**Doel:** foto sturen → relevante info geëxtraheerd en opgeslagen.

### Flow
1. iMessage ontvangt foto/screenshot.
2. **Eerste keus: on-device OCR** via Apple's Vision-framework (via een kleine Swift helper of `osascript`-wrapper). Gratis, snel, privé.
3. Fallback: Tesseract lokaal als Vision niet beschikbaar is.
4. Geëxtraheerde tekst → lokaal model bepaalt inhoudstype:
   - Visitekaartje → contact toevoegen aan memory
   - Whiteboard → actie-items extraheren
   - Factuur → bedrag/leverancier/datum vastleggen, evt. doorsturen naar todo ("betalen vóór…")
   - Schermafbeelding van mail/document → context-import
5. **Alleen als er geen PII in de foto zit**, mag Claude Vision gebruikt worden voor beter begrip. Default: volledig lokaal.

### Privacy
- Originele foto's lokaal, purgeren na verwerking tenzij als bijlage aan een memory-item gehecht.
- OCR-output doorloopt de redactielaag.
- Expliciete opt-in per keer voor Claude Vision (via iMessage-bevestiging: "Deze foto lokaal verwerken of mag Claude meekijken voor een beter resultaat?").

---

## 3. Mobiele voice-memo via iOS Shortcut

**Doel:** onderweg één druk op de knop, spreek in, klaar.

### Implementatie
- iOS Shortcut (handmatig door you te installeren): "Record audio → POST naar `https://<lokale-bridge>.tailscale.net/voice-memo` met auth-token".
- Voor bereikbaarheid zonder poorten open te zetten: **Tailscale** (of vergelijkbaar mesh-VPN) tussen iPhone en Mac. Veilig, geen publieke endpoints nodig.
- De Mac ontvangt de audio via een lokale HTTP-endpoint, verwerkt via dezelfde pipeline als voice-in van iMessage.
- Bevestiging via iMessage of push-notification: "Memo ontvangen: [samenvatting]".

### Waarom Tailscale
- Geen router-config nodig, werkt automatisch waar je iPhone ook is.
- End-to-end encrypted (WireGuard).
- Gratis voor persoonlijk gebruik.

---

## 4. Eigen SQLite-todolist

**Doel:** totale controle, geen externe dependency.

### Schema (essentie)
```sql
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  due_at TIMESTAMP,
  completed_at TIMESTAMP,
  status TEXT CHECK(status IN ('open','doing','done','cancelled')) DEFAULT 'open',
  priority INTEGER DEFAULT 3,            -- 1=hoog, 5=laag
  project_id INTEGER REFERENCES projects(id),
  source TEXT,                           -- 'voice', 'mail', 'manual', 'meeting'
  source_ref TEXT,                       -- mail-id, meeting-id, etc.
  parent_id INTEGER REFERENCES tasks(id),
  metadata JSON
);

CREATE TABLE projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  status TEXT DEFAULT 'active',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Interactie via iMessage
- `todo` → toont openstaande top-5 op prioriteit/deadline.
- `todo week` → alles voor deze week.
- `doe 12` → taak 12 op status 'doing'.
- `klaar 12` → taak 12 op 'done'.
- `taak: X voor vrijdag` → natuurlijke toevoeging.
- Agent voegt proactief taken toe vanuit mails/meetings/voice-memos, altijd met bron-referentie.

### Webdashboard
- Eenvoudige weergave voor bulk-review (drag/drop-prioriteit, project assignen, etc.).
- Lokale webservice, alleen bereikbaar op jouw netwerk.

---

## 5. Projectcontext (dossiers)

**Doel:** verbind alles rond één onderwerp.

### Concept
Projecten zijn de organiserende eenheid. Elke mail, meeting, taak, voice-memo, beslissing en bestand kan aan een project gehangen worden.

### Automatische koppeling
- Het lokale model classificeert nieuwe items op projectmatch, op basis van:
  - Keywords uit de projectbeschrijving.
  - Betrokken personen (via de mapping).
  - Eerdere gelabelde items (actieve learning).
- Onzekere matches → gepresenteerd als voorstel: "Deze mail lijkt bij *SSL-migratie* te horen, klopt dat?" (Y/N via iMessage).

### Queries
- "Toon alles over SSL-migratie" → tijdlijn van mails, meetings, taken, beslissingen.
- "Wat is de status van project X?" → agent vat samen.
- "Wie zijn de stakeholders in project Y?" → lijst met laatste contactmoment.

### Opslag
Eén `project_items` join-tabel die alles verbindt. Vector-store indexeert per project voor semantisch zoeken.

---

## 6. Beslissingslogboek

**Doel:** "Waarom hebben we ook alweer besloten om X?" beantwoordbaar maken.

### Trigger
- Expliciet: "Besluit: we gaan door met Exertis, niet AplusK. Reden: 7.5% goedkoper." → `decisions` tabel.
- Semi-automatisch: lokaal model detecteert beslissingsachtige zinnen in meeting-transcripts en mails → vraagt bevestiging: "Dit klonk als een besluit — vastleggen?" (Y/N).

### Schema
```sql
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  decision TEXT NOT NULL,
  rationale TEXT,
  alternatives_considered TEXT,
  project_id INTEGER REFERENCES projects(id),
  source_ref TEXT,                   -- mail/meeting/memo
  superseded_by INTEGER REFERENCES decisions(id),  -- voor heroverwegingen
  tags TEXT                          -- comma-separated
);
```

### Query
"Waarom kozen we niet voor AplusK?" → agent zoekt in logboek + relevante context, antwoordt met beslissing + datum + rationale + alternatieven.

---

## 7. Kennisbank over eigen bedrijf (interne RAG)

**Doel:** agent kan putten uit jouw documentatie, productinfo, eerdere offertes, mails.

### Bronnen
- Bestanden uit een lokale map (bv. `~/rosa-knowledge/`): documentatie, presentaties, productspecs van your product.
- Eerder verwerkte mails en meetings (al in memory).
- Optioneel: een gekoppelde map uit iCloud/Dropbox (read-only).

### Stack
- Embedding-model: **lokaal** via Ollama (bv. `nomic-embed-text` of `mxbai-embed-large`) — embeddings bevatten semantische info, zou ik ook niet extern willen.
- Vector store: `sqlite-vec` of Chroma (lokaal).
- Re-rank stap: optioneel lokaal met een kleine cross-encoder voor betere resultaten.

### Gebruik
- Bij conceptmails: agent haalt relevante context op ("vergelijkbare offerte uit 2025 stond op X euro, standaard clausule Y is gebruikelijk").
- Bij onboarding-vragen van resellers (al zei je dat je reseller-support niet wilt als feature — deze kennisbank kan nog steeds jouw eigen antwoorden versnellen als je die mails zelf doet).
- Bij "Wat hebben we hierover eerder afgesproken?" queries.

### Privacy
- Embeddings voor je kennisbank **blijven lokaal**.
- Bij een Claude-call voor een conceptmail mogen alleen de top-N geredacteerde snippets mee, niet het hele document.
- Documenten die als `confidential` zijn getagd, komen alleen in de index voor lokale retrieval.

---

## 8. Patroonherkenning en energie-management

**Doel:** "Je werkt het best tussen 9 en 11, meetings daarin zijn suboptimaal."

### Signalen die worden getracked
- Tijdstippen van mail-replies (wanneer ben je responsive?).
- Duur tussen binnenkomst en behandeling per mail-prioriteit.
- Meeting-dichtheid per dagdeel.
- Focus-blokken: periodes zonder agenda-events en zonder mail-activiteit.
- Zelf-gerapporteerd: "Hoe was je dag?" (schaal 1-5 in de dagafsluiting) → correlatie met meeting-/mail-patterns over tijd.

### Output
- Maandelijks (of op verzoek): een **patroonrapport**. Niet prescriptief — observerend. "Deze maand 27% minder focustijd dan vorige. Dinsdagen waren het slechtst (6 meetings gemiddeld)."
- Bij planning: agent waarschuwt bij conflicten met gedetecteerde patronen. "Je wilt deze meeting op woensdag 10:00 — dat is historisch je beste focus-tijd. Zeker weten?"

### Privacy
- Al deze data blijft lokaal (eigenlijk al het geval voor alle memory).
- Geen aggregatie naar externe services.

---

## 9. Cognitive debt bewaking

**Doel:** signaleer overbelasting aan mentale open loops.

### Metrics
- Aantal open mails met `actie_vereist` > 3 dagen oud.
- Aantal openstaande todo's zonder deadline.
- Aantal todo's met deadline in het verleden (overdue).
- Aantal open threads waar jij aan zet bent.
- Aantal reminders in de "follow-up radar" > 7 dagen.

### Drempels (configureerbaar)
- Normaal: 0-15 open loops totaal.
- Oplopend: 15-30 → zachte signalering in dagafsluiting.
- Overload: >30 → aparte iMessage met voorstel tot opschonen: "Je hebt 47 open loops. Ik kan 10 voorstellen om te archiveren/delegeren/sluiten — wil je ze zien?"

### Opschonings-assistent
- Agent presenteert batches van 5 items met voorstel: archiveren / delegeren / deadline verzetten / mail sturen / doen.
- Jij zegt bulk-ja of individueel ja/nee. Hele opschoning duurt idealiter 3-5 minuten.

---

## 10. Zaterdag-weekreview

**Doel:** wekelijks moment van reflectie en planning, zonder druk.

### Tijdstip
Zaterdag 10:00 (configureerbaar). Geen briefing-toon, meer een check-in.

### Structuur
```
Zaterdag-review — week 17

Gelukt:
  • 12 mails beantwoord, 3 offertes uit
  • SSL cert40 migratie afgerond (7 dagen eerder dan deadline)
  • Plaud-calls: 6 verwerkt

Niet gelukt / schuift:
  • Hiscox-review (nu 2 weken open)
  • Power BI-onderzoek (3 weken open)

Patronen die ik zag:
  • Je was 4× na 22:00 nog mails aan het beantwoorden. Opvallend.
  • Woensdag had 2 ongeplande conference-calls — iets om op te letten?

Volgende week:
  • 8 events gepland, woensdag is vol (5 meetings)
  • 2 deadlines: Hiscox (ma), klant-X offerte (do)
  • Focus-blok dinsdag vanaf 13:00 nog vrij

Vragen aan jou:
  • Wil je Hiscox deze week echt afronden of doorschuiven?
  • Woensdag rustiger maken?
```

### Interactie
- Niet alleen lezen: agent verwacht korte reacties ("ja Hiscox maandag", "woensdag 15:00 naar donderdag"), voert ze uit.
- Output van review → samenvatting opgeslagen in memory, voedt volgende reviews.

---

## 11. Voorspellend: terugkerende patronen

**Doel:** anticiperen op bekende cycli.

### Detectie
- Lokaal model scant (eens per week) de historie op terugkerende events/taken/klachten:
  - "Eind van elk kwartaal stijging in reseller-vragen."
  - "Rond de 15e van de maand komen meestal factuur-gerelateerde mails."
  - "Elke 3 maanden contract-renewal met [leverancier]."
- Geformaliseerd als `patterns` tabel met periodiciteit en volgende verwachte voorkomen.

### Actie
- 1-2 weken vóór verwacht voorkomen: proactieve briefing. "Historisch krijg je rond deze tijd contract-vragen van leverancier X. Wil je vóórbereid zijn?"
- Niet als reminder maar als *context* — je kunt besluiten te negeren.

### Privacy
- Pattern-analyse is pure tijdreeks-analyse op lokale data. Geen cloud nodig. Voor rationalisatie / verwoording kan Claude (na redactie) wel helpen.

---

## 12. Research-taken (deep search workflow)

**Doel:** "Zoek uit hoe [concurrent/onderwerp] werkt" → gestructureerd rapport.

### Trigger
Via iMessage of webinterface: `research: hoe structureert concurrent X hun licentiemodel?`

### Flow
1. Agent leest het verzoek, maakt een onderzoeksplan (deelvragen).
2. Web-search (via Claude's web search tool of via een eigen zoek-endpoint — afhankelijk van hoeveel je extern wilt trekken).
3. Per bron: ophalen, samenvatten, relevante stukken extracten.
4. Synthetiseren tot een antwoord: hoofdbevindingen, bronnen, open vragen.
5. Resultaat als Markdown-document in `~/pa-agent/research/` + korte samenvatting via iMessage.

### Uitvoering
- Dit draait het beste via **Claude met web search** — het is een context-waar-publiek-web-info-wordt-opgehaald. Jouw vraag is meestal niet zelf gevoelig (concurrent X is geen geheim).
- Als het verzoek gevoelige context bevat ("voor onze pitch aan Y") → redacteer die context weg voordat het naar Claude gaat; de query zelf is algemeen.

### Output-kwaliteit
- Structuur: samenvatting → bevindingen → bronnen → methodologie → open vragen.
- Bewaakt: geen citaten >15 woorden, parafrasering, bronvermelding per claim.

---

## 13. Delegeer-tracker

**Doel:** "Ik heb Piet gevraagd X — is dat gebeurd?"

### Trigger — drie manieren
1. **Expliciet via iMessage**: "Ik heb Piet gevraagd om offerte vóór dinsdag. Herinner me als ik niks hoor."
2. **Automatisch uit uitgaande mail**: jij stuurt een mail met een vraag/verzoek → lokaal model detecteert "delegatie" → vraagt bevestiging "Zal ik deze opvolgen als delegatie?".
3. **Automatisch uit meeting**: Plaud-transcript bevat "Piet zou X doen" → delegation-item voorgesteld.

### Opvolging
- Default deadline: 7 dagen (configureerbaar per delegatie, en per persoon — sommige mensen zijn sneller dan anderen).
- Agent checkt inkomende mail op match met delegatie (onderwerp/persoon/keywords).
- Bij match: automatisch afvinken en melden. "Piet antwoordde op offerte-vraag — status: afgerond."
- Bij geen match op deadline: iMessage. "Piet had dinsdag een offerte zouden sturen, nog niets ontvangen. Reminder sturen?"

### Schema
```sql
CREATE TABLE delegations (
  id INTEGER PRIMARY KEY,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  delegated_to TEXT NOT NULL,         -- persoon/ref
  what TEXT NOT NULL,
  expected_by TIMESTAMP,
  source_ref TEXT,                    -- mail/meeting/voice
  status TEXT DEFAULT 'open',         -- open/fulfilled/overdue/cancelled
  fulfilled_at TIMESTAMP,
  fulfilled_ref TEXT,                 -- mail die het bevestigt
  project_id INTEGER REFERENCES projects(id)
);
```

### Privacy
- Bij matching van inkomende mail tegen delegaties: volledig lokaal (keyword + embedding-match).

---

## 14. Bijgewerkte fasering

Bovenop de bestaande vijf fases uit `AGENT_SPEC.md`, komt:

### Fase 6 — Voice & vision (week 9-10)
- Whisper lokaal + intent parsing.
- iMessage voice-in end-to-end.
- iOS Shortcut + Tailscale bridge.
- Foto/screenshot via Apple Vision.

### Fase 7 — Taken + projecten (week 11-12)
- SQLite-todolist met iMessage-interface.
- Project-model + automatische koppeling.
- Webdashboard voor bulk-review.

### Fase 8 — Geheugen-lagen (week 13-14)
- Beslissingslogboek.
- Kennisbank / RAG over eigen documenten.
- Cognitive-debt tracking.

### Fase 9 — Proactieve lagen (week 15-17)
- Patroonherkenning + energie-management.
- Zaterdag-weekreview.
- Voorspellend op terugkerende patronen.

### Fase 10 — Autonome acties (week 18-20)
- Research-taken workflow.
- Delegeer-tracker volledig.

Dit is natuurlijk een schatting — werkelijke tijdlijn hangt af van hoeveel je er per week in kunt stoppen samen met Claude Code.

---

## 15. Instructies voor Claude Code

- Bouw elke extensie als losse module met eigen interface. Kernfundament mag niet afhankelijk zijn van een extensie.
- Elke extensie heeft een eigen `README.md` met: doel, dependencies, config-keys, privacy-implicaties, testscenario's.
- Feature-flags in `settings.yaml` per extensie, zodat ze individueel aan/uit kunnen.
- Documenteer voor elke nieuwe externe call (Claude, web search) welk sensitivity-niveau nodig is en welke redactie wordt toegepast — deze documentatie leeft naast de code, niet alleen erin.

---

*Laatst bijgewerkt: 22 april 2026.*
