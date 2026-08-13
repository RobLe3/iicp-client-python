# Phase 2 (#529/#55): re-register sends current_node_token ownership proof
"""The register payload must include current_node_token when a prior token is
held (re-registration), and omit it on a fresh register."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from iicp_client import IicpNode, NodeConfig

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/e050-client-credential-lifecycle-v1.json").read_text()
)


def _cfg() -> NodeConfig:
    return NodeConfig(
        node_id="n-reg",
        endpoint="https://node.example.com",
        intent="urn:iicp:intent:llm:chat:v1",
        model="llama-3-8b",
        region="eu-central",
        directory_url="https://iicp.test/api",
    )


@respx.mock
@pytest.mark.asyncio
async def test_fresh_register_omits_current_node_token():
    route = respx.post("https://iicp.test/api/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-new", "node_id": "n-reg"})
    )
    node = IicpNode(_cfg())
    await node.register()
    body = httpx.Request("POST", "x", content=route.calls[0].request.content).read()
    import json

    assert "current_node_token" not in json.loads(body)


@respx.mock
@pytest.mark.asyncio
async def test_reregister_sends_current_node_token():
    route = respx.post("https://iicp.test/api/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-new", "node_id": "n-reg"})
    )
    node = IicpNode(_cfg())
    node._node_token = "tok-prior"  # simulate a cached token from a prior run
    await node.register()
    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["current_node_token"] == "tok-prior"


@respx.mock
@pytest.mark.asyncio
async def test_e050_client_credential_lifecycle_fixture():
    """Accepted rotations advance credentials; refusals preserve the current token."""
    node = IicpNode(_cfg())
    for scenario in FIXTURE["scenarios"]:
        starting = scenario["starting_token"]
        node._node_token = starting or ""
        route = respx.post("https://iicp.test/api/v1/register").mock(
            return_value=httpx.Response(
                scenario["directory_status"],
                json=(
                    {"node_token": scenario["directory_token"], "node_id": "n-reg"}
                    if scenario["directory_token"]
                    else {"error": "IICP-E050"}
                ),
            )
        )
        before = node._node_token
        if scenario["directory_status"] == 201:
            await node.register()
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await node.register()
            assert node._node_token == before
        payload = json.loads(route.calls[-1].request.content)
        assert payload.get("current_node_token") == scenario["expected_request_token"]
        assert node._node_token == scenario["expected_saved_token"]


@respx.mock
@pytest.mark.asyncio
async def test_register_advertises_only_enabled_consumer_cosignature_profile():
    route = respx.post("https://iicp.test/api/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-new", "node_id": "n-reg"})
    )
    cfg = _cfg()
    cfg.supported_receipt_profiles = ["unknown_v1", "consumer_cosignature_v1", "consumer_cosignature_v1"]
    await IicpNode(cfg).register()
    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["supported_receipt_profiles"] == ["consumer_cosignature_v1"]
