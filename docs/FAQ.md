# FAQ

Conceptual questions about Rosa. For "why isn't X working?" see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Why another AI assistant?

Because the good ones live in someone else's cloud. Rosa runs on your
Mac. Your mail, calendar, voice recordings, and memory-database
never leave your hardware unless you explicitly enable an
integration. The only cloud interaction is Anthropic's Claude API,
and even those calls are pre-classified, redacted, and audit-logged.

## Why Claude and not OpenAI?

Claude's tool-use is more predictable in practice — for a system
that chains 30+ tools in a single reply, that matters. Also: Anthropic's
privacy stance and TOS are more aligned with what Rosa is trying to
do (no training on API traffic without opt-in, no dark-patterns
around data retention).

You could swap `models/claude.py` for an OpenAI or Groq client if you
insist — the `Gateway` interface is one function. But then the tests
break, and you're on your own for tool-use quirks.

## Why iMessage as the primary interface?

Because it's already on every Mac + iPhone, requires no separate
app to install, works offline (mostly), and is what most people
already use for quick "hey" messages. A chat interface is also
what most people expect from an AI assistant.

Alternatives (Slack DM, Telegram, WhatsApp) are on the roadmap.

## Why local models via Ollama?

Two reasons:

1. Anything classified as `confidential` (mail from lawyers,
   accountants, therapists) never leaves your Mac. That requires a
   local model.
2. Simple tasks (classifying "is this a task?", extracting a date)
   don't need Claude. Running them locally is faster and free.

Ollama is used because it's the simplest way to get llama.cpp
running on macOS with sane defaults.

## Does Rosa work on Windows / Linux?

Not currently. The iMessage bridge is macOS-only (AppleScript +
chat.db). The wizard, gateway, orchestrator, and most extensions
are OS-agnostic — but with no message interface on Windows/Linux,
you'd need to swap iMessage for something else (Telegram, Signal,
web chat). PRs welcome.

## Do I need Full Disk Access?

Yes, if you want Rosa to read incoming iMessages
(`~/Library/Messages/chat.db` is protected by macOS TCC). You do
NOT need Full Disk Access to send iMessages — that goes via
AppleScript automation.

If you don't grant FDA, Rosa can still send you notifications and
run scheduled briefings, but she can't respond to your messages.

## How much does Rosa cost to run?

- **Claude API**: $5–15 / month for typical daily use (1 briefing,
  1 day-close, ~10 conversational messages). Set a monthly budget
  cap in `config.yaml` → `privacy.monthly_anthropic_budget_usd` to
  prevent runaway.
- **Google APIs**: free within default quotas.
- **Ollama models**: free, run locally. Cost is CPU/GPU cycles and
  ~15 GB disk.
- **Everything else**: free (or covered by your existing
  subscription for Todoist / Slack / Plaud).

## Does Rosa learn from my data?

She remembers what you tell her (via the `remember` tool, or when
you edit `user_profile.yaml`) and uses SQLite to store communication
metadata for triage. She does NOT train any model on your data.

Every external Claude call is audit-logged to
`~/Library/Application Support/Rosa/audit/`; you can inspect
exactly what Rosa sent and when.

## Can I self-host Anthropic's API?

Anthropic doesn't offer self-hosted Claude weights. The best you can
do for full local operation is switch everything to Ollama by
setting the classifier default to `confidential`. Quality drops
noticeably; that's the trade-off.

## Why not use LangChain?

Because we want the code to remain readable. LangChain and similar
frameworks abstract things Rosa needs to be explicit about (which
tool is being called with what arguments, whether a message is
redacted before send, how memory is stored). We've kept the
orchestrator small on purpose — it's ~300 lines of Python and you
can read the whole flow.

## Can two people share a Rosa install?

No — each install is single-user. If you and a colleague both want
Rosa, run two installs (each with its own `ROSA_HOME`) and its own
Anthropic key.

Multi-user tenancy is explicitly out of scope. The privacy stance
would be very different if two people shared a memory-DB.

## What if Anthropic disappears / prices go up?

- Local briefings + reminders keep working (Ollama-only paths).
- Conversational replies stop until you swap the LLM backend.
- The privacy layer, adapters, and iMessage bridge don't care
  which model is on the other end of `models/claude.py`.

## How do I remove all my data?

```bash
rm -rf ~/Library/Application\ Support/Rosa
```

That's it. Rosa keeps nothing outside that directory.
