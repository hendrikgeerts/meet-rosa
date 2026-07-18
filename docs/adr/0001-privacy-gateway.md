# ADR 0001: Single privacy gateway as chokepoint

**Status:** Accepted

## Context

Rosa touches user data from multiple sources (mail, calendar, voice,
Slack, iMessage) and sends some of it to Claude (Anthropic's API) for
reasoning. Whether a specific piece of data leaves the machine is a
privacy decision that must be:

1. **Consistent**: same classification rules every time
2. **Auditable**: after the fact, we can prove what did/didn't go out
3. **Hard to bypass**: contributors can't accidentally add a
   `claude.messages.create(...)` call that skips classification

If classification and routing decisions were scattered across every
extension, correctness would depend on each contributor remembering
the rules — that scales poorly.

## Decision

There is exactly **one** function that talks to Claude:
`privacy.gateway.complete(...)`.

- All extensions that need Claude go through this function.
- Direct imports of the Anthropic SDK anywhere else are a
  review-block.
- The gateway internally: (1) classifies the input via
  `privacy.classifier`, (2) redacts via `privacy.redactor` or routes
  to `models.ollama` for `confidential` content, (3) writes an
  audit-log entry, (4) reconstructs placeholders in the response.

The gateway is enforced by convention (not import restriction) —
`grep -r "from anthropic" src/` outside `privacy/` and `models/` is
a failing PR.

## Consequences

**Easier:**
- One place to look when auditing "what leaves the Mac"
- Adding a new classification rule takes one edit, not N
- The `Gateway` interface is small (one function, three overloads
  for tools/no-tools/tool-turn continuation)

**Harder:**
- Any per-call knob (custom max_tokens, streaming) needs a matching
  parameter in `gateway.complete`
- Extensions can't opportunistically use Claude for one-off tasks —
  they have to declare their task-type and go through the routing

**Regret risks:**
- The `gateway.complete` signature has grown to 8+ kwargs. Once it
  passes ~12 we should split into a config-object.
- If Claude adds a fundamentally new capability (e.g. streaming
  vision), the gateway abstraction may block us. So far this hasn't
  happened.
