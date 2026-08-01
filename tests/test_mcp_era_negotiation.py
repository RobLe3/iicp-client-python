from __future__ import annotations

import json
from pathlib import Path

import pytest

from iicp_client.mcp_negotiation import (
    MODERN_MCP_REVISION,
    McpNegotiationError,
    build_modern_mcp_request,
    evaluate_mcp_era,
    validate_modern_mcp_response,
)


def test_shared_mcp_era_fixture() -> None:
    fixture = json.loads((Path(__file__).parents[1] / "parity/mcp-era-negotiation-v0.json").read_text())
    assert fixture["profile_id"] == "iicp.mcp.era-negotiation.v0"
    for case in fixture["cases"]:
        assert evaluate_mcp_era(case["input"]) == case["expected"], case["id"]


def test_modern_request_contains_only_explicit_safe_metadata() -> None:
    request = build_modern_mcp_request(
        request_id=7,
        method="tools/call",
        name="format_json",
        params={"name": "format_json", "arguments": {"value": 1}},
        extensions=("tasks",),
        request_state={"cursor": "opaque"},
    )
    assert request.headers["MCP-Protocol-Version"] == MODERN_MCP_REVISION
    assert request.headers["Mcp-Method"] == request.body["method"]
    assert request.headers["Mcp-Name"] == request.body["params"]["name"]
    rendered = json.dumps(request.body)
    assert "dispatch_ticket" not in rendered
    assert "node_token" not in rendered


def test_modern_request_rejects_unnegotiated_extension() -> None:
    with pytest.raises(McpNegotiationError, match="unsupported_extension"):
        build_modern_mcp_request(request_id=1, method="tools/call", name="x", params={}, extensions=("unknown",))


def test_modern_response_binds_server_identity() -> None:
    validate_modern_mcp_response(
        {"_meta": {"protocolVersion": MODERN_MCP_REVISION, "server": {"name": "local-mcp"}}},
        "local-mcp",
    )
    with pytest.raises(McpNegotiationError, match="server_identity_mismatch"):
        validate_modern_mcp_response(
            {"_meta": {"protocolVersion": MODERN_MCP_REVISION, "server": {"name": "other"}}},
            "local-mcp",
        )
