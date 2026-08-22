import os
import time

import httpx
import pytest

from iicp_client import ClientConfig, IicpClient, RestrictedDirectoryContext, SecretRef
from iicp_client.errors import IicpError
from iicp_client.restricted_directory import PROFILE_ID, validate_decision


def context() -> RestrictedDirectoryContext:
    os.environ["IICP_TEST_RESTRICTED_MEMBER"] = "member-token"
    return RestrictedDirectoryContext("domain-a", "did:iicp:test:directory-a", "client-a", "client", 7, SecretRef("environment", "IICP_TEST_RESTRICTED_MEMBER"))


def decision(operation: str = "discovery", **changes: object) -> dict[str, object]:
    value = {"schema": "iicp.restricted-trust-domain.directory-decision.v0", "profile": PROFILE_ID,
             "decision": "eligible", "operation": operation, "domain_id": "domain-a",
             "authority_id": "did:iicp:test:directory-a", "subject_kind": "client",
             "membership_generation": 7, "membership_expires_at": int(time.time()) + 300}
    value.update(changes)
    return {"restricted_domain_decision": value}


def test_context_and_decision_fail_closed(tmp_path):
    assert validate_decision(decision(), context(), "discovery").membership_generation == 7
    for body in ({}, decision(operation="bootstrap"), decision(domain_id="domain-b"), decision(membership_generation=6), decision(membership_expires_at=1)):
        with pytest.raises(IicpError):
            validate_decision(body, context(), "discovery")
    secret = tmp_path / "member"
    secret.write_text("secret")
    secret.chmod(0o644)
    with pytest.raises(IicpError):
        SecretRef("file", str(secret)).resolve()


@pytest.mark.asyncio
async def test_restricted_discovery_sends_membership_and_requires_decision(monkeypatch):
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        body = {**decision(), "nodes": []}
        return httpx.Response(200, json=body)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: original(transport=httpx.MockTransport(handler), **{k:v for k,v in kw.items() if k != "transport"}))
    client = IicpClient(ClientConfig(directory_url="https://directory.test", route_discovery_mode="ticketed", restricted_directory=context()))
    await client.discover_async("urn:iicp:intent:llm:chat:v1")
    assert seen["x-iicp-membership"] == "member-token"
    assert seen["x-iicp-subject-id"] == "client-a"


def test_restricted_mode_refuses_legacy_fallback():
    with pytest.raises(ValueError, match="legacy"):
        IicpClient(ClientConfig(route_discovery_mode="legacy", restricted_directory=context()))
