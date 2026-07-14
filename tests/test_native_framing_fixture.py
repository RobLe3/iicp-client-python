"""Implementation-backed vectors for the established 12-byte native frame."""
from __future__ import annotations

import json
from pathlib import Path

from iicp_client.iicp_tcp import FRAME_HEADER_LEN, IicpFrame, MsgType

FIXTURE = Path(__file__).parent / "fixtures" / "native-framing-v1.json"


def test_native_frame_decoder_matches_canonical_implementation_backed_vectors() -> None:
    data = json.loads(FIXTURE.read_text())
    assert data["frame"]["header_bytes"] == FRAME_HEADER_LEN == 12

    expected_errors = {
        "invalid_magic": "Invalid IICP magic",
        "truncated_header": "frame too short",
        "truncated_payload": "payload truncated",
    }
    for scenario in data["scenarios"]:
        name = scenario["name"]
        wire = bytes.fromhex(scenario["wire_hex"])
        expected = scenario["expected"]
        if expected["outcome"] == "accept":
            frame, consumed = IicpFrame.decode(wire)
            assert frame.version == expected["version"], name
            assert frame.msg_type == expected["message_type"], name
            assert frame.flags == expected["flags"], name
            assert frame.payload == bytes.fromhex(expected["payload_hex"]), name
            assert consumed == expected["consumed"], name
        else:
            try:
                IicpFrame.decode(wire)
            except ValueError as error:
                assert expected_errors[expected["reason"]] in str(error), name
            else:
                raise AssertionError(f"{name}: expected rejection")


def test_native_frame_encoder_emits_the_canonical_empty_ping_vector() -> None:
    data = json.loads(FIXTURE.read_text())
    ping = next(scenario for scenario in data["scenarios"] if scenario["name"] == "ping_empty")
    assert IicpFrame.make(MsgType.PING, b"").encode() == bytes.fromhex(ping["wire_hex"])
