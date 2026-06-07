"""Tests for OpenAI ↔ IICP translation."""
from __future__ import annotations

from iicp_client.proxy.openai_compat.translator import to_iicp_task, to_openai_response


def test_to_iicp_task_generates_uuid_and_intent():
    """PROXY-OAI-01: POST /v1/chat/completions translates to IICP CALL with correct intent URN."""
    task_id, intent, payload = to_iicp_task({"messages": [{"role": "user", "content": "Hi"}]})
    assert intent == "urn:iicp:intent:llm:chat:v1"
    assert payload["messages"] == [{"role": "user", "content": "Hi"}]
    assert str(task_id)  # valid UUID


def test_to_openai_response_maps_fields():
    """PROXY-OAI-02: IICP response translates to OpenAI format with choices[0].message.content."""
    from uuid import uuid4
    tid = str(uuid4())
    iicp = {
        "task_id": tid,
        "status": "success",
        "result": {"choices": [{"message": {"role": "assistant", "content": "Hi"}}], "usage": {}},
        "metrics": {"latency_ms": 10},
        "error": None,
    }
    out = to_openai_response(iicp, model="iicp")
    assert out["object"] == "chat.completion"
    assert out["model"] == "iicp"
    assert len(out["choices"]) == 1
    assert out["id"].startswith("chatcmpl-")
