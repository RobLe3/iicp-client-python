from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iicp_client.runtime_identity import (
    CHAT_INTENT,
    RUNTIME_IDENTITY_MARKER,
    RuntimeIdentityContextUnsupported,
    RuntimeIdentityOptions,
    compose_runtime_identity,
)
from iicp_client.types import ChatMessage

FIXTURE_BYTES = (Path(__file__).parents[1] / "parity/runtime-identity-context-v0/fixture.json").read_bytes()
FIXTURE = json.loads(FIXTURE_BYTES)


def messages(*values: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=role, content=content) for role, content in values]


def test_exact_shared_fixture_is_pinned() -> None:
    assert (
        hashlib.sha256(FIXTURE_BYTES).hexdigest() == "a31064ca630ab5409fb2f57edd1ef29a5c79532b8960927f6a0d2b52d7d71c81"
    )
    assert FIXTURE["context_marker"] == RUNTIME_IDENTITY_MARKER
    assert FIXTURE["composition"]["eligible_intent"] == CHAT_INTENT


def test_disabled_and_non_chat_requests_are_unchanged() -> None:
    original = messages(("user", "hello"))
    assert compose_runtime_identity(original, intent=CHAT_INTENT, options=None) != original
    assert (
        compose_runtime_identity(original, intent=CHAT_INTENT, options=RuntimeIdentityOptions(mode="disabled"))
        == original
    )
    assert (
        compose_runtime_identity(
            original,
            intent="urn:iicp:intent:llm:embedding:v1",
            options=RuntimeIdentityOptions(mode="explicit"),
        )
        == original
    )


def test_context_follows_leading_application_instructions_and_precedes_user() -> None:
    original = messages(("system", "Answer briefly."), ("developer", "Use plain text."), ("user", "What is this?"))
    composed = compose_runtime_identity(original, intent=CHAT_INTENT, options=RuntimeIdentityOptions(mode="explicit"))
    assert composed[0:2] == original[0:2]
    assert composed[2].role == "system"
    assert RUNTIME_IDENTITY_MARKER in composed[2].content
    assert composed[3] == original[2]


def test_existing_marker_suppresses_duplicate() -> None:
    original = messages(("system", f"[{RUNTIME_IDENTITY_MARKER}] existing"), ("user", "hello"))
    assert (
        compose_runtime_identity(original, intent=CHAT_INTENT, options=RuntimeIdentityOptions(mode="explicit"))
        == original
    )


def test_unknown_facts_are_omitted_and_supplied_facts_are_bounded() -> None:
    composed = compose_runtime_identity(
        messages(("user", "Which model?")),
        intent=CHAT_INTENT,
        options=RuntimeIdentityOptions(
            mode="explicit",
            selected_model="model-a",
            effective_capabilities=("input_modality:image",),
            selection_reason="matched_intent_and_constraints",
            client_name="iicp-client-python",
            client_version="0.7.105",
            connection_mode="routed",
        ),
    )
    content = composed[0].content
    assert "model-a" in content
    assert "input_modality:image" in content
    assert "candidate" not in content
    assert len(content.encode()) <= FIXTURE["composition"]["max_rendered_utf8_bytes"]


def test_unsupported_channel_degrades_optional_and_refuses_required() -> None:
    original = messages(("user", "hello"))
    optional = RuntimeIdentityOptions(mode="explicit", instruction_channel="unsupported")
    assert compose_runtime_identity(original, intent=CHAT_INTENT, options=optional) == original
    required = RuntimeIdentityOptions(mode="required", instruction_channel="unsupported")
    with pytest.raises(RuntimeIdentityContextUnsupported, match="required_identity_context_unsupported"):
        compose_runtime_identity(original, intent=CHAT_INTENT, options=required)


def test_default_auto_renders_client_and_rejects_control_character_facts() -> None:
    content = compose_runtime_identity(
        messages(("user", "What is this?")),
        intent=CHAT_INTENT,
        options=RuntimeIdentityOptions(client_name="iicp-client-python", client_version="0.7.105"),
    )[0].content
    assert "client: iicp-client-python 0.7.105" in content
    with pytest.raises(ValueError, match="control characters"):
        compose_runtime_identity(
            messages(("user", "hello")),
            intent=CHAT_INTENT,
            options=RuntimeIdentityOptions(selected_model="model\ninjected"),
        )


def test_malformed_runtime_context_options_fail_before_chat_dispatch() -> None:
    original = messages(("user", "hello"))
    with pytest.raises(ValueError, match="mode is unsupported"):
        compose_runtime_identity(
            original,
            intent=CHAT_INTENT,
            options=RuntimeIdentityOptions(mode="surprise"),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="instruction channel is unsupported"):
        compose_runtime_identity(
            original,
            intent=CHAT_INTENT,
            options=RuntimeIdentityOptions(instruction_channel="surprise"),  # type: ignore[arg-type]
        )
