from __future__ import annotations

from iicp_client.effective_capability import (
    CapabilityClaimProvenance,
    EffectiveCapability,
)
from iicp_client.node import IicpNode, NodeConfig

CHAT = "urn:iicp:intent:llm:chat:v1"


class _Response:
    status_code = 201

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, str]:
        return {"node_token": "token", "node_hmac_key": "hmac"}


async def test_explicit_effective_variants_replace_model_name_heuristics(monkeypatch) -> None:
    explicit = EffectiveCapability(
        intent=CHAT,
        variant_id="explicit-vision",
        models=("custom-model",),
        input_modalities=("text", "image"),
        output_modalities=("text",),
        claim_provenance=CapabilityClaimProvenance(source="runtime_introspection"),
    )
    node = IicpNode(
        NodeConfig(
            node_id="node-effective",
            endpoint="https://node.invalid",
            intent=CHAT,
            model="qwen-vl-heuristic-name",
            effective_capabilities=[explicit],
        )
    )
    captured: dict[str, object] = {}

    async def fake_post(_url: str, json=None, **_kwargs):
        captured["payload"] = json
        return _Response()

    monkeypatch.setattr(node._http, "post", fake_post)
    await node.register()

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["capabilities"] == [
        {
            "intent": CHAT,
            "variant_id": "explicit-vision",
            "models": ["custom-model"],
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "claim_provenance": {"source": "runtime_introspection"},
        }
    ]
