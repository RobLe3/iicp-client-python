# SPDX-License-Identifier: Apache-2.0
"""#463/#464 — the register payload carries the operator identity (delegation +
display_name) so the directory can record the operator + surface display_name on node
detail; it NEVER sends the operator's secret key or contact/email. Fails without the wiring
(payload would omit operator_display_name)."""

import json

from iicp_client.delegation import issue_delegation
from iicp_client.identity import OperatorIdentity
from iicp_client.node import IicpNode, NodeConfig

CHAT = "urn:iicp:intent:llm:chat:v1"


class _Resp:
    status_code = 201

    def raise_for_status(self):
        pass

    def json(self):
        return {"node_token": "tok", "node_hmac_key": "hk"}


async def test_register_payload_carries_operator_fields_never_secret(monkeypatch):
    op = OperatorIdentity.generate(display_name="Rebel One", contact="me@example.com")
    node_id = "test-node-1"
    cfg = NodeConfig(
        node_id=node_id,
        endpoint="http://host.test:9484",
        intent=CHAT,
        model="m",
        operator_delegation=issue_delegation(op.signing_key(), node_id),
        operator_display_name=op.display_name,
        operator_created_at=op.created_at,
        operator_integrity_hash=op.operator_integrity_hash,
    )
    node = IicpNode(cfg)

    captured: dict = {}

    async def _fake_post(url, json=None, **kw):  # noqa: A002
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(node._http, "post", _fake_post)
    await node.register()

    p = captured["payload"]
    # Operator identity rides with the delegation; operator_pub IS the operator_id (#464).
    assert p["operator_delegation"]["operator_pub"] == op.operator_id
    assert p["operator_display_name"] == "Rebel One"
    assert p["operator_integrity_hash"] == op.operator_integrity_hash
    # The private key + contact/email MUST NEVER be in the payload.
    raw = json.dumps(p)
    assert op.operator_secret not in raw
    assert "me@example.com" not in raw
    assert "operator_secret" not in raw
    assert "contact" not in raw


async def test_register_without_operator_omits_operator_fields(monkeypatch):
    # No operator identity bound → no operator_delegation, no operator_display_name.
    cfg = NodeConfig(node_id="n2", endpoint="http://h.test:9484", intent=CHAT, model="m")
    node = IicpNode(cfg)
    captured: dict = {}

    async def _fake_post(url, json=None, **kw):  # noqa: A002
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(node._http, "post", _fake_post)
    await node.register()
    assert "operator_delegation" not in captured["payload"]
    assert "operator_display_name" not in captured["payload"]
