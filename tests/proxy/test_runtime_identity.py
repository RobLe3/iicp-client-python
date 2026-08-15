from __future__ import annotations

from iicp_client.proxy.runtime_identity import compose_proxy_payload, mode_from_header, options_from_header
from iicp_client.runtime_identity import RUNTIME_IDENTITY_MARKER


def test_proxy_header_defaults_auto_and_accepts_explicit_controls() -> None:
    assert mode_from_header(None) == "auto"
    assert mode_from_header("disabled") == "disabled"
    assert mode_from_header("required") == "required"


def test_proxy_recomposes_candidate_facts_from_original_payload() -> None:
    payload = {"messages": [{"role": "user", "content": "Which model?"}], "model": "model-b"}
    first = compose_proxy_payload(
        payload,
        intent="urn:iicp:intent:llm:chat:v1",
        node={"models": ["model-a"]},
        candidate_index=0,
        options=options_from_header(None),
    )
    second = compose_proxy_payload(
        payload,
        intent="urn:iicp:intent:llm:chat:v1",
        node={"models": ["model-b"]},
        candidate_index=1,
        options=options_from_header(None),
    )
    first_context = first["messages"][0]["content"]
    second_context = second["messages"][0]["content"]
    assert RUNTIME_IDENTITY_MARKER in first_context
    assert "model-a" in first_context
    assert "model-b" not in first_context
    assert "model-b" in second_context
    assert "model-a" not in second_context
    assert "earlier candidate was unavailable" in second_context
    assert payload == {"messages": [{"role": "user", "content": "Which model?"}], "model": "model-b"}
