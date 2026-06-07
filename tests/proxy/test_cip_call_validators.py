"""CIP-V01/CIP-V02: CIPCallFields wire-boundary validation tests (S.12 §4.1).

Validates that the coordinator rejects invalid `cip.policy` and `cip.replicas`
values in incoming CIP CALL requests with 422 IICP-E028.

CORC D5 — CIP Wire Format Validation.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from iicp_client.proxy.cip.consumer import CIPCallFields


def _cip(**overrides: object) -> dict:
    base = {"policy": "best_of_n", "replicas": 2}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# CIP-V01: cip.policy validation
# ---------------------------------------------------------------------------


def test_policy_accepts_best_of_n():
    """CIP-V01: policy='best_of_n' is valid (normative value §4.1)."""
    f = CIPCallFields(**_cip(policy="best_of_n"))
    assert f.policy == "best_of_n"


def test_policy_accepts_majority_vote():
    """CIP-V01: policy='majority_vote' is valid (normative value §4.1); replicas must be odd ≥ 3."""
    f = CIPCallFields(**_cip(policy="majority_vote", replicas=3))
    assert f.policy == "majority_vote"


def test_policy_accepts_map_reduce():
    """CIP-V01: policy='map_reduce' is valid (normative value §4.1)."""
    f = CIPCallFields(**_cip(policy="map_reduce"))
    assert f.policy == "map_reduce"


def test_policy_rejects_unknown_value():
    """CIP-V01: unknown policy value → ValidationError (maps to 422 IICP-E028)."""
    with pytest.raises(ValidationError) as exc_info:
        CIPCallFields(**_cip(policy="round_robin"))
    assert "policy" in str(exc_info.value).lower() or "best_of_n" in str(exc_info.value)


def test_policy_rejects_empty_string():
    """CIP-V01: empty policy string → ValidationError (maps to 422 IICP-E028)."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(policy=""))


def test_policy_rejects_none():
    """CIP-V01: None policy → ValidationError — field is required."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(policy=None))


def test_policy_rejects_uppercase_variant():
    """CIP-V01: 'BEST_OF_N' (uppercase) → ValidationError — policy is case-sensitive."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(policy="BEST_OF_N"))


# ---------------------------------------------------------------------------
# CIP-V02: cip.replicas validation
# ---------------------------------------------------------------------------


def test_replicas_accepts_minimum():
    """CIP-V02: replicas=1 (minimum) is valid per [1, 10] range."""
    f = CIPCallFields(**_cip(replicas=1))
    assert f.replicas == 1


def test_replicas_accepts_maximum():
    """CIP-V02: replicas=10 (maximum) is valid per [1, 10] range."""
    f = CIPCallFields(**_cip(replicas=10))
    assert f.replicas == 10


def test_replicas_accepts_midrange():
    """CIP-V02: replicas=5 (midrange) is valid."""
    f = CIPCallFields(**_cip(replicas=5))
    assert f.replicas == 5


def test_replicas_rejects_zero():
    """CIP-V02: replicas=0 → ValidationError (maps to 422 IICP-E028)."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(replicas=0))


def test_replicas_rejects_negative():
    """CIP-V02: negative replicas → ValidationError (maps to 422 IICP-E028)."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(replicas=-1))


def test_replicas_rejects_above_maximum():
    """CIP-V02: replicas=11 (above maximum) → ValidationError (maps to 422 IICP-E028)."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(replicas=11))


def test_replicas_rejects_far_above_maximum():
    """CIP-V02: replicas=999 (far above range) → ValidationError."""
    with pytest.raises(ValidationError):
        CIPCallFields(**_cip(replicas=999))


# ---------------------------------------------------------------------------
# Combined / happy path
# ---------------------------------------------------------------------------


def test_valid_cip_call_fields_accepted():
    """CIP-V01+V02: valid policy + replicas combination accepted."""
    f = CIPCallFields(policy="majority_vote", replicas=3)
    assert f.policy == "majority_vote"
    assert f.replicas == 3
    assert f.quorum is None


def test_quorum_optional_accepted():
    """quorum is optional — None and integer both accepted."""
    f1 = CIPCallFields(policy="best_of_n", replicas=2, quorum=None)
    f2 = CIPCallFields(policy="majority_vote", replicas=3, quorum=2)
    assert f1.quorum is None
    assert f2.quorum == 2


# ---------------------------------------------------------------------------
# CIP-V02a majority_vote odd-replicas constraint (S.12 §3.2 IICP-E025)
# model_validator(mode="after") runs automatically at construction time
# ---------------------------------------------------------------------------


def test_majority_vote_even_replicas_rejected():
    """IICP-E025: majority_vote with even replicas → ValidationError at construction (S.12 §3.2)."""
    with pytest.raises(ValidationError, match="IICP-E025"):
        CIPCallFields(policy="majority_vote", replicas=4)


def test_majority_vote_replicas_less_than_3_rejected():
    """IICP-E025: majority_vote with replicas < 3 → ValidationError at construction (S.12 §3.2)."""
    with pytest.raises(ValidationError, match="IICP-E025"):
        CIPCallFields(policy="majority_vote", replicas=1)


def test_majority_vote_replicas_3_accepted():
    """majority_vote with replicas=3 (odd, ≥ 3) → accepted."""
    f = CIPCallFields(policy="majority_vote", replicas=3)
    assert f.replicas == 3


def test_majority_vote_replicas_5_accepted():
    """majority_vote with replicas=5 (odd, ≥ 3) → accepted."""
    f = CIPCallFields(policy="majority_vote", replicas=5)
    assert f.replicas == 5


def test_majority_vote_replicas_2_rejected():
    """IICP-E025: replicas=2 is even → rejected at construction (minimum odd is 3)."""
    with pytest.raises(ValidationError, match="IICP-E025"):
        CIPCallFields(policy="majority_vote", replicas=2)


def test_best_of_n_even_replicas_accepted():
    """Non-majority_vote policies are not subject to the odd-replicas constraint."""
    f = CIPCallFields(policy="best_of_n", replicas=2)
    assert f.replicas == 2


def test_map_reduce_even_replicas_accepted():
    """map_reduce is not subject to the odd-replicas constraint."""
    f = CIPCallFields(policy="map_reduce", replicas=4)
    assert f.replicas == 4


# ---------------------------------------------------------------------------
# CIP-V07: cip.quorum cross-field constraint (S.12 §4.1 IICP-E028)
# quorum MUST be null OR a positive integer ≤ replicas
# ---------------------------------------------------------------------------


def test_quorum_null_accepted():
    """CIP-V07: quorum=None is always valid (optional field)."""
    f = CIPCallFields(policy="best_of_n", replicas=5, quorum=None)
    assert f.quorum is None


def test_quorum_equal_to_replicas_accepted():
    """CIP-V07: quorum == replicas is valid (full quorum)."""
    f = CIPCallFields(policy="best_of_n", replicas=5, quorum=5)
    assert f.quorum == 5


def test_quorum_less_than_replicas_accepted():
    """CIP-V07: quorum < replicas is valid (partial quorum)."""
    f = CIPCallFields(policy="best_of_n", replicas=5, quorum=3)
    assert f.quorum == 3


def test_quorum_one_accepted():
    """CIP-V07: quorum=1 (minimum) is valid."""
    f = CIPCallFields(policy="best_of_n", replicas=5, quorum=1)
    assert f.quorum == 1


def test_quorum_exceeds_replicas_rejected():
    """CIP-V07: quorum > replicas → ValidationError IICP-E028 (S.12 §4.1)."""
    with pytest.raises(ValidationError, match="IICP-E028"):
        CIPCallFields(policy="best_of_n", replicas=3, quorum=4)


def test_quorum_far_exceeds_replicas_rejected():
    """CIP-V07: quorum >> replicas → ValidationError IICP-E028."""
    with pytest.raises(ValidationError, match="IICP-E028"):
        CIPCallFields(policy="best_of_n", replicas=2, quorum=10)


def test_quorum_zero_rejected():
    """CIP-V07: quorum=0 is not a positive integer → ValidationError IICP-E028 (S.12 §4.1)."""
    with pytest.raises(ValidationError, match="IICP-E028"):
        CIPCallFields(policy="best_of_n", replicas=5, quorum=0)


def test_quorum_negative_rejected():
    """CIP-V07: negative quorum → ValidationError IICP-E028."""
    with pytest.raises(ValidationError, match="IICP-E028"):
        CIPCallFields(policy="best_of_n", replicas=5, quorum=-1)


def test_majority_vote_quorum_valid():
    """CIP-V07 + CIP-V02a: majority_vote with valid quorum ≤ replicas → accepted."""
    f = CIPCallFields(policy="majority_vote", replicas=5, quorum=3)
    assert f.quorum == 3
    assert f.replicas == 5


# ---------------------------------------------------------------------------
# CIP-V08: cip object present but policy key absent
# ---------------------------------------------------------------------------


def test_policy_key_absent_in_cip_object_rejected():
    """CIP-V08: cip object with no policy key → ValidationError (S.12 §4.1).

    The spec requires: 'A Coordinator MUST NOT process a CIP CALL where the
    cip object is present but cip.policy is absent.' Pydantic enforces this
    because policy is a required field (no default). The absent-key case is
    distinct from policy=None (covered by CIP-V01 test_policy_rejects_none).
    """
    with pytest.raises(ValidationError):
        CIPCallFields(replicas=3)  # policy key entirely absent
