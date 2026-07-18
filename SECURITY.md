# Security Policy

## Reporting a vulnerability

If you find a security issue in Rosa, please **don't open a public
GitHub issue**. Instead, email the maintainer directly:

> security@ (see the maintainer's GitHub profile for the address)

Include:

- A description of the issue and the impact
- Steps to reproduce, or a proof-of-concept
- Your suggested fix if you have one
- Whether you'd like public credit

You should get a first response within 72 hours. We aim to have a
fix ready within 14 days for HIGH-severity issues.

## Scope

Rosa is a single-user, self-hosted application. In-scope:

- Privacy-gateway bypass — anything that lets user data reach Claude
  without going through classification + redaction
- Wizard-server auth bypass — RCE, session-token bypass, path
  traversal in wizard endpoints
- Secrets leakage — logs, error messages, or audit files that
  contain unmasked API keys, tokens, refresh_tokens
- Adjacent-user attacks on shared Macs — one user reading another
  user's Rosa data via world-readable files
- Path-traversal in backup/restore
- Prompt-injection that produces confirmed exfiltration (not
  jailbreaks that just make Rosa refuse to answer)

Out-of-scope (please don't report these):

- Weaknesses in dependencies (report to those upstreams)
- Missing rate-limiting on the wizard (bound to loopback, single-user)
- Denial-of-service against the wizard-server
- Rosa's LLM outputting Wrong Things (that's a model quality issue,
  not a security issue)
- Missing HTTPS on `localhost:8765` (loopback = same origin as the
  process)

## Supported versions

Only the `main` branch. There are no release channels for 0.x.

## Public-key encryption

If you want to encrypt your report, ask for a PGP key by email.
