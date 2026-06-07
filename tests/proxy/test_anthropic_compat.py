"""Tests for Anthropic Messages API ↔ IICP translation (#279)."""
from __future__ import annotations

from uuid import UUID

from iicp_client.proxy.anthropic_compat.translator import (
    _flatten_content,
    to_anthropic_response,
    to_iicp_task,
)

# ---------------------------------------------------------------------------
# _flatten_content
# ---------------------------------------------------------------------------

def test_flatten_plain_string():
    assert _flatten_content("hello") == "hello"


def test_flatten_text_blocks():
    blocks = [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
    assert _flatten_content(blocks) == "foo bar"


def test_flatten_skips_non_text_blocks():
    blocks = [{"type": "image", "source": {}}, {"type": "text", "text": "caption"}]
    assert _flatten_content(blocks) == "caption"


def test_flatten_empty_list():
    assert _flatten_content([]) == ""


def test_flatten_none():
    assert _flatten_content(None) == ""


# ---------------------------------------------------------------------------
# to_iicp_task
# ---------------------------------------------------------------------------

def test_basic_user_message():
    task_id, intent, payload = to_iicp_task({
        "model": "claude-3-5-sonnet-20241022",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    })
    assert isinstance(task_id, UUID)
    assert intent == "urn:iicp:intent:llm:chat:v1"
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]
    assert payload["max_tokens"] == 100
    assert payload["model"] == "claude-3-5-sonnet-20241022"


def test_system_prompt_prepended_as_system_message():
    _, _, payload = to_iicp_task({
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "Hi"}],
    })
    assert payload["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}
    assert payload["messages"][1] == {"role": "user", "content": "Hi"}


def test_typed_content_blocks_flattened():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "What is"}, {"type": "text", "text": "2+2?"}],
            }
        ]
    }
    _, _, payload = to_iicp_task(body)
    assert payload["messages"][0]["content"] == "What is 2+2?"


def test_stream_flag_forwarded():
    _, _, payload = to_iicp_task({"stream": True})
    assert payload["stream"] is True


def test_stream_defaults_to_false():
    _, _, payload = to_iicp_task({})
    assert payload["stream"] is False


def test_temperature_forwarded():
    _, _, payload = to_iicp_task({"temperature": 0.5})
    assert payload["temperature"] == 0.5


def test_non_dict_message_items_are_skipped():
    # BUG-377: string items in messages list must not cause AttributeError
    _, _, payload = to_iicp_task({"messages": ["not a dict", {"role": "user", "content": "ok"}]})
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == "ok"


# ---------------------------------------------------------------------------
# to_anthropic_response
# ---------------------------------------------------------------------------

def _make_iicp(content: str = "Sure!", prompt_tokens: int = 5, completion_tokens: int = 3) -> dict:
    return {
        "task_id": "abc-123",
        "status": "success",
        "result": {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    }


def test_anthropic_response_shape():
    out = to_anthropic_response(_make_iicp("Hello!"), model="iicp", task_id="tid1")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "Hello!"}]
    assert out["stop_reason"] == "end_turn"
    assert out["id"].startswith("msg_")


def test_anthropic_response_model_passthrough():
    out = to_anthropic_response(_make_iicp(), model="claude-3-5-sonnet-20241022")
    assert out["model"] == "claude-3-5-sonnet-20241022"


def test_anthropic_response_token_counts():
    out = to_anthropic_response(_make_iicp(prompt_tokens=10, completion_tokens=20))
    assert out["usage"]["input_tokens"] == 10
    assert out["usage"]["output_tokens"] == 20


def test_anthropic_response_empty_result():
    out = to_anthropic_response({})
    assert out["content"] == [{"type": "text", "text": ""}]
    assert out["stop_reason"] == "end_turn"
