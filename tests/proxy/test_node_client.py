"""CIP-CALL-01: NodeClient wire-body tests — cip envelope in outbound CALL.

Verifies that NodeClient.submit_task() places the cip_envelope in the JSON
body under the "cip" key (S.12 §4.1) and omits it entirely when None.

CORC D5 — CIP Wire Format / CIP-CALL-01.
"""
from __future__ import annotations

from uuid import UUID

import pytest
import respx
from httpx import Response

from iicp_client.proxy.clients.node import NodeClient

ENDPOINT = "https://worker.test"
TOKEN = "test-node-token"
TASK_ID = UUID("550e8400-e29b-41d4-a716-000000000001")
INTENT = "urn:iicp:intent:llm:chat:v1"
PAYLOAD = {"messages": [{"role": "user", "content": "hi"}]}
TIMEOUT_MS = 3000

_WORKER_OK = Response(200, json={"status": "success", "result": {"content": "ok"}})


# ---------------------------------------------------------------------------
# CIP-CALL-01: cip envelope included when coordinator dispatches sub-task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_submit_task_includes_cip_envelope_in_body():
    """CIP-CALL-01: cip_envelope must appear as body['cip'] in the outbound CALL.

    S.12 §4.1: The CALL body sent to a worker MUST carry a 'cip' object containing
    at minimum cip_role='worker' and cip_session_key. Without it the worker
    adapter cannot identify the request as a CIP sub-task and will not produce
    a cip_receipt.
    """
    captured: list[dict] = []

    def capture_request(request):
        captured.append(request.content)
        return _WORKER_OK

    respx.post(f"{ENDPOINT}/v1/task").mock(side_effect=capture_request)

    cip_env = {
        "cip_role": "worker",
        "cip_session_key": "sess-xyz-123",
        "cip_parent_task_id": str(TASK_ID),
    }
    client = NodeClient(ENDPOINT, TOKEN)
    await client.submit_task(TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS, cip_envelope=cip_env)

    import json
    body = json.loads(captured[0])
    assert body["cip"]["cip_role"] == "worker"
    assert body["cip"]["cip_session_key"] == "sess-xyz-123"
    assert body["cip"]["cip_parent_task_id"] == str(TASK_ID)


@pytest.mark.asyncio
@respx.mock
async def test_submit_task_omits_cip_key_when_envelope_is_none():
    """CIP-CALL-01: when cip_envelope is None (non-CIP dispatch), body must not contain 'cip'."""
    captured: list[dict] = []

    def capture_request(request):
        captured.append(request.content)
        return _WORKER_OK

    respx.post(f"{ENDPOINT}/v1/task").mock(side_effect=capture_request)

    client = NodeClient(ENDPOINT, TOKEN)
    await client.submit_task(TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    import json
    body = json.loads(captured[0])
    assert "cip" not in body


# ---------------------------------------------------------------------------
# CIP-CALL-06: trace.cip_role = "coordinator" when dispatching CIP sub-task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_submit_task_sets_cip_role_coordinator_when_cip_envelope_provided():
    """CIP-CALL-06: trace.cip_role MUST be 'coordinator' when dispatching a CIP sub-task.

    S.12 §4.2: The Coordinator MUST set trace.cip_role='coordinator' in its own
    CALL trace when initiating CIP dispatch so the worker can identify the origin.
    """
    captured: list[dict] = []

    def capture_request(request):
        captured.append(request.content)
        return _WORKER_OK

    respx.post(f"{ENDPOINT}/v1/task").mock(side_effect=capture_request)

    cip_env = {
        "cip_role": "worker",
        "cip_session_key": "sess-xyz-123",
        "cip_parent_task_id": str(TASK_ID),
    }
    client = NodeClient(ENDPOINT, TOKEN)
    await client.submit_task(
        TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS, trace_id="trace-abc", cip_envelope=cip_env
    )

    import json
    body = json.loads(captured[0])
    assert body["trace"]["cip_role"] == "coordinator"
    assert body["trace"]["trace_id"] == "trace-abc"


@pytest.mark.asyncio
@respx.mock
async def test_submit_task_omits_cip_role_from_trace_for_non_cip_dispatch():
    """CIP-CALL-06: trace MUST NOT contain cip_role for non-CIP dispatches."""
    captured: list[dict] = []

    def capture_request(request):
        captured.append(request.content)
        return _WORKER_OK

    respx.post(f"{ENDPOINT}/v1/task").mock(side_effect=capture_request)

    client = NodeClient(ENDPOINT, TOKEN)
    await client.submit_task(TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS, trace_id="trace-abc")

    import json
    body = json.loads(captured[0])
    assert "cip_role" not in body.get("trace", {})


@pytest.mark.asyncio
@respx.mock
async def test_submit_task_cip_role_coordinator_without_trace_id():
    """CIP-CALL-06: trace.cip_role appears even when no trace_id is provided."""
    captured: list[dict] = []

    def capture_request(request):
        captured.append(request.content)
        return _WORKER_OK

    respx.post(f"{ENDPOINT}/v1/task").mock(side_effect=capture_request)

    cip_env = {
        "cip_role": "worker",
        "cip_session_key": "sess-xyz-456",
        "cip_parent_task_id": str(TASK_ID),
    }
    client = NodeClient(ENDPOINT, TOKEN)
    await client.submit_task(TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS, cip_envelope=cip_env)

    import json
    body = json.loads(captured[0])
    assert body["trace"]["cip_role"] == "coordinator"
    assert "trace_id" not in body["trace"]
