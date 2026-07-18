# Privacy Layer — Data Routing & PII Redaction

**Doel:** Voorkomen dat gevoelige data (klant-PII, vertrouwelijke bedrijfsinformatie, credentials) naar externe LLM-API's lekt, terwijl de agent wél nuttig blijft.

**Principe:** *Data minimization by default.* Elke byte die de machine verlaat, verlaat hem bewust.

---

## 1. Drielaags model

```
┌────────────────────────────────────────────────────────────┐
│  Laag 1: CLASSIFICATIE (100% lokaal)                       │
│  → Bepaalt gevoeligheidsniveau per item                    │
└────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────┐
│  Laag 2: ROUTING                                           │
│  → Public  → externe LLM (Claude API) toegestaan           │
│  → Internal → redactie vereist vóór externe call           │
│  → Confidential → alleen lokaal model, nooit extern        │
└────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────┐
│  Laag 3: REDACTIE + VERIFICATIE (lokaal)                   │
│  → PII detecteren, vervangen door placeholders             │
│  → Placeholder-mapping lokaal bewaren                      │
│  → Output van LLM reconstrueren met echte waarden          │
└────────────────────────────────────────────────────────────┘
```

---

## 2. Classificatie (Laag 1)

Elk inkomend item (mail, transcript, agenda-event, bericht) krijgt een label:

| Label | Criterium | Voorbeelden |
|---|---|---|
| `public` | Publiek beschikbaar of volstrekt generiek | Nieuwsbrief, openbare calendarlink, Wikipedia-achtige info |
| `internal` | Bedrijfsintern, bevat namen/contacten maar geen bijzondere categorieën | Interne mail, reguliere klantcommunicatie, gespreksnotitie |
| `confidential` | Bijzondere categorieën, NDA, financieel gevoelig, security-gerelateerd | Contractonderhandelingen, salarisinfo, security incidents, auth-tokens, patiëntdata |

**Classificatie-methode (lokaal, in volgorde):**

1. **Hard rules** (regex + keyword):
   - Bevat IBAN, BSN, creditcardnummer, API-key-patroon → direct `confidential`.
   - Afzender-domein op `confidential-domains` whitelist (bijv. jurist, accountant, arts) → `confidential`.
   - Keywords: `salary`, `NDA`, `vertrouwelijk`, `geheim`, `incident`, `breach`, `wachtwoord`, `token`, `cert` (security context) → minimaal `confidential`.
   - Klant-domeinen op VIP-lijst → minimaal `internal`.
2. **Lokaal classificatiemodel** (klein LLM via Ollama, bijv. Llama 3.1 8B of Phi-3) voor grijs gebied.
3. **Default:** bij twijfel → `internal` (nooit naar beneden schalen).

Classificatie wordt **gecached per mail-ID / transcript-ID** zodat dezelfde beslissing niet 10x herberekend wordt.

---

## 3. Routing (Laag 2)

### 3.1 Model-keuze per label

| Label | Welk model | Waar |
|---|---|---|
| `public` | Claude Sonnet 4.x | Anthropic API |
| `internal` | Claude Sonnet 4.x **met redactie** | Anthropic API, gepseudonimiseerd |
| `confidential` | Lokaal model (Llama 3.1 70B via Ollama of Mistral) | On-device, GPU/CPU |

### 3.2 Task-complexity fallback

Niet elke taak hoeft een frontier-model. Simpele classificatie, sentiment, samenvatten van korte stukken → **altijd lokaal model**, ongeacht label. Dat scheelt kosten én dataverkeer.

```python
def choose_model(task_complexity, sensitivity_label):
    if sensitivity_label == "confidential":
        return LOCAL_BIG      # bv. Llama 3.1 70B lokaal
    if task_complexity == "simple":
        return LOCAL_SMALL    # bv. Phi-3 of Llama 3.1 8B
    if sensitivity_label == "internal":
        return CLAUDE_API_WITH_REDACTION
    return CLAUDE_API         # public + complex
```

### 3.3 Hardcoded blocks

Regardless of label, **deze data verlaat nooit de machine**:
- Inhoud van bijlagen gemarkeerd als `vertrouwelijk` of met extensies `.key`, `.pem`, `.pfx`, `.p12`.
- Mails uit `confidential-domains` lijst.
- Alles wat de gebruiker tagt met `#local-only` in iMessage.

---

## 4. Redactie (Laag 3)

Voor `internal` items die naar Claude API gaan, wordt tekst getransformeerd:

### 4.1 Wat wordt geredacteerd

| Categorie | Voorbeeld input | Placeholder |
|---|---|---|
| Persoonsnamen | "Piet de Vries" | `[PERSON_001]` |
| E-mailadressen | "piet@klant.nl" | `[EMAIL_001]` |
| Telefoonnummers | "+31 6 12345678" | `[PHONE_001]` |
| Bedrijfsnamen | "Heineken B.V." | `[ORG_001]` |
| IBAN / rekeningen | "NL91ABNA0417164300" | `[IBAN_001]` |
| Adressen | "Kerkstraat 12, Groningen" | `[ADDRESS_001]` |
| URLs met tokens | "https://...?token=abc" | `[URL_001]` |
| Bedragen >€1000 | "€ 45.000" | `[AMOUNT_001]` |
| Datums in combinatie met namen | "Piet's contract per 1 mei" | `[PERSON_001]`'s contract per `[DATE_001]` |
| Interne projectcodes | "DST-INC-2026-04" | `[PROJECT_001]` |

### 4.2 Detectie-stack (cascade)

1. **Regex-laag** — IBAN, telefoon, email, URL, bedragen: deterministisch, snel.
2. **spaCy NER (NL-model)** — personen, organisaties, locaties: redelijk goed voor Nederlandse tekst.
3. **Presidio (Microsoft, open source)** — volwassen PII-engine, aanvullend op spaCy.
4. **Custom dictionary** — jouw VIP-lijst, projectnamen, interne codes die NER mist.
5. **LLM-review (lokaal)** — klein lokaal model ziet de redacted versie en signaleert "dit ziet er nog steeds als PII uit" — laatste vangnet.

Deze cascade is **additief**: elke laag voegt detections toe, nooit weghalen.

### 4.3 Mapping en reconstructie

Voor elke redacted request:

```python
# Vóór API-call
original = "Piet belt morgen over de Heineken offerte van €45.000"
redacted = "[PERSON_001] belt morgen over de [ORG_001] offerte van [AMOUNT_001]"
mapping = {
    "[PERSON_001]": "Piet",
    "[ORG_001]": "Heineken",
    "[AMOUNT_001]": "€45.000"
}
# mapping wordt lokaal opgeslagen, gekoppeld aan request-ID

# Na API-response
llm_output = "Stel voor om [PERSON_001] morgenochtend te bellen over [ORG_001]."
final = reconstruct(llm_output, mapping)
# → "Stel voor om Piet morgenochtend te bellen over Heineken."
```

**Mapping-opslag:** in-memory tijdens request, optioneel kortstondig gepersisteerd (encrypted SQLite) voor debugging — met automatische purge na 24u.

### 4.4 Consistent pseudonymizen binnen een sessie

Binnen één thread/gesprek moet "Piet" steeds `[PERSON_001]` zijn, niet elke keer een nieuw nummer. Anders verliest het LLM coreferentie. Dus: **stabiele hash per entity binnen conversatie-context**, nieuwe IDs alleen bij nieuwe entities.

---

## 5. Verificatie (pre-flight check)

Vóórdat een payload de machine verlaat, draait een **laatste check**:

1. **Regex-scan** op de redacted output: staat er nog een IBAN/telefoon/email in? → abort, log, fallback naar lokaal model.
2. **Diff-audit** (optioneel, voor `confidential`-gerelateerde flows): toon mij via iMessage wat er verzonden gaat worden, met 1× akkoord-klik. Alleen voor nieuwe flows; zodra een flow vertrouwd is, automatisch door.
3. **Rate-limit op egress** — als meer dan N requests/minuut naar externe API gaan, pauzeer en notify (kan duiden op lekkage-loop).

---

## 6. Audit en transparantie

- **Egress-log**: elke externe API-call krijgt een entry met: timestamp, task-type, sensitivity-label, model, size (bytes in/out), redaction-stats (aantal placeholders per categorie). Géén inhoud.
- **Replay-vriendelijk**: op basis van log kan ik achteraf reconstrueren wélke soort data naar buiten ging, zonder dat de data zelf bewaard hoeft te blijven.
- **Dashboard-weergave**: "Deze week 347 externe calls, 89% `public`, 11% `internal-redacted`, 0% `confidential`." Die laatste 0% is een hard target.

---

## 7. Gebruikerscontroles

Via iMessage of webdashboard kan ik op elk moment:

- `pa privacy status` → huidige policy en statistieken.
- `pa privacy strict` → alleen lokale modellen, zelfs voor public tasks (reisscenario, onzekere netwerken).
- `pa privacy normal` → default routing.
- `pa redact show <request-id>` → laat de laatste redacted payload zien (debugging).
- `#local-only` als hashtag in elk verzoek → forceert lokale verwerking.

---

## 8. Grenzen en eerlijkheid

**Wat deze laag wél doet:**
- Voorkomt onbedoelde lekkage van namen, contactgegevens, bedragen naar externe API.
- Maakt data-flows auditeerbaar — je kunt aantonen wat er gebeurt.
- Biedt een escape-hatch (lokaal model) voor alles wat écht niet naar buiten mag.

**Wat deze laag níet kan garanderen:**
- Semantische privacy: als een mail "onze directeur is ontslagen na het incident vorige week" zegt, dan is die zin zonder namen nog steeds gevoelig.
- 100% detectie: NER mist dingen, vooral ongewone namen, typo's, nieuw jargon.
- Bescherming tegen de provider zelf — Anthropic's enterprise-zero-retention en verwerkersovereenkomst zijn nog steeds onderdeel van je compliance-stack, niet een vervanging.

**Conclusie:** behandel de `confidential`-categorie als *lokaal, punt*. De redactie-laag is voor de `internal`-grijze zone — de meeste zakelijke correspondentie.

---

## 9. Stack-suggestie (concreet)

```
- Regex engine:          eigen module, ~200 regels Python
- NER:                   spaCy 3.x met nl_core_news_lg
- PII framework:         Microsoft Presidio (pip install presidio-analyzer presidio-anonymizer)
- Lokaal klein model:    Ollama + phi3 of llama3.1:8b
- Lokaal groot model:    Ollama + llama3.1:70b (vereist GPU of Mac met genoeg RAM)
- Verwijzingsdict:       SQLite, encrypted via SQLCipher
- Egress-log:            structured logging naar lokale JSON-lines file, dagelijks geroteerd
```

---

## 10. Integratie met AGENT_SPEC

Deze privacy-laag zit **tussen** de orchestrator en elke externe LLM-call. Concreet:

- Vervang in `AGENT_SPEC.md` elke `claude_api.complete(prompt)` door `llm_gateway.complete(prompt, context=item)`.
- `llm_gateway` is de module die classificeert, routeert, redacteert en reconstruct — de rest van de agent hoeft niets van deze laag te weten.
- Implementeer `llm_gateway` als fase 1.5 — tussen foundation en triage — zodat geen enkele latere fase ooit direct naar de externe API schrijft.

---

*Laatst bijgewerkt: 22 april 2026.*
