# Hybrid Architecture — Lokale data, externe reasoning

**Principe:** *Echte data blijft lokaal. Claude ziet alleen abstracties. Reconstructie gebeurt on-device vóór output.*

Dit is de werkende architectuur voor de PA agent. Het is een concretisering van `PRIVACY_LAYER.md` met duidelijke stappen en voorbeelden.

---

## 1. De flow in één plaatje

```
┌─────────────────────────────────────────────────────────────────┐
│                         LOKAAL (Mac / server)                   │
│                                                                 │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────────┐         │
│  │ Inputs   │──▶│ Lokaal model │──▶│  Redactor        │         │
│  │ Gmail    │   │ (Ollama)     │   │  (spaCy+Presidio │         │
│  │ GCal     │   │              │   │   +custom dict)  │         │
│  │ Plaud    │   │ • extractie  │   │                  │         │
│  │ iMessage │   │ • classific. │   │ → placeholders   │         │
│  └──────────┘   │ • triage     │   │ → mapping store  │         │
│                 └──────────────┘   └────────┬─────────┘         │
│                                             │                   │
│                                             │ geanonimiseerd    │
│                                             ▼                   │
│                                    ┌─────────────────┐          │
│                                    │  LLM Gateway    │          │
│                                    │  • route        │          │
│                                    │  • pre-flight   │          │
│                                    │    scan         │          │
│                                    └────────┬────────┘          │
└─────────────────────────────────────────────┼───────────────────┘
                                              │ HTTPS
                                              ▼
                               ┌───────────────────────────┐
                               │      Claude API           │
                               │  (ziet alleen tokens      │
                               │   + placeholders)         │
                               └──────────────┬────────────┘
                                              │
┌─────────────────────────────────────────────┼───────────────────┐
│                                             ▼                   │
│                                    ┌────────────────┐           │
│                                    │ Reconstructor  │           │
│                                    │ vul mapping    │           │
│                                    │ terug in       │           │
│                                    └───────┬────────┘           │
│                                            │                    │
│                                            ▼                    │
│                                    ┌────────────────┐           │
│                                    │ iMessage bridge│──▶ 📱     │
│                                    │ (lokaal, Mac)  │           │
│                                    └────────────────┘           │
│                         LOKAAL (Mac / server)                   │
└─────────────────────────────────────────────────────────────────┘
```

**De essentie:** Claude krijgt nooit een echt naam, e-mailadres, bedrag of bedrijfsnaam te zien. Jij ziet in je iMessage wél gewoon "Piet" en "Heineken" — omdat de reconstructie tussen Claude's response en jouw scherm gebeurt, allemaal op jouw hardware.

---

## 2. Rolverdeling: wie doet wat?

### Lokaal model (Ollama, bv. Llama 3.1 8B of 70B)
Werkt mét echte data. Taken waar het goed genoeg in is:

- **Extractie**: uit een mail de velden halen (afzender, intent, deadline, gevraagde actie).
- **Classificatie**: sensitivity-label, mail-categorie, urgentie.
- **Korte samenvatting**: 1-2 zinnen over waar de mail over gaat.
- **Redactie-ondersteuning**: vangnet-check of er nog PII in de geanonimiseerde tekst zit.
- **Simpele concepten**: "Ontvangen, kom er morgen op terug" — standaardantwoorden.

### Claude API
Werkt op geanonimiseerde input. Voor:

- **Complex redeneren**: "Gegeven deze 12 mails en deze 3 meetings, wat is de beste volgorde om vandaag aan te pakken?"
- **Goede conceptantwoorden**: langere, genuanceerde, toonvaste mails.
- **Synthese**: briefing-teksten samenstellen, conflicten uitleggen, prioriteitsadviezen.
- **Multi-step planning**: "Onderhandel deze afspraak via mail" — meerdere stappen vooruit denken.

### Reconstructor + iMessage-bridge
Puur lokaal. Geen model, geen externe calls. Gewoon string-vervanging op basis van de mapping, en AppleScript/bridge naar Messages.

---

## 3. Een concrete flow uitgewerkt: ochtendbriefing

**Stap 1 — Data ophalen (lokaal):**
- Gmail/Outlook/IMAP: 23 nieuwe mails sinds gisteren.
- GCal: 3 events vandaag.
- Plaud: 1 transcript van gister 16:30.
- Openstaande reminders: 2.

**Stap 2 — Lokaal model leest alles (echte data, geen network):**

Per mail extraheert lokaal model gestructureerd:
```json
{
  "mail_id": "m_4821",
  "from": "piet.devries@heineken.com",
  "from_name": "Piet de Vries",
  "org": "Heineken",
  "intent": "offerte_uitbreiding",
  "urgency": "medium",
  "deadline_implied": "2026-04-25",
  "sentiment": "positief_zakelijk",
  "summary_local": "Piet vraagt of we de offerte kunnen uitbreiden met 20 extra schermen."
}
```

Deze JSON-records blijven lokaal. Niets hiervan gaat naar Claude.

**Stap 3 — Redactor maakt een Claude-veilige versie:**

```json
{
  "mail_id": "m_4821",
  "from": "[EMAIL_001]",
  "from_name": "[PERSON_001]",
  "org": "[ORG_001]",
  "intent": "offerte_uitbreiding",
  "urgency": "medium",
  "deadline_implied": "2026-04-25",
  "summary_local": "[PERSON_001] vraagt of we de offerte kunnen uitbreiden met 20 extra schermen."
}
```

Mapping (lokaal, encrypted):
```
[EMAIL_001]  → piet.devries@heineken.com
[PERSON_001] → Piet de Vries
[ORG_001]    → Heineken
```

**Stap 4 — Claude krijgt de prompt:**

> "Gegeven deze 23 geanonimiseerde mailrecords, deze 3 agenda-events en deze 2 open reminders: stel een dagbriefing samen voor you in het Nederlands. Prioriteer op urgentie en impact. Max 200 woorden."

Claude ziet dus:
> "[PERSON_001] vraagt via [EMAIL_001] of [ORG_001] de offerte kan uitbreiden..."

En produceert iets als:
> "[ORG_001] (contact: [PERSON_001]) vraagt om offerte-uitbreiding met 20 schermen — deadline [DATE_003]. Gezien hun historie: kansrijk, advies om vóór [DATE_002] te reageren."

**Stap 5 — Reconstructor vult terug:**
> "Heineken (contact: Piet de Vries) vraagt om offerte-uitbreiding met 20 schermen — deadline vrijdag. Gezien hun historie: kansrijk, advies om vóór donderdag te reageren."

**Stap 6 — iMessage:**
Lokale bridge stuurt het naar jouw telefoon. Geen cloud-messaging in het pad.

---

## 4. Waarom dit werkt (en waar het niet werkt)

### Waarom het werkt
- **Claude is heel goed in redeneren over gestructureerde abstracties.** Het heeft niet per se "Heineken" nodig om te begrijpen dat `[ORG_001]` een belangrijk klant is, als je het label erbij levert (`vip_customer: true`, `revenue_tier: "A"`).
- **Coreferentie blijft intact** doordat dezelfde entity binnen een request steeds dezelfde placeholder krijgt. Claude kan dus "Piet wil X, stuur Piet een reactie" perfect redeneren als `[PERSON_001] wil X, stuur [PERSON_001] een reactie`.
- **Lokaal model hoeft niet briljant te zijn** voor z'n taken (extractie, classificatie). Llama 3.1 8B is daar al prima in voor Nederlands, vooral met een paar shots in de prompt.

### Waar het vastloopt
Drie scenario's waar pure redactie tekort schiet:

**1. Tone-sensitief schrijven**
Als je Claude vraagt een mail te schrijven *in de stijl die past bij de relatie met deze persoon*, dan heeft Claude context nodig die je misschien liever niet deelt ("Piet is ouderwets formeel, hou het u-formulier aan"). Oplossing: stuur een **abstracte persona-descriptor** mee: `{"style": "formal_dutch", "relationship": "long_term_client"}`. Dat is geen PII, wel bruikbaar.

**2. Inhoudelijk gevoelige context**
"Onze CFO vertrekt vanwege het incident" blijft na redactie `"[PERSON_001] vertrekt vanwege het incident"`. Dat is nog steeds gevoelig. **Beslisregel:** als het lokale model de content classificeert als `confidential` → hele item gaat niet naar Claude, alleen lokaal model handelt het af.

**3. Lange gedetailleerde documenten**
Een contract of gespreksverslag van 10 pagina's helemaal redacteren werkt, maar je riskeert dat zó veel context wordt vervangen dat Claude er niet meer fatsoenlijk over kan redeneren. Oplossing: lokaal model maakt eerst een **geabstraheerde samenvatting** ("Dit contract gaat over levering van X aan Y met looptijd Z"), en díe samenvatting (al inherent anoniem) gaat naar Claude voor het zware denkwerk.

---

## 5. Routing-regels (harde logica)

```python
def process(item):
    # 1. Lokaal model leest altijd eerst (met echte data)
    extracted = local_model.extract(item)
    sensitivity = classify(extracted)

    # 2. Hard stop voor confidential
    if sensitivity == "confidential":
        return local_model.handle_fully(item)

    # 3. Simpele taken: blijf lokaal, bespaar tokens + latency
    if extracted.task_complexity == "simple":
        return local_model.respond(item)

    # 4. Complex + internal/public: redacteer en ga naar Claude
    redacted, mapping = redactor.transform(extracted)
    preflight_check(redacted)   # laatste regex-scan, abort bij lek

    claude_response = claude_api.complete(redacted)

    # 5. Reconstructie lokaal
    final = reconstructor.apply(claude_response, mapping)
    return final
```

Niets in stap 5 raakt het netwerk. De mapping leeft kortstondig in-memory per request.

---

## 6. iMessage: waarom dit goed past

De iMessage-bridge draait per definitie lokaal (macOS is de enige plek waar iMessage bestaat). Dat betekent:

- **Outbound briefing**: reconstructed tekst → lokale bridge → Apple's iMessage-netwerk → jouw telefoon. Alleen Apple ziet het (end-to-end versleuteld tussen jouw Mac en jouw iPhone), Claude ziet het nooit in gereconstrueerde vorm.
- **Inbound van jou**: jouw iMessages aan de agent worden door de Mac-bridge opgepakt, klassificeerd als user-input, en doorlopen dezelfde pipeline. Als jij schrijft "Stuur Piet een mail dat ik morgen tijd heb", dan zal Claude (na redactie) "[PERSON_001]" zien, niet "Piet".

**Let op**: iMessage is E2E encrypted tussen Apple-apparaten, maar Apple's servers routeren het bericht. Voor `confidential` categorie-briefings kun je overwegen om die *niet* via iMessage te versturen maar via een lokaal dashboard of Signal (signal-cli, ook lokaal). Dat is paranoia-modus, maar past bij jouw rol.

---

## 7. Wat Claude ontvangt — een eerlijke inventaris

Om te valideren dat je comfortabel bent met deze architectuur, hier **letterlijk** wat Claude te zien krijgt in een typische request:

```json
{
  "task": "generate_morning_briefing",
  "context": {
    "user_ref": "USER",
    "date": "2026-04-22",
    "weekday": "wednesday",
    "items": [
      {
        "type": "email",
        "from_ref": "[PERSON_001]",
        "org_ref": "[ORG_001]",
        "org_tier": "A",
        "relationship": "long_term_client",
        "intent": "offerte_uitbreiding",
        "urgency": "medium",
        "days_open": 3,
        "summary": "[PERSON_001] vraagt uitbreiding offerte met 20 schermen."
      },
      {
        "type": "calendar_event",
        "time": "11:30",
        "duration_min": 30,
        "title_ref": "[EVENT_TITLE_001]",
        "attendees_refs": ["[PERSON_002]", "[PERSON_003]"],
        "location_type": "online"
      }
    ],
    "open_reminders": [
      "[REMINDER_001]",
      "[REMINDER_002]"
    ]
  },
  "output_format": "dutch_briefing_under_200_words"
}
```

Geen echte namen, geen echte mails, geen echte bedragen. Wel genoeg structuur om slim te redeneren.

De reminders worden ook geredacteerd? Ja — als jouw reminder is "Piet bellen over de Heineken-deal", dan wordt dat `"[PERSON_001] bellen over de [ORG_001]-deal"` richting Claude, en reconstruct je het lokaal terug.

---

## 8. Als je Claude écht meer context wilt geven

Soms is anonimisatie te restrictief en wil je dat Claude kán redeneren op karaktereigenschappen. Voeg dan **abstracte metadata** toe:

```json
{
  "from_ref": "[PERSON_001]",
  "meta": {
    "communication_style": "direct_no_pleasantries",
    "seniority": "c_level",
    "historical_response_time_hours": 48,
    "preferred_channel": "email",
    "language": "dutch"
  }
}
```

Dit is géén PII — het zijn gedragsprofielen. Claude kan hiermee een mail schrijven die qua toon klopt, zonder dat het de persoon kent.

---

## 9. Kostenprofiel

Bonus van deze hybride aanpak: het is ook goedkoper dan alles-naar-Claude.

Ruwe schatting (per werkdag, 30 mails + 4 meetings + 2 transcripts):

| Route | Verwerking | Cost estimate |
|---|---|---|
| Volledig via Claude | 30 mails full-body + samenvatting | ~$1.50 – $3.00 |
| Hybride (deze spec) | Extractie lokaal, alleen structured data naar Claude | ~$0.20 – $0.50 |

Lokaal draait gratis (na hardware-investering). Je bespaart 80-90% op API-kosten als bijvangst.

---

## 10. Aanpassing aan AGENT_SPEC

Deze hybride pipeline vervangt secties §6 (Architectuur) en §7 (Security & privacy) uit `AGENT_SPEC.md`. De rest van de spec blijft staan — fasering, kernfuncties, etc.

**Nieuwe fase-volgorde:**
1. Foundation (§8.1) — ongewijzigd.
2. **Lokaal model + redactor + gateway** (NIEUW, vóór triage). Zonder deze laag gaat geen enkele integratie naar buiten.
3. Triage & memory (§8.2) — met gateway eronder.
4. Plaud + meetings (§8.3).
5. Outlook + IMAP + polish (§8.4).

---

*Laatst bijgewerkt: 22 april 2026.*
