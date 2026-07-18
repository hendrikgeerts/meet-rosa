"""Thin wrapper around the Anthropic SDK with prompt caching of the system block."""
from __future__ import annotations

import logging
from typing import Any

from anthropic import Anthropic

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2048


class ClaudeClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def reply(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Any:
        """Single turn. `messages` follows the Anthropic messages format.

        System prompt is sent as a cacheable block so repeated turns with the
        same prompt share the cache (5-minute TTL).
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(**kwargs)
