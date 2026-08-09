# SPDX-License-Identifier: Apache-2.0
"""MCP protocol-era negotiation and stateless request helpers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

LEGACY_MCP_REVISION = "2025-11-25"
MODERN_MCP_REVISION = "2026-07-28"
SUPPORTED_MCP_REVISIONS = (LEGACY_MCP_REVISION, MODERN_MCP_REVISION)
SUPPORTED_MCP_EXTENSIONS = frozenset({"tasks", "skills", "apps"})


class McpNegotiationError(ValueError):
    """Fail-closed MCP negotiation error with a stable reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def evaluate_mcp_era(case: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one content-free ``iicp.mcp.era-negotiation.v0`` input."""
    for field, reason in (
        ("oauth_issuer_matches", "oauth_issuer_mismatch"),
        ("oauth_audience_matches", "oauth_audience_mismatch"),
        ("resource_indicator_present", "missing_resource_indicator"),
        ("protected_resource_metadata_valid", "invalid_protected_resource_metadata"),
        ("pkce_valid", "pkce_required"),
        ("consent_granted", "consent_required"),
        ("audit_output_redacted", "audit_redaction_required"),
    ):
        if case.get(field) is False:
            return {"accepted": False, "reason": reason}
    if case.get("downstream_credential_source") == "caller":
        return {"accepted": False, "reason": "credential_passthrough_prohibited"}
    if case.get("server_identity_matches_selected_endpoint") is False:
        return {"accepted": False, "reason": "server_identity_mismatch"}
    if case.get("modern_request_failed") and not case.get("legacy_authentication_available"):
        return {"accepted": False, "reason": "unauthenticated_downgrade"}
    extension = case.get("extension")
    if extension and extension not in SUPPORTED_MCP_EXTENSIONS:
        return {"accepted": False, "reason": "unsupported_extension"}

    offered = case.get("offered_revision")
    if offered == MODERN_MCP_REVISION:
        if case.get("protocol_header_present") is False:
            return {"accepted": False, "reason": "missing_protocol_version"}
        if case.get("method_header_matches") is False or case.get("name_header_matches") is False:
            return {"accepted": False, "reason": "header_body_mismatch"}
        if case.get("reserved_meta_valid") is False:
            return {"accepted": False, "reason": "malformed_reserved_metadata"}
        peer = case.get("peer_supported_revisions", [])
        if MODERN_MCP_REVISION in peer or not peer:
            result: dict[str, Any] = {"accepted": True}
            if case.get("request_state_explicit"):
                result["state_source"] = "request"
            else:
                result.update({"era": "modern", "session_mode": "stateless"})
            return result
        if (
            LEGACY_MCP_REVISION in peer
            and case.get("legacy_revision_explicitly_offered")
            and case.get("security_requirements_preserved")
        ):
            return {"accepted": True, "era": "legacy", "reason": "explicit_mutual_downgrade"}
        return {"accepted": False, "reason": "unsupported_revision"}

    if offered == LEGACY_MCP_REVISION:
        peer = case.get("peer_supported_revisions", [])
        if LEGACY_MCP_REVISION in peer or not peer:
            return {"accepted": True, "era": "legacy", "session_mode": "negotiated"}
    return {"accepted": False, "reason": "unsupported_revision"}


@dataclass(frozen=True)
class ModernMcpRequest:
    headers: dict[str, str]
    body: dict[str, Any]


def build_modern_mcp_request(
    *,
    request_id: int,
    method: str,
    name: str,
    params: Mapping[str, Any],
    client_name: str = "iicp-gateway",
    extensions: tuple[str, ...] = (),
    request_state: Mapping[str, Any] | None = None,
) -> ModernMcpRequest:
    unknown = sorted(set(extensions) - SUPPORTED_MCP_EXTENSIONS)
    if unknown:
        raise McpNegotiationError("unsupported_extension")
    meta: dict[str, Any] = {
        "protocolVersion": MODERN_MCP_REVISION,
        "client": {"name": client_name},
    }
    if extensions:
        meta["extensions"] = list(extensions)
    if request_state is not None:
        meta["requestState"] = dict(request_state)
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**dict(params), "_meta": meta},
    }
    return ModernMcpRequest(
        headers={
            "MCP-Protocol-Version": MODERN_MCP_REVISION,
            "Mcp-Method": method,
            "Mcp-Name": name,
        },
        body=body,
    )


def validate_modern_mcp_response(data: Mapping[str, Any], expected_server_name: str) -> None:
    meta = data.get("_meta")
    if not isinstance(meta, Mapping) or meta.get("protocolVersion") != MODERN_MCP_REVISION:
        raise McpNegotiationError("malformed_reserved_metadata")
    server = meta.get("server")
    if not isinstance(server, Mapping) or server.get("name") != expected_server_name:
        raise McpNegotiationError("server_identity_mismatch")
