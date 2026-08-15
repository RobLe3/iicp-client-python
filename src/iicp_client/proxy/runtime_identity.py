"""Runtime identity composition for compatibility proxy chat surfaces."""

from __future__ import annotations

from typing import Any

from iicp_client import __version__
from iicp_client.runtime_identity import RuntimeIdentityOptions, compose_runtime_identity, with_runtime_facts
from iicp_client.types import ChatMessage

RUNTIME_IDENTITY_HEADER = "X-IICP-Runtime-Identity"
_ALLOWED_MODES = {"auto", "disabled", "explicit", "required"}


def mode_from_header(value: str | None) -> str:
    """Resolve a bounded proxy override; omitted headers use auto."""

    mode = (value or "auto").strip().lower()
    if mode not in _ALLOWED_MODES:
        raise ValueError("invalid_runtime_identity_mode")
    return mode


def options_from_header(value: str | None) -> RuntimeIdentityOptions:
    return RuntimeIdentityOptions(mode=mode_from_header(value))  # type: ignore[arg-type]


def compose_proxy_payload(
    payload: dict[str, Any],
    *,
    intent: str,
    node: dict[str, Any],
    candidate_index: int,
    options: RuntimeIdentityOptions,
) -> dict[str, Any]:
    """Compose from the original proxy payload for one selected candidate."""

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return payload
    messages: list[ChatMessage] = []
    for raw in raw_messages:
        if not isinstance(raw, dict) or not isinstance(raw.get("role"), str) or not isinstance(raw.get("content"), str):
            return payload
        messages.append(ChatMessage(role=raw["role"], content=raw["content"]))

    requested_model = payload.get("model") if isinstance(payload.get("model"), str) else None
    models = node.get("models")
    advertised_models = [value for value in models if isinstance(value, str)] if isinstance(models, list) else []
    selected_model = requested_model if requested_model in advertised_models else None
    if selected_model is None and len(advertised_models) == 1:
        selected_model = advertised_models[0]
    candidate_options = with_runtime_facts(
        options,
        client_name="iicp-client-python-proxy",
        client_version=__version__,
        connection_mode="routed",
        selected_model=selected_model,
        selection_reason=(
            "matched_intent_and_constraints" if candidate_index == 0 else "fallback_after_unavailable_candidate"
        ),
    )
    composed = compose_runtime_identity(messages, intent=intent, options=candidate_options)
    return {
        **payload,
        "messages": [{"role": message.role, "content": message.content} for message in composed],
    }
