import pytest

from iicp_client import ClientConfig, DiscoverOptions, IicpClient, ProfileRequest
from iicp_client.errors import IicpError


@pytest.mark.asyncio
async def test_required_profile_negotiation_is_sent_and_exposed(monkeypatch):
    captured = {}

    async def fake_get_json(_url, *, params, **_kwargs):
        captured.update(params)
        return {
            "nodes": [{"node_id": "node-a", "endpoint": "https://node.example.com", "score": 0.8, "available": True, "region": "eu-central"}],
            "profile_negotiation": {"requested": True, "status": "compatible", "reason": "compatible", "dispatch_allowed": True},
        }

    monkeypatch.setattr("iicp_client.client.get_json", fake_get_json)
    request = ProfileRequest("iicp.profile.compatibility.v0", "0.3.0-draft", "a" * 64, required=True)
    result = await IicpClient(ClientConfig()).discover_async("urn:iicp:intent:llm:chat:v1", DiscoverOptions(profile_request=request))
    assert captured["profile_id"] == request.profile_id
    assert captured["profile_required"] == "true"
    assert result.profile_negotiation and result.profile_negotiation.status == "compatible"


@pytest.mark.asyncio
async def test_required_profile_negotiation_fails_closed_when_directory_omits_result(monkeypatch):
    async def fake_get_json(*_args, **_kwargs):
        return {"nodes": []}

    monkeypatch.setattr("iicp_client.client.get_json", fake_get_json)
    request = ProfileRequest("iicp.profile.compatibility.v0", "0.3.0-draft", "a" * 64, required=True)
    with pytest.raises(IicpError, match="required pre-normative profile"):
        await IicpClient().discover_async("urn:iicp:intent:llm:chat:v1", DiscoverOptions(profile_request=request))
