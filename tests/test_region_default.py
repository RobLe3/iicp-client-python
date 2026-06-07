# SPDX-License-Identifier: Apache-2.0
"""Region must never be silently mislabeled `eu-central`.

Regression for the first external operator's report (@shaal, near Miami: set `us-east`, the
directory showed `eu-central`). Root cause: `--region` defaulted to the truthy `"eu-central"`,
which both mislabeled non-EU operators AND shadowed the saved-config restore. The fix defaults
the flag to None and registers `"unknown"` when nothing is set. See #484 (auto-detect follow-up).
"""
import asyncio
import json

import httpx
import respx

from iicp_client import IicpNode, NodeConfig
from iicp_client.cli import _build_parser

REGISTER = "https://iicp.test/v1/register"


def _node(region):
    return IicpNode(
        NodeConfig(
            node_id="n",
            endpoint="https://provider.example:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="q",
            directory_url="https://iicp.test",
            region=region,
        )
    )


def test_serve_region_flag_defaults_to_none():
    """--region must default to None (not the truthy 'eu-central'), so a saved/explicit
    region is actually honored rather than shadowed."""
    args = _build_parser().parse_args(["serve"])
    assert args.region is None


@respx.mock
def test_register_payload_region_unknown_when_unset():
    """A node with no region registers as 'unknown', never 'eu-central'."""
    route = respx.post(REGISTER).mock(
        return_value=httpx.Response(201, json={"node_token": "tok", "node_id": "n"})
    )
    asyncio.run(_node(None).register())
    body = json.loads(route.calls[0].request.content)
    assert body["region"] == "unknown"


@respx.mock
def test_register_payload_honors_explicit_region():
    """An explicit region (e.g. us-east) is sent verbatim — not overridden."""
    route = respx.post(REGISTER).mock(
        return_value=httpx.Response(201, json={"node_token": "tok", "node_id": "n"})
    )
    asyncio.run(_node("us-east").register())
    body = json.loads(route.calls[0].request.content)
    assert body["region"] == "us-east"
