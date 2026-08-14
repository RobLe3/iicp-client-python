from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from iicp_client.effective_capability import (
    EFFECTIVE_CAPABILITY_PROFILE_ID,
    CapabilityClaimProvenance,
    CapabilityLimitRequirement,
    CapabilityRequirement,
    CapabilityRequirements,
    EffectiveCapability,
    effective_capabilities_from_advertisement,
    match_effective_capabilities,
    resolve_effective_capabilities,
)

PARITY_DIR = Path(__file__).parents[1] / "parity/effective-capability-v1"
FIXTURE_BYTES = (PARITY_DIR / "fixture.json").read_bytes()
FIXTURE = json.loads(FIXTURE_BYTES)
FIXTURE_SHA256 = "e6e3c32aa7c4cf814e639d3a97cd1c1cb49ac020ed6ebe7e1e16bc2314e14761"


def _requirement(raw: dict[str, object]) -> CapabilityRequirement:
    return CapabilityRequirement(str(raw["class"]), str(raw["id"]))


def _request(raw: dict[str, object]) -> CapabilityRequirements:
    return CapabilityRequirements(
        intent=str(raw["intent"]),
        requires=tuple(_requirement(item) for item in raw.get("requires", [])),  # type: ignore[arg-type]
        prefers=tuple(_requirement(item) for item in raw.get("prefers", [])),  # type: ignore[arg-type]
        limits=tuple(
            CapabilityLimitRequirement(
                identifier=str(item["id"]),
                operator=str(item["operator"]),
                value=float(item["value"]),
                unit=str(item["unit"]),
            )
            for item in raw.get("limits", [])  # type: ignore[union-attr]
        ),
    )


def test_shared_fixture_and_schema_digests_are_pinned() -> None:
    assert hashlib.sha256(FIXTURE_BYTES).hexdigest() == FIXTURE_SHA256
    assert FIXTURE["profile_id"] == EFFECTIVE_CAPABILITY_PROFILE_ID
    expected = {
        "advertisement.schema.json": "707da7eebc5e8b55a720386ca713c977beeadd640f4b09eb48ea99573d2b1ab0",
        "requirements.schema.json": "0d234ef4de420b977661d3222c3c9f433332e8224a3320175318338c76e760e9",
        "refusal.schema.json": "5d35b57c31eeb176bd7db72bfaf1ccaa84defe864bc63a10c59b97d689e52f9e",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((PARITY_DIR / name).read_bytes()).hexdigest() == digest


def test_shared_matching_scenarios_pass_without_cross_variant_union() -> None:
    capabilities = effective_capabilities_from_advertisement(FIXTURE["advertisement"])
    for scenario in FIXTURE["matching_scenarios"]:
        actual = match_effective_capabilities(
            capabilities,
            _request(scenario["request"]),
            FIXTURE["vocabulary"],
            datetime.fromisoformat(scenario.get("evaluation_time", FIXTURE["evaluation_time"]).replace("Z", "+00:00")),
            tuple(_requirement(item) for item in scenario.get("policy_denials", [])),
        )
        expected = scenario["expected"]
        assert actual.eligible is expected["eligible"], scenario["name"]
        if actual.eligible:
            assert list(actual.variant_ids) == expected["variant_ids"], scenario["name"]
            assert actual.preference_unavailable is expected.get("preference_unavailable", False), scenario["name"]
            if preserved := expected.get("preserved_extension"):
                assert preserved in actual.preserved_extensions, scenario["name"]
        else:
            assert actual.refusal == expected["refusal"]["code"], scenario["name"]


@pytest.mark.parametrize("case", FIXTURE["invalid_advertisements"], ids=lambda case: case["name"])
def test_invalid_shared_advertisements_are_rejected(case: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        effective_capabilities_from_advertisement(case["value"])  # type: ignore[arg-type]


def test_explicit_and_introspected_evidence_precede_labelled_heuristics() -> None:
    explicit = EffectiveCapability(intent="urn:iicp:intent:llm:chat:v1", variant_id="explicit")
    introspected = EffectiveCapability(intent=explicit.intent, variant_id="introspected")
    heuristic = EffectiveCapability(
        intent=explicit.intent,
        variant_id="heuristic",
        claim_provenance=CapabilityClaimProvenance(source="heuristic_fallback"),
    )
    assert resolve_effective_capabilities(explicit=[explicit], introspected=[introspected], heuristic=[heuristic]) == (
        explicit,
    )
    assert resolve_effective_capabilities(introspected=[introspected], heuristic=[heuristic]) == (introspected,)
    assert resolve_effective_capabilities(heuristic=[heuristic]) == (heuristic,)


def test_unlabelled_heuristic_is_rejected() -> None:
    capability = EffectiveCapability(intent="urn:iicp:intent:llm:chat:v1")
    with pytest.raises(ValueError, match="heuristic_fallback"):
        resolve_effective_capabilities(heuristic=[capability])
