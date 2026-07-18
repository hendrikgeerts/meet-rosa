# Contributing to Rosa

Thanks for wanting to help. Rosa is a working single-user system;
we optimise for stability and readability over cleverness.

## Ground rules

1. **Privacy is a constraint, not a feature.** Every external LLM
   call goes through `privacy.gateway.complete(...)`. Direct
   Claude-API imports outside that file are review-blocks. See
   [`docs/PRIVACY_LAYER.md`](docs/PRIVACY_LAYER.md).

2. **No big frameworks.** LangChain, LlamaIndex, etc. are out of
   scope. If a framework really adds value, open an issue and let's
   discuss before implementing.

3. **No cloud services beyond the specified ones.** Anthropic,
   Google APIs, Microsoft Graph — that's the list. No Supabase, no
   Vercel deploys, no OpenAI fallback.

4. **No telemetry, no crash-reporting, no analytics.** Local logs
   only, full stop.

5. **Feature flags for every extension.** Core must work with all
   extensions off. See `_ALLOWED_FEATURES` in `src/wizard/server.py`.

## Working on the code

### Setup for development

```bash
git clone <this-repo>
cd rosa
./install.sh                # creates venv + pulls Ollama models
```

Run the tests:

```bash
PYTHONPATH=src ~/Library/Application\ Support/Rosa/venv/bin/python -m pytest tests/
```

You should see 1400+ tests passing in ~1 minute.

### Coding conventions

Language: English for code, comments, docstrings, user-facing
strings, commit messages. Documentation-in-user-language is
allowed for reflection-style prompts (weekly retro, CEO letter)
when the user set `preferred_language: "nl"` etc.

Style:

- `ruff` for linting: `ruff check src/ tests/`
- `mypy --strict` for type-checking (in progress; not enforced yet)
- Tests use pytest; mock external HTTP with `respx`
- Docstrings: 1-line if self-explanatory; multi-line only when there
  are non-obvious constraints

Commits:

- English, imperative form: `Add Whisper integration`, not
  `Added Whisper`
- One logical change per commit
- Prefix with `(privacy)` if it touches the classifier / gateway
- Reference issues: `Fix #42`

Pull requests:

- Ship tests with new code
- Update docs (README, CLAUDE.md, module READMEs) when public
  behaviour changes
- Don't refactor unrelated code in the same PR

## Adding an integration

Rough shape of "add a new integration":

1. Create `src/integrations/<name>.py` with a client class.
   Constructor takes credentials from `Settings`; there's a
   `README.md` in the module folder describing config-keys.
2. Add a wizard step: `src/wizard/server.py` with a `POST
   /api/step/<name>` endpoint. Persist the token to `secrets.env`
   and any structured config via `adapters.py`.
3. Add wizard UI in `wizard.html` (new `<template>`) + `wizard.js`
   (a `wire<Name>()` function).
4. If the integration produces data, add a scheduler tick in
   `src/core/scheduler.py`.
5. Tests: unit tests for the integration client, `respx`-based
   HTTP mocks, and a wizard-endpoint test.
6. Docs: update `README.md` feature-list and `docs/INSTALL.md`.

## Reporting bugs

Use `.github/ISSUE_TEMPLATE/bug_report.md`. Always paste `rosa
doctor` output — it saves at least one round-trip.

## Reviewing PRs

If you have review-access:

- Actually run the code, don't just read
- Check that new code has tests
- Check that privacy invariants hold (nothing routes around the
  gateway)
- Push back on scope-creep — small PRs are better than mega-PRs

## License

By contributing you agree to license your contribution under the
MIT license (same as the rest of the repo).
