# ADR 0006: Single-user per install, no multi-tenancy

**Status:** Accepted

## Context

Multiple people on one Mac (family shared computer, small office
setup) is a real scenario. Making Rosa multi-tenant — one process
serving multiple users with a per-user config — sounds attractive
from a resource-usage perspective.

But it also complicates:

- The privacy layer, which currently trusts everything in
  `ROSA_HOME` as belonging to one user
- iMessage identity — Rosa's `owner_handles` becomes ambiguous
- Memory database — do users share the "who did I meet last week"
  index? That's a privacy violation.
- Ollama sharing — one Ollama process is fine for two Rosa instances
  in principle, but scheduling and rate-limiting get thorny.

## Decision

Rosa is single-user per installation. If two people want Rosa:

- Each sets a different `ROSA_HOME` (e.g. `~/Library/Application
  Support/Rosa/` for user A, `/tmp/rosa-bob/` for user B).
- Each has their own Anthropic API key.
- Each runs their own `main.py` process (different LaunchAgent
  labels).
- They can share the same Ollama server, since Ollama is
  request-scoped.

Sharing the same repo is fine — it's the runtime state under
`ROSA_HOME` that differentiates.

## Consequences

**Easier:**
- The whole codebase can assume "one user" — no per-request
  identity threading
- The privacy layer can trust that data reaching `main.py` belongs
  to *the* user
- Testing is simpler

**Harder:**
- Family / office use cases need docs on how to run two Rosa's
- If we ever want a hosted / shared variant, we'd need a fork
- Some people expect "multi-account" to mean multi-user; it does
  not (multiple Gmail accounts *within* one user is fine — see
  `imap_accounts.yaml`)

**Regret risks:**
- If Rosa gets adopted by teams (not individuals), single-user
  becomes a hard limit. Reconsider then.
- The `ROSA_DEV=1` guard rail is a special case that has grown
  organically — we don't want more of those.
