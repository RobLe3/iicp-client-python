import json
from pathlib import Path

from iicp_client.cli import _managed_operator_decision
from iicp_client.operator_profile import ManagedOperatorInput, evaluate_managed_operator


def test_shared_managed_operator_vectors():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "managed-operator-v1.json").read_text())
    for vector in fixture["vectors"]:
        accepted, reason = evaluate_managed_operator(ManagedOperatorInput(**vector["input"]))
        assert {"accepted": accepted, "reason": reason} == vector["expected"], vector["name"]


def test_vendored_fixture_matches_adjacent_authority_when_available():
    local = Path(__file__).parent / "fixtures" / "managed-operator-v1.json"
    authority = Path(__file__).parents[2] / "IICP" / "research" / "pre-normative-profiles" / "fixtures" / local.name
    if authority.exists():
        assert json.loads(local.read_text()) == json.loads(authority.read_text())


class _Operator:
    def __init__(self, encrypted=True):
        self.encrypted = encrypted

    def is_key_backed(self):
        return True

    def is_encrypted(self):
        return self.encrypted


class _Args:
    auto_detect_nat = False


def test_managed_startup_uses_real_update_and_exposure_defaults(monkeypatch):
    monkeypatch.setenv("IICP_OPERATOR_PROFILE", "managed")
    monkeypatch.setenv("IICP_AUTO_UPDATE", "0")
    assert _managed_operator_decision(_Args(), False, _Operator()) == (
        True,
        "managed_requirements_met",
    )


def test_managed_startup_rejects_automatic_tunnel_before_listener(monkeypatch):
    monkeypatch.setenv("IICP_OPERATOR_PROFILE", "managed")
    monkeypatch.setenv("IICP_AUTO_UPDATE", "0")
    assert _managed_operator_decision(_Args(), None, _Operator()) == (
        False,
        "tunnel_approval_required",
    )
