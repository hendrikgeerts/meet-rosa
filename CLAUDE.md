# CLAUDE.md

Claude Code reads this file at the start of every session. Keep it
short and current.

---

## What Rosa is

Rosa is a personal AI assistant for macOS. She reads the user's mail
(Gmail, Outlook, IMAP), calendar (Google Calendar), and voice
recordings (Plaud), and communicates with the user primarily over
iMessage. The goal is proactive daily support: briefings, draft
replies, follow-ups, memory.

Each installation is single-user. The user configures Rosa once
through a browser wizard (`localhost:8765`), then she runs as a
background daemon.

Before starting a task, read:

- `README.md` — user-facing intro and quick-start
- `docs/AGENT_SPEC.md` — feature specification
- `docs/PRIVACY_LAYER.md` — classifier + redactor + gateway
- `docs/HYBRID_ARCHITECTURE.md` — how local model + Claude cooperate
- `docs/AGENT_SPEC_EXTENSIONS.md` — optional extensions

---

## Language and tone

- Code (identifiers, comments, docstrings): English
- User-facing strings (iMessage, briefings, dashboard): English by
  default. The wizard has a `preferred_language` field; some
  reflection-style prompts (weekly retro, weekend prep, CEO letter)
  respect it and can output in the user's own language.
- Commits: English, imperative form ("Add Whisper integration", not
  "Added" or "Adds")
- Documentation: user-facing docs (README, INSTALL, CHANGELOG) in
  English; specs (AGENT_SPEC, PRIVACY_LAYER) in English.

Address the user with "you", direct and concise, no disclaimers.

---

## Stack

| Component | Choice |
|---|---|
| Runtime | Python 3.12 |
| Web layer | FastAPI (wizard + dashboard) |
| Local LLM | Ollama + `llama3.1:8b-instruct-q4_K_M` (main), `phi3:mini` (fast) |
| Embedding model | `nomic-embed-text` via Ollama |
| External LLM | Claude Sonnet 4.x via Anthropic API |
| Whisper | `faster-whisper` with `medium` model |
| Database | SQLite (with `sqlite-vec` for vectors) |
| OCR | Apple Vision (via Swift helper or `shortcuts run`) |
| iMessage bridge | Local daemon on macOS watching `chat.db` + sending via AppleScript |
| Testing | pytest + respx (HTTP mocks) |
| Linting | ruff |
| Type-checking | mypy (strict) |

Hardware target: any Apple Silicon Mac; MVP verified on macOS 13+.
For CPU-only Intel Macs, expect slower Ollama inference — use the
smaller `phi3:mini` model for interactive paths.

---

## Project structure

```
rosa/
├── README.md                    # user-facing intro
├── CLAUDE.md                    # this file
├── LICENSE
├── install.sh                   # first-time install
├── docs/                        # specs (see list above)
├── config/                      # .example.yaml templates only
├── src/
│   ├── core/                    # orchestrator, memory, scheduler, config
│   ├── integrations/            # gmail, outlook, imap, gcal, plaud, imessage
│   ├── privacy/                 # classifier, redactor, reconstructor, gateway
│   ├── models/                  # ollama-client, claude-client, whisper
│   ├── extensions/              # voice-in, tasks, projects, etc.
│   ├── wizard/                  # FastAPI setup wizard + adapters
│   └── web/                     # local dashboard
├── tests/                       # pytest, mirrors src/
├── scripts/                     # setup, migration, one-offs
```

Runtime data lives in `ROSA_HOME` (default:
`~/Library/Application Support/Rosa/`) — `data/`, `logs/`, `audit/`,
`secrets.env`, `config.yaml`. Nothing in the repo tree is
per-user.

**Rules:**
- `core/` does not import from `extensions/` or `integrations/`.
  Extensions and integrations register with core through explicit
  interfaces.
- `privacy/gateway.py` is the **only** file that imports the Claude
  API. Direct API use elsewhere is a review-block.
- All paths come from `Settings` (loaded via `core.config.load_settings`).
  No hardcoded paths in code — always via config.

---

## Principles

### 1. Privacy is a constraint, not a feature
Every external LLM call goes through `privacy.gateway.complete(...)`.
That function:
1. Classifies the input (`public` / `internal` / `confidential`).
2. Routes: `confidential` → local, otherwise → redact + Claude.
3. Runs a pre-flight regex scan on the redacted payload before send.
4. Reconstructs placeholders in the response.

If you write code that calls Claude directly, review it — you're
probably doing it wrong.

### 2. Everything is config
Model names, endpoints, paths, thresholds, timeouts → wizard →
`config.yaml`. Code reads config at startup. This is what makes
migration between machines trivial.

### 3. Log egress, not content
Every external call logs: timestamp, task-type, sensitivity-label,
model, bytes in/out, redaction-stats. **Never** the content. Logs
live under `ROSA_HOME/audit/` and rotate daily.

### 4. Local first, Claude for reasoning
Not every task needs Claude. Classification, extraction, summarising
simple items → local. Claude for: complex reasoning, briefing
synthesis, quality writing. When in doubt: local.

### 5. Async where possible
Briefings, triage, transcript processing are batch-ish and can take
minutes. Only direct iMessage conversation has a synchronous latency
budget. Use the simple job-queue (SQLite-backed is fine for now).

### 6. Feature flags for extensions
Every extension sits behind a flag written by the wizard's
"Features" step. Core works with zero extensions enabled.

---

## Working conventions

### Commits
- Small, focused commits. One logical change per commit.
- Imperative English: `Add redactor spaCy NER layer`, not
  `Added NER` or `Adds spaCy layer for redactor`.
- If the change touches security or privacy, tag it with `(privacy)`
  in the message. E.g. `Add audit logging for Claude calls (privacy)`.

### Tests
- Core logic and privacy layer: **required** unit-tests. Especially
  the redactor — test with realistic (fictional) mails.
- Integrations: mock external APIs with `respx` / fixtures. No tests
  that hit real Gmail.
- Orchestrator: integration tests with a mock LLM gateway that
  returns fixed responses.
- Not chasing 100% coverage. Test quality > count.

### Module docs
Every module under `src/` has a `README.md` covering:
- **Purpose** (1-2 lines)
- **Public interface** (which functions/classes are meant for other consumers)
- **Config keys** the module uses
- **Privacy implications** (what data does it touch; anything external?)
- **Test scenarios** (what MUST work; what are edge cases)

### Secrets
- **Never** committed. `.env`, `secrets.env`, `google_credentials.json`
  are in `.gitignore`.
- API keys: `secrets.env` (chmod 0600), managed by the wizard.
- OAuth tokens: `google_token.json` under `ROSA_HOME` (also 0600).

### Uncertainty
If a spec is ambiguous, add a `# TODO(clarify):` comment in code
**and** flag it in your commit description. Don't guess about the
user's tooling, preferences, or business processes.

---

## What NOT to do

- Don't pull in heavy frameworks (LangChain, LlamaIndex, etc.). We
  keep the orchestrator readable and our own. If a framework really
  adds value, discuss before implementing.
- Don't add cloud services beyond what's in the specs (Anthropic
  API, Google APIs, Microsoft Graph). No Vercel, no Supabase, no
  OpenAI-as-fallback. Local or the recognised external APIs.
- Don't add "smart" auto-actions that aren't in the specs. Autonomy
  expands only with explicit user approval per feature.
- Don't refactor "because it could be prettier" without a concrete
  problem. This is a working system — stability > elegance.
- No telemetry, analytics, crash reporting to third parties. Local
  logs, full stop.

---

## Starting a fresh session

If you're a Claude Code session starting fresh on this repo:

1. Read `docs/STATUS.md` first — current snapshot.
2. Skim `docs/CHANGELOG.md` for recent changes.
3. Read `docs/AGENT_SPEC.md` through §8 (phasing) if you're new to
   the codebase.
4. `git log --oneline | head -20` for actual repo state.
5. If there's a specific task: check the relevant module `README.md`
   under `src/`.
6. If there's no explicit task: ask what the goal is.

**At the end of a working session:** update `docs/STATUS.md` if the
codebase's state changed materially — at minimum "Last update",
"What ships in <version>", and any big-picture roadmap shift.

---

*CLAUDE.md is intentionally shorter than the specs. For deep dives:
the specs. For working conventions: this file.*
