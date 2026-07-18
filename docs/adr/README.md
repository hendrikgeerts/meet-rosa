# Architecture Decision Records

An ADR captures a decision, the context that motivated it, and the
consequences. When you find yourself thinking "wait, why is it done
this way?" — an ADR should have the answer.

Index (most recent first):

| ADR | Title | Status |
|---|---|---|
| [0001](0001-privacy-gateway.md) | Single privacy gateway as chokepoint | Accepted |
| [0002](0002-rosa-home.md) | `ROSA_HOME` as the single per-installation directory | Accepted |
| [0003](0003-feature-flags.md) | Every extension behind a boolean feature-flag | Accepted |
| [0004](0004-wizard-first-onboarding.md) | Browser wizard, not CLI, for first setup | Accepted |
| [0005](0005-adapter-layer.md) | Wizard writes both `config.yaml` and per-feature YAMLs | Accepted |
| [0006](0006-no-multi-tenant.md) | Single-user per install, no multi-tenancy | Accepted |

## Writing a new ADR

Copy an existing file. Format:

```
# ADR NNNN: Title

**Status:** Proposed | Accepted | Superseded by ADR-XXXX

## Context
What's the problem? What forces are at play?

## Decision
What did we decide?

## Consequences
What becomes easier? What becomes harder? What might we regret?
```

Keep them short. If you need 3 pages, the ADR isn't the right forum.
