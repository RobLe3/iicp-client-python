"""Registration diagnostics must not render arbitrary acknowledgment fields."""
from __future__ import annotations

import httpx
import pytest
import respx

from iicp_client import IicpNode, NodeConfig

DIRECTORY = "https://directory.example.invalid"
MARKER = "SYNTHETIC_REGISTRATION_DETAIL_NOT_FOR_DIAGNOSTICS"


def config() -> NodeConfig:
    return NodeConfig(
        node_id="synthetic-registration-node",
        endpoint="https://provider.example.invalid",
        intent="urn:iicp:intent:llm:chat:v1",
        model="fixture-model",
        directory_url=DIRECTORY,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"private_detail": MARKER},
        {"node_token": None, "token": None, "node_hmac_key": MARKER},
        {"node_token": "", "token": "", "private_detail": MARKER},
    ],
    ids=["missing-token", "null-token", "empty-tokens"],
)
@respx.mock
@pytest.mark.asyncio
async def test_missing_registration_token_redacts_arbitrary_response(body):
    route = respx.post(f"{DIRECTORY}/v1/register").mock(
        return_value=httpx.Response(200, json=body)
    )
    node = IicpNode(config())
    node._node_token = "synthetic-existing-token"
    try:
        with pytest.raises(RuntimeError) as caught:
            await node.register()
        assert str(caught.value) == "Directory did not return node_token"
        assert MARKER not in str(caught.value)
        assert node._node_token == "synthetic-existing-token"
        assert route.call_count == 1
    finally:
        await node._http.aclose()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"node_token": "primary", "token": "alias"}, "primary"),
        ({"token": "alias"}, "alias"),
        ({"node_token": None, "token": "alias"}, "alias"),
        ({"node_token": "", "token": "alias"}, "alias"),
    ],
    ids=["primary-precedence", "alias-only", "null-primary-alias", "empty-primary-alias"],
)
@respx.mock
@pytest.mark.asyncio
async def test_registration_token_selection_is_unchanged(body, expected):
    respx.post(f"{DIRECTORY}/v1/register").mock(
        return_value=httpx.Response(200, json=body)
    )
    node = IicpNode(config())
    try:
        assert await node.register() == expected
        assert node._node_token == expected
    finally:
        await node._http.aclose()
