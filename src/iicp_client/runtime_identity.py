"""Opt-in model-visible IICP runtime identity context."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from iicp_client.types import ChatMessage

RUNTIME_IDENTITY_PROFILE_ID = "urn:iicp:profile:runtime-identity-context:v0"
RUNTIME_IDENTITY_MARKER = "IICP-RUNTIME-CONTEXT/1"
CHAT_INTENT = "urn:iicp:intent:llm:chat:v1"
MAX_RENDERED_UTF8_BYTES = 2048

BASE_CAPSULE = (
    "This request reached you through IICP, the Intent-based Inter-agent Communication Protocol. "
    "IICP discovers eligible services and routes requests. You are the selected model or service, "
    "not IICP. When asked about this connection, use only supplied runtime facts; do not guess missing facts."
)

RuntimeIdentityMode = Literal["disabled", "explicit", "required"]
InstructionChannel = Literal["system", "unsupported"]


class RuntimeIdentityContextUnsupported(ValueError):
    """Raised before dispatch when required identity context cannot be composed."""


@dataclass(frozen=True)
class RuntimeIdentityOptions:
    mode: RuntimeIdentityMode = "disabled"
    instruction_channel: InstructionChannel = "system"
    selected_model: str | None = None
    effective_capabilities: tuple[str, ...] = ()
    selection_reason: str | None = None


def render_runtime_identity(intent: str, options: RuntimeIdentityOptions) -> str:
    """Render only bounded, explicitly supplied facts."""

    lines = [f"[{RUNTIME_IDENTITY_MARKER}]", BASE_CAPSULE, "Runtime facts:", f"- intent: {intent}"]
    if options.selected_model:
        lines.append(f"- selected model (provider assertion): {options.selected_model}")
    if options.effective_capabilities:
        lines.append(f"- effective capabilities: {', '.join(options.effective_capabilities)}")
    if options.selection_reason == "matched_intent_and_constraints":
        lines.append("- selection: This service matched the requested intent and constraints.")
    elif options.selection_reason is not None:
        raise ValueError("selection_reason is not an approved bounded value")
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > MAX_RENDERED_UTF8_BYTES:
        raise ValueError("runtime identity context exceeds the 2048-byte limit")
    return rendered


def compose_runtime_identity(
    messages: Sequence[ChatMessage],
    *,
    intent: str,
    options: RuntimeIdentityOptions | None,
) -> list[ChatMessage]:
    """Insert one canonical context while preserving application messages."""

    original = list(messages)
    if options is None or options.mode == "disabled" or intent != CHAT_INTENT:
        return original
    if options.mode not in {"explicit", "required"}:
        raise ValueError("runtime identity mode is unsupported")
    if options.instruction_channel == "unsupported":
        if options.mode == "required":
            raise RuntimeIdentityContextUnsupported("required_identity_context_unsupported")
        return original
    if any(
        message.role in {"system", "developer"} and RUNTIME_IDENTITY_MARKER in message.content for message in original
    ):
        return original

    insertion = 0
    while insertion < len(original) and original[insertion].role in {"system", "developer"}:
        insertion += 1
    capsule = ChatMessage(role="system", content=render_runtime_identity(intent, options))
    return [*original[:insertion], capsule, *original[insertion:]]
