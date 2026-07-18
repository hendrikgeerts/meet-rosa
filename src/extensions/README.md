# extensions

## Doel
Optionele features achter een feature-flag. Per `CLAUDE.md`-principe: de
core werkt zonder enige extensie aan; extensies worden wel/niet ingeladen op
basis van `extensions:` in `config/settings.yaml`.

## Modules
- `reminders.py` — SQLite-tabel + add/list/cancel + due_now/mark_sent.
  De scheduler vuurt due reminders elke 10 seconden via iMessage.

## Niet aanwezig (komt later, per fase)
- `voice_in/`, `vision_ocr/` — Fase 6 (`AGENT_SPEC_EXTENSIONS §1, §2`)
- `tasks/`, `projects/` — Fase 7 (`§4, §5`)
- `decisions_log/`, `knowledge_base/`, `cognitive_debt/` — Fase 8 (`§6, §7, §9`)
- `patterns/`, `saturday_review/` — Fase 9 (`§8, §10`)
- `research/`, `delegation_tracker/` — Fase 10 (`§12, §13`)

## Public interface
- `extensions.reminders.{init_reminders_schema, add_reminder, list_pending,
  cancel_reminder, due_now, mark_sent}`

## Config-keys
- `extensions.reminders` (bool) — momenteel altijd op `true` aangezien
  scheduler en tools harde imports hebben. Wanneer plugin-registratie
  klaarstaat wordt deze flag echt afdwingbaar.

## Privacy-implicaties
Reminders bevatten user-gegenereerde tekst. Worden vandaag direct via
iMessage verstuurd (lokaal). Geen externe call aan deze module.

## Testscenario's
Geen unit-tests vandaag; SQLite-helpers zijn dun. Te schrijven zodra we
voorbij de regex-laag gaan en reminder-bodies door de redactor lopen.
