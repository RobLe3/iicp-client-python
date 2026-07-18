from __future__ import annotations

import json

import pytest

from iicp_client.cli import _build_parser, _resolve_receipt_profiles
from iicp_client.identity import NodeIdentity


def test_receipt_profile_cli_is_repeatable() -> None:
    args = _build_parser().parse_args([
        "serve",
        "--receipt-profile",
        "consumer_cosignature_v1",
        "--receipt-profile",
        "consumer_cosignature_v1",
    ])
    assert _resolve_receipt_profiles(args.receipt_profile, None, None) == [
        "consumer_cosignature_v1"
    ]


def test_receipt_profile_precedence_and_validation() -> None:
    assert _resolve_receipt_profiles(None, "consumer_cosignature_v1", ["saved"]) == [
        "consumer_cosignature_v1"
    ]
    assert _resolve_receipt_profiles([], "consumer_cosignature_v1", ["saved"]) == []
    with pytest.raises(ValueError, match="unsupported receipt profile"):
        _resolve_receipt_profiles(["unknown_v1"], None, None)


def test_old_saved_node_defaults_to_no_receipt_profiles() -> None:
    raw = {
        "node_id": "node-1",
        "operator_id": "operator-1",
        "name": "node",
        "backend_url": "http://127.0.0.1:11434",
        "model": "model",
    }
    node = NodeIdentity(**json.loads(json.dumps(raw)))
    assert node.supported_receipt_profiles == []
