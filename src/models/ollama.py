"""Lokale Ollama-client (HTTP tegen localhost:11434).

Voor `confidential`-flows die nooit extern mogen, en voor classifier-/redactor-
tiebreakers die een lokaal LLM nodig hebben. Geen tool-use (Ollama-modellen
zijn daar niet betrouwbaar in genoeg) — alleen platte chat completion.

Response-shape spiegelt de Anthropic-SDK enough om door dezelfde callers
gebruikt te kunnen worden (orchestrator, briefings) zonder extra adapters:
`.content` is een lijst van `_TextBlock(type="text", text=...)`, `.stop_reason`
en `.usage` bestaan.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LocalResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


def _normalize_keep_alive(value: str | int) -> str | int:
    """Coerce keep_alive naar wat Ollama accepteert.

    - `-1` of `"-1"` → integer -1 (Ollama parsed dit als 'forever')
    - `"30m"` / `"1h"` / etc. → pass-through (duration-string met eenheid)
    - andere ints (bv. 0, 300) → pass-through (Ollama interpreteert als seconden)
    - rest → pass-through, laat Ollama de error geven
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip() in ("-1", "-1s"):
        return -1
    return value


class OllamaClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: float = 240.0,   # 4 min — terug naar origineel; scheduler-briefings gaan via force_label='internal' zodat ze niet lokaal draaien
        keep_alive: str | int = "30m",
    ) -> None:
        """`keep_alive`: hoe lang Ollama het model in memory houdt na de
        laatste call. Default Ollama is 5m, wat voor onze workload té kort
        is — model wordt steeds re-loaded (30-60s per call op CPU). We
        zetten hem op 30m voor de gedeelde clients (gateway/summarize/
        embed/scheduler-Llama). `-1` betekent voor altijd ingeladen tot
        Ollama-server stopt — handig voor batch-jobs.

        Coerce-laag (toegevoegd na productie-bug 30/4-4/6 2026): newer
        Ollama versies wijzen `"-1"` string af met HTTP 400 (geen
        duration-eenheid). We accepteren `-1`, `"-1"`, of een duration-
        string ("30m"/"1h"/"24h"/etc.) en normaliseren naar wat Ollama
        snapt — integer voor `-1`, anders pass-through."""
        self._model = model
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._keep_alive = _normalize_keep_alive(keep_alive)

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LocalResponse:
        """Chat completion via /api/chat. Anthropic-format messages worden
        vertaald naar Ollama's flat {role, content} per turn."""
        ollama_msgs: list[dict[str, str]] = []
        if system:
            ollama_msgs.append({"role": "system", "content": system})
        for m in messages:
            ollama_msgs.append({
                "role": str(m.get("role", "user")),
                "content": _flatten_content(m.get("content")),
            })

        body = {
            "model": self._model,
            "messages": ollama_msgs,
            "stream": False,
            "keep_alive": self._keep_alive,
            "options": {"num_predict": max_tokens},
        }
        req = urllib.request.Request(
            f"{self._base}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama unreachable at {self._base}: {exc}") from exc

        text = (data.get("message") or {}).get("content", "") or ""
        return LocalResponse(
            content=[_TextBlock(text=text)],
            stop_reason="end_turn",
            usage=_Usage(
                input_tokens=int(data.get("prompt_eval_count", 0) or 0),
                output_tokens=int(data.get("eval_count", 0) or 0),
            ),
        )


def _flatten_content(content: Any) -> str:
    """Anthropic content kan str zijn of een list of blocks (text/tool_use/
    tool_result). We pakken alleen de tekst — Ollama heeft geen tool-use."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                parts.append(str(b.get("text", "")))
            elif btype == "tool_result":
                parts.append(str(b.get("content", "")))
        return "\n".join(p for p in parts if p)
    return ""
