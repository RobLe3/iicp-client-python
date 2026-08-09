import json
from pathlib import Path

import pytest

from iicp_client.native_response_sequence import (
    NativeResponseSequence,
    NativeResponseSequenceError,
)

FIXTURE = Path(__file__).parents[1] / "parity" / "service-profiles-v1.json"


def _vectors():
    return {item["id"]: item for item in json.loads(FIXTURE.read_text())["lifecycle_vectors"]}


@pytest.mark.parametrize("vector_id", ["SERVICE-LIFECYCLE-14", "SERVICE-LIFECYCLE-15", "SERVICE-LIFECYCLE-16"])
def test_accepts_valid_native_sequences(vector_id):
    vector = _vectors()[vector_id]
    sequence = NativeResponseSequence(**vector["input"])
    for frame in vector["native_frames"]:
        sequence.accept(frame)
    sequence.finish()


@pytest.mark.parametrize(
    ("vector_id", "code"),
    [
        ("SERVICE-LIFECYCLE-17", "call_id_drift"),
        ("SERVICE-LIFECYCLE-18", "sequence_drift"),
        ("SERVICE-LIFECYCLE-19", "finality_disagreement"),
        ("SERVICE-LIFECYCLE-20", "response_after_terminal"),
    ],
)
def test_rejects_invalid_native_sequences(vector_id, code):
    vector = _vectors()[vector_id]
    sequence = NativeResponseSequence(**vector["input"])
    with pytest.raises(NativeResponseSequenceError) as caught:
        for frame in vector["native_frames"]:
            sequence.accept(frame)
        sequence.finish()
    assert caught.value.code == code


def test_finish_rejects_transport_close_before_terminal():
    sequence = NativeResponseSequence("session", "call", "task")
    with pytest.raises(NativeResponseSequenceError, match="missing_terminal_response"):
        sequence.finish()
