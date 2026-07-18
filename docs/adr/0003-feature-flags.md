# ADR 0003: Every extension behind a boolean feature-flag

**Status:** Accepted

## Context

Rosa has 20+ optional features (Todoist sync, weekly retro,
receipt collector, market-intel digest, etc.). Some users want all
of them; most only want a handful. If every extension ran by
default, first-boot would be slow, noisy, and confusing.

We also want to bisect problems: "did the CEO letter break because
of the classifier change, or something else?" — turning off
individual features quickly is essential.

## Decision

Every extension has a corresponding entry in `config.yaml` under
`features:` (managed by the wizard's Features step). Extensions
check `settings.extension_on("name")` before doing anything.

The whitelist lives in one place: `_ALLOWED_FEATURES` in
`src/wizard/server.py`. The wizard UI's checkbox list must be kept
in sync (there's a `test_l2_features_ui_covers_all_server_whitelist`
regression test).

Defaults:

- Core productivity features (briefings, dayclose, reminders,
  memory) default ON.
- Third-party integrations (Todoist, Slack, sales-pipeline,
  market-intel) default OFF.
- Experimental features (English practice, OKR coaching)
  default OFF.

## Consequences

**Easier:**
- Turning off a misbehaving feature is a one-line edit
- New users see a manageable list on first boot
- A/B testing an extension's behaviour without shipping code

**Harder:**
- Every new extension needs a wizard-step + a config-key + a UI
  entry. Non-trivial ceremony.
- Extension interdependencies must be explicit — e.g. `weekly_retro`
  needs `comm_intel` to be useful

**Regret risks:**
- The flag-set has grown; managing it in a flat namespace is
  starting to strain. If we get to 30 flags, group them into
  categories.
- Some flags overlap semantically ("comm_intel" vs "open_loops"
  vs "delegation_tracker" all touch open-thread state).
