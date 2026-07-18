# ADR 0004: Browser wizard, not CLI, for first setup

**Status:** Accepted

## Context

A new user needs to configure at least:

- Their name, timezone, language
- Anthropic API key
- iMessage handle
- Google OAuth (if they want mail/calendar)
- Slack / Todoist / IMAP / Plaud (each with its own auth flow)
- Feature flags (20+)

Doing this via CLI prompts would be:

- Slow (30+ questions)
- Fragile (miss one prompt in a script, whole thing breaks)
- Non-visual (users can't see what they've configured so far)

Doing it via manual YAML editing pushes complexity onto the user
and makes the tool feel amateurish.

## Decision

The setup is a browser-based wizard, served by a temporary FastAPI
server on `localhost:8765`. On first boot, `main.py` detects that
`ROSA_HOME/config.yaml` doesn't exist, starts the wizard, prints
the URL, and blocks until the user clicks *Finish setup*.

Design constraints:

- Server bound to `127.0.0.1` only
- CSRF via in-memory `_SESSION_TOKEN` injected into HTML meta-tag
- No JavaScript frameworks — vanilla JS, one file
- No build step — HTML/CSS/JS ships as-is
- OAuth callbacks use the same server so no extra port config

## Consequences

**Easier:**
- Setup is discoverable ("your browser opens, follow the steps")
- Users can see progress, skip optional steps, go back
- Field-level help text lives with the field
- Same UI updates in place — server + wizard code co-evolve

**Harder:**
- Wizard-server is another surface with its own security concerns
  (CSRF, session token, callback validation)
- Testing the wizard requires either Selenium (bloat) or hitting
  the API directly (what we do, via FastAPI TestClient)
- Users on headless / SSH sessions need the terminal-fallback we
  currently don't have (open issue)

**Regret risks:**
- Vanilla JS starts to feel painful past 15 wizard-steps. If we
  add another dimension (multi-account per integration), we'll
  need a small component framework.
- OAuth callback flows currently expect `localhost:8765` — if the
  port shifts, users need to update Google Cloud client
  authorized-URIs.
