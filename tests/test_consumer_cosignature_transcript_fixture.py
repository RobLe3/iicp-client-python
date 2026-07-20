from __future__ import annotations

import json
from pathlib import Path

FIXTURE = Path(__file__).parents[1] / "parity/cip-consumer-cosignature-transcript-v1.json"


def test_consumer_cosignature_transcript_is_content_free_and_fail_closed() -> None:
    data = json.loads(FIXTURE.read_text())
    messages = [step["message"] for step in data["transcript"]]
    assert [message["type"] for message in messages] == [
        "receipt_offer", "receipt_acceptance", "settlement_request"
    ]
    assert len({message["receipt_digest_hex"] for message in messages}) == 1
    assert data["privacy_contract"]["content_free"] is True
    rendered = json.dumps(data)
    for field in data["privacy_contract"]["forbidden_fields"]:
        assert f'"{field}":' not in rendered
    modes = {item["mode"]: item for item in data["transition_modes"]}
    assert modes["legacy"]["authoritative_path"] == "existing_hmac_receipt"
    assert modes["observe"]["economic_effect"] == "no_additional_award_or_debit"
    assert modes["required"]["runtime_status"] == "unavailable"
    assert not any(item["strict_enforcement_authorized"] for item in modes.values())
