"""Regression guard for #10 — node_hmac_key is a @property, not a method.

On 0.7.61, cli.py called `node.node_hmac_key()` (with parens) on the post-registration
HMAC-cache step. node_hmac_key is a @property returning str, so the call raised
`TypeError: 'str' object is not callable`; the broad except treated it as a registration
failure across all 3 retries, the node never reached a registered client state, heartbeats
stopped, and the directory marked it offline within ~30s — even though the server-side
register succeeded. This pins both the property contract and that the CLI accesses it as
an attribute.
"""
from __future__ import annotations

import inspect


def test_node_hmac_key_is_a_property():
    from iicp_client.node import IicpNode

    assert isinstance(IicpNode.node_hmac_key, property)


def test_cli_does_not_call_node_hmac_key_as_method():
    from iicp_client import cli

    src = inspect.getsource(cli)
    assert "node_hmac_key()" not in src, (
        "cli must read node_hmac_key as a @property attribute, never call it as a method (#10)"
    )
