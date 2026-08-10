import json
from pathlib import Path

import pytest

from iicp_client.native_call_identity import NativeCallIdentityError, NativeCallIdentityRegistry

FIXTURE = Path(__file__).parents[1] / "parity" / "service-profiles-v1.json"


def _vector(vector_id: str):
    vectors = json.loads(FIXTURE.read_text())["lifecycle_vectors"]
    return next(item for item in vectors if item["id"] == vector_id)


@pytest.mark.parametrize("vector_id", ["SERVICE-LIFECYCLE-21", "SERVICE-LIFECYCLE-22"])
def test_accepts_native_call_identity_vectors(vector_id):
    registry = NativeCallIdentityRegistry()
    for call in _vector(vector_id)["calls"]:
        registry.accept(call)


def test_rejects_missing_and_conflicting_task_identity():
    registry = NativeCallIdentityRegistry()
    errors = []
    for call in _vector("SERVICE-LIFECYCLE-23")["calls"]:
        try:
            registry.accept(call)
        except NativeCallIdentityError as error:
            errors.append(error.code)
    assert errors == ["missing_task_id", "task_identity_conflict"]


def test_unnegotiated_call_does_not_require_task_identity():
    NativeCallIdentityRegistry().accept({"call_id": "base-call"})
