"""Tests for Ollama ↔ IICP translation and route surface (#278)."""
from __future__ import annotations

from uuid import UUID

from iicp_client.proxy.ollama_compat.translator import (
    to_iicp_task,
    to_ollama_generate_response,
    to_ollama_response,
)

# ---------------------------------------------------------------------------
# to_iicp_task
# ---------------------------------------------------------------------------

def test_chat_body_preserves_messages():
    messages = [{"role": "user", "content": "Hello"}]
    task_id, intent, payload = to_iicp_task({"messages": messages, "model": "iicp"})
    assert isinstance(task_id, UUID)
    assert intent == "urn:iicp:intent:llm:chat:v1"
    assert payload["messages"] == messages
    assert payload["model"] == "iicp"


def test_generate_body_wraps_prompt_as_user_message():
    _, _, payload = to_iicp_task({"prompt": "Tell me a joke", "model": "iicp"})
    assert payload["messages"] == [{"role": "user", "content": "Tell me a joke"}]


def test_empty_body_produces_empty_user_message():
    _, _, payload = to_iicp_task({})
    assert payload["messages"] == [{"role": "user", "content": ""}]


def test_options_num_predict_maps_to_max_tokens():
    _, _, payload = to_iicp_task({"options": {"num_predict": 256, "temperature": 0.7}})
    assert payload["max_tokens"] == 256
    assert payload["temperature"] == 0.7


def test_stream_flag_forwarded():
    _, _, payload = to_iicp_task({"stream": True})
    assert payload["stream"] is True


def test_stream_defaults_to_false():
    _, _, payload = to_iicp_task({})
    assert payload["stream"] is False


def test_non_dict_options_does_not_crash():
    # BUG-377: options must be ignored (not crash) when malformed
    _, _, payload = to_iicp_task({"options": "fast"})
    assert payload["temperature"] is None
    assert payload["max_tokens"] is None


def test_none_options_treated_as_empty():
    _, _, payload = to_iicp_task({"options": None})
    assert payload["temperature"] is None


# ---------------------------------------------------------------------------
# to_ollama_response (/api/chat)
# ---------------------------------------------------------------------------

def _make_iicp(content: str = "hi", prompt_tokens: int = 5, completion_tokens: int = 3) -> dict:
    return {
        "task_id": "tid-123",
        "status": "success",
        "result": {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    }


def test_ollama_response_shape():
    out = to_ollama_response(_make_iicp("Hello"))
    assert out["done"] is True
    assert out["done_reason"] == "stop"
    assert out["message"]["role"] == "assistant"
    assert out["message"]["content"] == "Hello"
    assert "created_at" in out


def test_ollama_response_model_passthrough():
    out = to_ollama_response(_make_iicp(), model="llama3")
    assert out["model"] == "llama3"


def test_ollama_response_token_counts():
    out = to_ollama_response(_make_iicp(prompt_tokens=10, completion_tokens=20))
    assert out["prompt_eval_count"] == 10
    assert out["eval_count"] == 20


def test_ollama_response_empty_result():
    out = to_ollama_response({})
    assert out["message"]["content"] == ""
    assert out["done"] is True


# ---------------------------------------------------------------------------
# to_ollama_generate_response (/api/generate)
# ---------------------------------------------------------------------------

def test_generate_response_uses_response_field():
    out = to_ollama_generate_response(_make_iicp("Punchline!"))
    assert out["response"] == "Punchline!"
    assert "message" not in out
    assert out["done"] is True


def test_generate_response_empty():
    out = to_ollama_generate_response({})
    assert out["response"] == ""
