"""Lokaal-LLM helpers — gedeeld door alle features die een korte
Llama-call doen voor verrijking (pattern-narrative, decision-tag,
person-summary, vendor-suggest, project-keyword-suggest).

Doel: één plek voor het patroon "vraag Llama om JSON of korte tekst,
timeout-tolerant, fallback op None bij elke fout". Geen ruwe
ollama.chat-calls in feature-code.

Privacy: alle calls blijven on-device. Geen audit-logging want geen
externe egress.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def llm_short_text(
    ollama: Any | None, *,
    system: str, user: str, max_tokens: int = 120,
) -> str | None:
    """Vraag een 1-2 zin Llama-antwoord. Returns None bij elke fout
    (timeout, JSON-error, geen ollama-client)."""
    if ollama is None:
        return None
    try:
        response = ollama.chat(
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
        )
        text = ""
        for block in getattr(response, "content", []):
            if getattr(block, "type", None) == "text":
                text += block.text
        text = text.strip()
        if not text:
            return None
        # Strip markdown-fences als Llama die per ongeluk toevoegt
        if text.startswith("```"):
            text = text.strip("`").lstrip("a-z\n ").strip()
        return text[:500]  # safety cap
    except Exception:
        log.exception("llm_short_text failed")
        return None


def llm_json_array(
    ollama: Any | None, *,
    system: str, user: str, max_tokens: int = 200,
) -> list[Any] | None:
    """Vraag Llama om een JSON-array. Strip optionele markdown-fences
    + parse. Returns None bij elke fout."""
    text = llm_short_text(ollama, system=system, user=user,
                            max_tokens=max_tokens)
    if not text:
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        result = json.loads(text[start:end + 1])
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        log.warning("llm_json_array: non-JSON response %r", text[:200])
    return None


def llm_json_object(
    ollama: Any | None, *,
    system: str, user: str, max_tokens: int = 200,
) -> dict[str, Any] | None:
    """Vraag Llama om een JSON-object. Returns None bij elke fout."""
    text = llm_short_text(ollama, system=system, user=user,
                            max_tokens=max_tokens)
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        result = json.loads(text[start:end + 1])
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        log.warning("llm_json_object: non-JSON response %r", text[:200])
    return None
