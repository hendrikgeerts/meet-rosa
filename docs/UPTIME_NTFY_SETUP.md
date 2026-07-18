# Uptime monitor — Ntfy.sh push voor wakker-worden-'s-nachts

iMessage tekst + voice-bubble komen binnen via je gebruikelijke Apple-
notification settings. Maar tijdens Do-Not-Disturb / Focus / Sleep mode
heb je géén garantie dat je de melding hoort. Voor dat scenario:
**Ntfy.sh** — gratis push service met critical-priority die door iOS
Focus heen breekt.

## Wat is Ntfy

Open-source pub/sub voor push. Je host een **topic** (= random string,
= je private channel), iOS-app abonneert op die topic, server stuurt
push als er iets in komt. Geen account, geen credit card, geen DPA-
overhead. Bij Critical priority gebruikt iOS de "Time Sensitive" / 
"Critical Alert" toggle die door Focus breekt.

## Setup (5 min)

### Stap 1 — kies een topic

Verzin een lange random string die alleen jij weet. Bv:
```
rosa-uptime-xyz4Q9k2mZ
```
Hoe langer + random hoe veiliger — anderen kunnen je topic niet
afluisteren OF spammen als de naam niet bekend is. Geen secret-store
nodig: het is gewoon een naam.

### Stap 2 — voeg het topic toe aan `.env`

Open `~/pa-agent/.env`, voeg toe:
```
NTFY_TOPIC=rosa-uptime-xyz4Q9k2mZ
```
(en eventueel `NTFY_SERVER=https://ntfy.sh` — default).

Restart de daemon zodat Settings het oppikt:
```
launchctl kickstart -k gui/$(id -u)/com.rosa.pa-agent
```

### Stap 3 — Ntfy iOS-app installeren

App Store → zoek **"Ntfy"** (open-source, free).

In de app:
1. Tap `+` om een subscription toe te voegen.
2. **Topic name:** je gekozen string (`rosa-uptime-xyz4Q9k2mZ`).
3. **Server:** `https://ntfy.sh` (default).
4. **Notification priority:** zet op **Maximum** zodat hij Focus
   override gebruikt.
5. Save.

iOS vraagt om Notification permissions → Allow.

### Stap 4 — test

In een terminal:
```
curl -H "Priority: 5" \
     -H "Title: Test from Rosa" \
     -d "Ntfy werkt." \
     https://ntfy.sh/rosa-uptime-xyz4Q9k2mZ
```

Je iPhone zou binnen 1-2 seconden een melding moeten geven, ook als
hij in Focus / Do-Not-Disturb staat (priority 5 = critical).

### Stap 5 — verifieer dat Rosa pusht

Forceer een downtime-scenario door bv. tijdelijk een fake-target met
een onbestaande URL in `config/uptime.yaml` te zetten:
```yaml
  - name: "TEST"
    url: "https://intentionally-nonexistent-domain-xyz.test/"
    expected_status: 200
    check_interval_seconds: 60
    fail_threshold: 2
    alert_channels: [imessage, voice, ntfy]
```

## Automatische escalation bij lange downtime

Vanaf de "11/6-fix"-ronde hoef je `ntfy` **niet** meer expliciet
in `alert_channels` te zetten om door DND te breken bij lange
outages. De worker doet auto-escalation: zodra de downtime de
geconfigureerde drempel overschrijdt wordt `ntfy` automatisch
toegevoegd aan de alert-channels voor die specifieke alert.

**Default drempel: 600 seconden (10 min).** Configureerbaar via
`settings.yaml`:

```yaml
uptime:
  escalate_after_seconds: 600   # 0 om escalation uit te zetten
```

Per-target override mogelijk in `config/uptime.yaml`:

```yaml
  - name: "your CMS"
    url: "https://cms.dst-connect.io/"
    # … standaard fields …
    alert_channels: [imessage, voice]   # ntfy NIET expliciet
    escalate_after_seconds: 120         # MAAR escaleer na 2 min
```

Bij gebruik van auto-escalation: je moet wel een `NTFY_TOPIC` in `.env`
hebben — zonder topic is er geen plek om naartoe te escaleren en
slaat de worker het stil over (geen valse beloftes).

Restart daemon. Binnen ~2 minuten krijg je een iMessage + voice-bubble
+ Ntfy-push voor "TEST is DOWN". Daarna config weghalen + nog een
restart.

## Wat de melding bevat

```
Title: DOWN: your CMS
Body:
  🔴 DOWN: your CMS
  URL: https://cms.dst-connect.io/
  Status: HTTP 503 — Server Error
  Down for 2m 30s (since 03:42)
  Latency at fail: 8350ms
Priority: 5 (critical)
Click: https://cms.dst-connect.io/  ← tap = open URL
```

## Privacy + sub-processor

- Topic naam alleen aan jouw kant bekend — server doet geen
  authenticatie maar kent ook niet welke topics actief zijn (server
  bewaart niets na delivery).
- ntfy.sh is een Duits-Spanje hosted open-source service (heimdal.io).
  Voor strict ISO 27001-conform: self-host de ntfy-server. Documentatie:
  https://docs.ntfy.sh/install/
- Berichten bevatten geen PII anders dan de URL van je platform en
  HTTP-error info. Geen klant-data, geen interne metrics.
- Audit-trail: élke ntfy-push wordt in `data/audit/egress-*.jsonl`
  gelogd via `core.external_audit.timed_call(service="ntfy")`.

## Stop / pauzeren

- Per target: zet `alert_channels: [imessage]` (haal `ntfy` weg) in
  `config/uptime.yaml`.
- Globaal: verwijder `NTFY_TOPIC` uit `.env`. Worker pusht dan niets
  meer, zelfs als ntfy in channels-list staat.
- Geplande maintenance op één target (bv. komende deploy van 10 min):
  ```bash
  ./venv/bin/python scripts/uptime_silence.py \
      --name "your CMS" --minutes 10 --reason "deploy v2.4"
  ```
  Alle alerts (iMessage + voice + ntfy) worden onderdrukt tot dat
  moment. Recovery-bericht wordt ook onderdrukt — eerlijk maintenance-
  signaal, geen pseudo-recovery. Het script schrijft een audit-event
  in `uptime_events` (kind='silence') met operator + reden zodat de
  silence-actie traceerbaar is. Clear handmatig vóór tijd:
  ```bash
  ./venv/bin/python scripts/uptime_silence.py --name "your CMS" --clear
  ```
