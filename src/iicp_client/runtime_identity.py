"""Lean model-visible IICP runtime identity context for compatible chat calls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from iicp_client.types import ChatMessage

RUNTIME_IDENTITY_PROFILE_ID = "urn:iicp:profile:runtime-identity-context:v0"
RUNTIME_IDENTITY_MARKER = "IICP-RUNTIME-CONTEXT/1"
CHAT_INTENT = "urn:iicp:intent:llm:chat:v1"
MAX_RENDERED_UTF8_BYTES = 2048
MAX_FACT_UTF8_BYTES = 160
MAX_CAPABILITIES = 32

BASE_CAPSULE = (
    "This request reached you through IICP, the Intent-based Inter-agent Communication Protocol. "
    "IICP discovers eligible services and routes requests. You are the selected model or service, "
    "not IICP. When asked about this connection, use only supplied runtime facts; do not guess missing facts."
)

RuntimeIdentityMode = Literal["auto", "disabled", "explicit", "required"]
InstructionChannel = Literal["system", "unsupported"]
ConnectionMode = Literal["routed", "local_browser"]

_SELECTION_TEXT = {
    "matched_intent_and_constraints": "This service matched the requested intent and constraints.",
    "explicit_model_match": "This service matched the requested model and constraints.",
    "fallback_after_unavailable_candidate": "This service was selected after an earlier candidate was unavailable.",
    "intentional_exploration": "This service was selected for an intentional routing exploration.",
    "local_browser_execution": "This model is running locally in the browser.",
}


class RuntimeIdentityContextUnsupported(ValueError):
    """Raised before dispatch when required identity context cannot be composed."""


@dataclass(frozen=True)
class RuntimeIdentityOptions:
    mode: RuntimeIdentityMode = "auto"
    instruction_channel: InstructionChannel = "system"
    selected_model: str | None = None
    effective_capabilities: tuple[str, ...] = ()
    selection_reason: str | None = None
    client_name: str | None = None
    client_version: str | None = None
    connection_mode: ConnectionMode | None = None


def with_runtime_facts(
    options: RuntimeIdentityOptions | None,
    *,
    client_name: str,
    client_version: str,
    connection_mode: ConnectionMode,
    selected_model: str | None,
    effective_capabilities: Sequence[str] = (),
    selection_reason: str,
) -> RuntimeIdentityOptions:
    """Replace candidate-specific facts so retries cannot retain stale values."""

    base = options or RuntimeIdentityOptions()
    return replace(
        base,
        client_name=client_name,
        client_version=client_version,
        connection_mode=connection_mode,
        selected_model=selected_model,
        effective_capabilities=tuple(effective_capabilities),
        selection_reason=selection_reason,
    )


def _fact(value: str, name: str) -> str:
    if not value or any(ord(char) < 0x20 or char == "\x7f" for char in value):
        raise ValueError(f"runtime identity {name} contains control characters")
    if len(value.encode("utf-8")) > MAX_FACT_UTF8_BYTES:
        raise ValueError(f"runtime identity {name} exceeds the bounded fact limit")
    return value


def render_runtime_identity(intent: str, options: RuntimeIdentityOptions) -> str:
    """Render bounded facts supplied by the active client and selected route."""

    lines = [f"[{RUNTIME_IDENTITY_MARKER}]", BASE_CAPSULE, "Runtime facts:", f"- intent: {_fact(intent, 'intent')}"]
    if options.client_name or options.client_version:
        if not options.client_name or not options.client_version:
            raise ValueError("runtime identity client name and version must be supplied together")
        lines.append(
            f"- client: {_fact(options.client_name, 'client name')} {_fact(options.client_version, 'client version')}"
        )
    if options.connection_mode == "routed":
        lines.append("- connection: routed through IICP to an eligible provider.")
    elif options.connection_mode == "local_browser":
        lines.append(
            "- connection: This model is running locally in the browser; no remote IICP provider was selected."
        )
    elif options.connection_mode is not None:
        raise ValueError("runtime identity connection_mode is unsupported")
    if options.selected_model:
        lines.append(f"- selected model: {_fact(options.selected_model, 'selected model')}")
    if options.effective_capabilities:
        if len(options.effective_capabilities) > MAX_CAPABILITIES:
            raise ValueError("runtime identity effective capabilities exceed the bounded count")
        capabilities = [_fact(value, "effective capability") for value in options.effective_capabilities]
        lines.append(f"- effective capabilities: {', '.join(capabilities)}")
    if options.selection_reason is not None:
        selection = _SELECTION_TEXT.get(options.selection_reason)
        if selection is None:
            raise ValueError("selection_reason is not an approved bounded value")
        lines.append(f"- selection: {selection}")
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > MAX_RENDERED_UTF8_BYTES:
        raise ValueError("runtime identity context exceeds the 2048-byte limit")
    return rendered


def compose_runtime_identity(
    messages: Sequence[ChatMessage],
    *,
    intent: str,
    options: RuntimeIdentityOptions | None = None,
) -> list[ChatMessage]:
    """Insert one canonical context while preserving application messages."""

    original = list(messages)
    resolved = options or RuntimeIdentityOptions()
    if intent != CHAT_INTENT:
        return original
    if resolved.mode not in {"auto", "explicit", "required"}:
        if resolved.mode == "disabled":
            return original
        raise ValueError("runtime identity mode is unsupported")
    if resolved.instruction_channel not in {"system", "unsupported"}:
        raise ValueError("runtime identity instruction channel is unsupported")
    if resolved.instruction_channel == "unsupported":
        if resolved.mode == "required":
            raise RuntimeIdentityContextUnsupported("required_identity_context_unsupported")
        return original
    if any(
        message.role in {"system", "developer"} and RUNTIME_IDENTITY_MARKER in message.content for message in original
    ):
        return original

    insertion = 0
    while insertion < len(original) and original[insertion].role in {"system", "developer"}:
        insertion += 1
    capsule = ChatMessage(role="system", content=render_runtime_identity(intent, resolved))
    return [*original[:insertion], capsule, *original[insertion:]]
