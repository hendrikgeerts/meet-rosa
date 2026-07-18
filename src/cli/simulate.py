"""`rosa simulate` — pipe a synthetic message through Rosa without iMessage.

Useful for:
- Developing without Full Disk Access
- Testing prompt-changes without spending Anthropic credits (--dry-run)
- Reproducing bug reports (`rosa simulate "the exact user message"`)

Usage:
    rosa simulate "wat staat er vandaag op mijn agenda?"
    rosa simulate --dry-run "test message"        # skip Claude, just show what would be sent
    rosa simulate --handle +31612345678 "hi"      # pretend a specific handle sent it
"""
from __future__ import annotations

import argparse
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rosa simulate", description=__doc__)
    ap.add_argument("message", help="The message text to simulate")
    ap.add_argument("--handle", default=None,
                    help="iMessage handle to simulate (default: your primary)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip Claude — just show the classification + prompt")
    args = ap.parse_args(argv)

    from core.config import load_settings
    settings = load_settings()

    handle = args.handle or settings.primary_handle

    # First check quick-commands — these bypass Claude anyway.
    from core.quick_commands import try_quick_command
    quick = try_quick_command(args.message, settings)
    if quick is not None:
        print(f"→ Rosa (quick-command):\n{quick}")
        return 0

    # Classifier check
    import yaml

    from privacy.classifier import Classifier
    conf_yaml = settings.data_dir.parent / "config" / "confidential_domains.yaml"
    domains = []
    if conf_yaml.exists():
        data = yaml.safe_load(conf_yaml.read_text()) or {}
        for group in (data.get("domains") or {}).values():
            domains.extend(group)
    classifier = Classifier(
        confidential_domains=tuple(domains),
        confidential_keywords=("vertrouwelijk", "geheim"),
    )
    result = classifier.classify(sender=handle, text=args.message)
    print(f"Classification: {result.label}")
    print(f"  reason: {result.reason}")
    if result.matched:
        print(f"  matched: {result.matched}")

    if args.dry_run:
        print("\n[dry-run] would route to:",
              "local Llama" if str(result.label) == "confidential" else "Claude")
        return 0

    # For a real simulation we'd need to instantiate the whole orchestrator
    # — but that spins up Ollama, Gmail-client, etc. Instead: minimal
    # Claude call for interactive testing.
    print("\nRunning against Claude (this uses your API credits)...")
    from models.claude import ClaudeClient
    claude = ClaudeClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
    )
    # M-3: import direct uit core.prompts i.p.v. main, zodat we
    # main.py's ~800 regels module-scope import-side-effects skippen.
    from core.prompt_builder import render_system_prompt
    from core.prompts import SYSTEM_PROMPT_TEMPLATE
    system = render_system_prompt(SYSTEM_PROMPT_TEMPLATE, settings)

    t0 = time.time()
    response = claude.reply(
        system=system,
        messages=[{"role": "user", "content": args.message}],
        tools=None, max_tokens=1024,
    )
    dt = time.time() - t0
    text_parts = [b.text for b in response.content
                  if getattr(b, "type", None) == "text"]
    text = "".join(text_parts).strip()

    usage = getattr(response, "usage", None)
    tok_in = getattr(usage, "input_tokens", 0) if usage else 0
    tok_out = getattr(usage, "output_tokens", 0) if usage else 0
    from core.cost_tracker import usd_for
    cost = usd_for(settings.claude_model, tok_in, tok_out)

    print(f"\n→ Rosa (in {dt:.1f}s, {tok_in}+{tok_out} tokens, ${cost:.4f}):")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
