"""Tests voor models.ollama — HTTP-client mocked op urllib.request.urlopen."""
from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import patch

import pytest

from models.ollama import LocalResponse, OllamaClient, _flatten_content


# --- _flatten_content ------------------------------------------------------

def test_flatten_str_passes_through() -> None:
    assert _flatten_content("hi") == "hi"


def test_flatten_text_blocks() -> None:
    blocks = [{"type": "text", "text": "deel 1"}, {"type": "text", "text": "deel 2"}]
    assert _flatten_content(blocks) == "deel 1\ndeel 2"


def test_flatten_strips_tool_use_keeps_tool_result() -> None:
    blocks = [
        {"type": "text", "text": "context"},
        {"type": "tool_use", "id": "1", "name": "x", "input": {}},
        {"type": "tool_result", "tool_use_id": "1", "content": "tool output"},
    ]
    out = _flatten_content(blocks)
    assert "context" in out
    assert "tool output" in out
    assert "tool_use" not in out  # the block-type marker shouldn't leak


def test_flatten_handles_other() -> None:
    assert _flatten_content(None) == ""
    assert _flatten_content(42) == ""


# --- OllamaClient.chat (mocked HTTP) --------------------------------------

class _FakeResp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))
    def read(self) -> bytes: return self._buf.read()
    def __enter__(self) -> "_FakeResp": return self
    def __exit__(self, *a: Any) -> None: pass


@pytest.fixture
def client() -> OllamaClient:
    return OllamaClient(model="test-model", base_url="http://localhost:11434")


def test_chat_returns_local_response(client: OllamaClient) -> None:
    body = {
        "message": {"role": "assistant", "content": "Antwoord van het model."},
        "prompt_eval_count": 42,
        "eval_count": 17,
    }
    with patch("urllib.request.urlopen", return_value=_FakeResp(body)):
        resp = client.chat(system="be brief", messages=[{"role": "user", "content": "hoi"}])
    assert isinstance(resp, LocalResponse)
    assert resp.content[0].text == "Antwoord van het model."
    assert resp.content[0].type == "text"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 42
    assert resp.usage.output_tokens == 17


def test_chat_translates_anthropic_messages(client: OllamaClient) -> None:
    """Anthropic-format met list-of-blocks moet platgetrokken worden."""
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResp:
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"message": {"content": "ok"}})

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        client.chat(
            system="systeemprompt",
            messages=[
                {"role": "user", "content": "vraag 1"},
                {"role": "assistant", "content": [{"type": "text", "text": "antwoord 1"}]},
                {"role": "user", "content": "vraag 2"},
            ],
        )

    msgs = captured["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "systeemprompt"}
    assert msgs[1] == {"role": "user", "content": "vraag 1"}
    assert msgs[2] == {"role": "assistant", "content": "antwoord 1"}
    assert msgs[3] == {"role": "user", "content": "vraag 2"}


def test_chat_raises_on_url_error(client: OllamaClient) -> None:
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        with pytest.raises(RuntimeError, match="Ollama unreachable"):
            client.chat(system="", messages=[{"role": "user", "content": "x"}])
