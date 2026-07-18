# Status

**Version:** 0.1.0 (MVP)
**Last update:** 2026-07-18

Rosa is a working personal AI assistant for macOS. This document
tracks what's shipping and what's next.

---

## What ships in 0.1.0

**Core**
- Setup wizard on `localhost:8765` — 15 steps, all optional except
  identity + Anthropic key
- Bootstrap: `main.py` detects missing config and launches the wizard
- LaunchAgent installer for 24/7 operation
- Full YAML round-trip: wizard writes, daemon reads, no manual edits

**Integrations**
- Anthropic Claude API (bring your own key)
- Ollama for local LLM routing (Llama 3.1 8B + Phi-3 mini)
- Google OAuth 2.0 for Gmail + Calendar (bring your own OAuth client)
- IMAP for non-Gmail mailboxes
- Slack (via User OAuth token)
- Todoist (via API token)
- Plaud voice recorder (watched folder)
- iMessage (via macOS bridge, requires Full Disk Access)

**Privacy**
- Single-chokepoint gateway: `privacy.gateway.complete()` is the only
  path to Claude
- Classifier routes confidential-domain mail to the local model
- Redactor + Reconstructor swap real names for placeholders before
  external calls, and reconstruct after
- Every external call is audit-logged (metadata only, no content)

**Features (feature-flagged)**
- Morning briefing, midday heads-up, day-close
- Weekly retrospective (Sat), weekend prep (Sun), CEO letter (Fri)
- Reminders + Todoist sync
- VIP-aware triage
- Communication intelligence (cross-channel "who's waiting")
- Uptime monitor
- Meeting prep (30min before external meetings)
- Voice recorder analysis
- Market-intel digest
- Pattern detection
- Receipt collector

Twenty-plus feature flags, all off by default. You turn on what you
need in the wizard.

---

## Test coverage

1400+ tests, all passing. Key areas:

- Wizard end-to-end via TestClient (all 15 steps)
- Full-flow integration: wizard config → `load_settings()` →
  schema init → classifier construction
- Privacy: classifier, redactor, reconstructor, gateway audit
- Bootstrap guard rails: `ROSA_DEV=1` and implicit
  `config/settings.yaml` detection

---

## Not in 0.1.0

**Deferred to 0.2:**
- Outlook Graph API integration (IMAP works for Outlook accounts today)
- OCR of images / PDFs beyond text extraction
- Realtime voice conversation
- Web dashboard beyond the local one (currently localhost only)

**Won't build:**
- Multi-user tenancy (each install is single-user by design)
- Cloud-hosted variant (privacy stance means self-hosted only)
- Auto-actions without confirmation (autonomy expands with explicit
  per-user opt-in)

---

## Roadmap

The next milestones focus on operational polish, not new features:

1. **Verified real-world install** — bootstrapping on a fresh Mac
   with only the README + a fresh Anthropic key, end-to-end
2. **Rich installer UX** — progress bars for the Ollama pulls, better
   error messages when prereqs are missing
3. **Documentation of the extension points** — how to add your own
   integration without touching core

See `docs/ROADMAP_2026.md` for the longer view.
