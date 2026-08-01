import pytest

from iicp_client import IicpClient


@pytest.mark.asyncio
async def test_discovery_exposes_additive_evidence_without_requiring_it(monkeypatch):
    async def fake_get_json(*_args, **_kwargs):
        return {
            "nodes": [{
                "node_id": "node-a",
                "endpoint": "https://node.example.com",
                "score": 0.8,
                "available": True,
                "region": "eu-central",
                "latency_evidence": {"estimate_ms": 143, "basis": "multi_proxy_ema"},
                "health_reasons": [{"dimension": "backend", "state": "ok", "reason": "ok", "evidence": "self_reported"}],
                "trust_progress": {"gold_task_threshold_met": True, "remaining_gold_requirements": []},
                "sdk_release": {"compatibility": "current", "relation": "latest_known"},
            }],
            "diversity_evidence": {"nodes": 1, "distinct_verified_operators": 1},
        }

    monkeypatch.setattr("iicp_client.client.get_json", fake_get_json)
    result = await IicpClient().discover_async("urn:iicp:intent:llm:chat:v1")

    assert result.nodes[0].latency_evidence == {"estimate_ms": 143, "basis": "multi_proxy_ema"}
    assert result.nodes[0].health_reasons[0]["dimension"] == "backend"
    assert result.nodes[0].trust_progress["gold_task_threshold_met"] is True
    assert result.nodes[0].sdk_release["relation"] == "latest_known"
    assert result.diversity_evidence["distinct_verified_operators"] == 1


@pytest.mark.asyncio
async def test_older_discovery_without_evidence_remains_compatible(monkeypatch):
    async def fake_get_json(*_args, **_kwargs):
        return {"nodes": [{
            "node_id": "node-a",
            "endpoint": "https://node.example.com",
            "score": 0.8,
            "available": True,
            "region": "eu-central",
        }]}

    monkeypatch.setattr("iicp_client.client.get_json", fake_get_json)
    result = await IicpClient().discover_async("urn:iicp:intent:llm:chat:v1")
    assert result.nodes[0].health_reasons is None
    assert result.diversity_evidence is None
