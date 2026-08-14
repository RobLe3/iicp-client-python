"""Binding-neutral effective capability advertisement and matching.

This module implements the pre-normative
``urn:iicp:profile:effective-capability:v1`` parity contract.  It does not
perform discovery or dispatch: callers must provide an already applicable
advertisement and remain responsible for policy and final route validation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

EFFECTIVE_CAPABILITY_PROFILE_ID = "urn:iicp:profile:effective-capability:v1"
EFFECTIVE_CAPABILITY_SCHEMA_VERSION = "1.0.0"

CapabilityClass = Literal[
    "input_modality",
    "output_modality",
    "feature",
    "execution_capability",
    "profile",
]

REFUSAL_REQUIRED_UNKNOWN = "required_capability_unknown"
REFUSAL_REQUIRED_UNSUPPORTED = "required_capability_unsupported"
REFUSAL_REQUIRED_STALE = "required_capability_stale"
REFUSAL_LIMIT_UNSATISFIED = "capability_limit_unsatisfied"
REFUSAL_POLICY_DENIED = "capability_policy_denied"


@dataclass(frozen=True)
class CapabilityLimit:
    value: float
    unit: str


@dataclass(frozen=True)
class CapabilityClaimProvenance:
    source: str
    observed_at: str | None = None
    valid_until: str | None = None
    evidence_ref: str | None = None


@dataclass(frozen=True)
class CapabilityExtension:
    required: bool
    value: Any


@dataclass(frozen=True)
class EffectiveCapability:
    """One complete, non-unionable service-path variant for an intent."""

    intent: str
    version: str | None = None
    phase: int | None = None
    variant_id: str | None = None
    models: tuple[str, ...] = ()
    max_tokens: int | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    execution_capabilities: tuple[str, ...] = ()
    limits: Mapping[str, CapabilityLimit] = field(default_factory=dict)
    supported_profiles: tuple[str, ...] = ()
    claim_provenance: CapabilityClaimProvenance | None = None
    extensions: Mapping[str, CapabilityExtension] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class CapabilityRequirement:
    capability_class: str
    identifier: str


@dataclass(frozen=True)
class CapabilityLimitRequirement:
    identifier: str
    operator: str
    value: float
    unit: str


@dataclass(frozen=True)
class CapabilityRequirements:
    intent: str
    requires: tuple[CapabilityRequirement, ...] = ()
    prefers: tuple[CapabilityRequirement, ...] = ()
    limits: tuple[CapabilityLimitRequirement, ...] = ()


@dataclass(frozen=True)
class EffectiveCapabilityMatch:
    eligible: bool
    variant_ids: tuple[str | None, ...] = ()
    preference_unavailable: bool = False
    refusal: str | None = None
    preserved_extensions: tuple[str, ...] = ()


def _parse_time(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("capability timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field_name} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(value)


def effective_capability_from_dict(raw: Mapping[str, Any]) -> EffectiveCapability:
    """Parse one canonical advertisement variant without accepting shadow fields."""

    allowed = {
        "intent",
        "version",
        "phase",
        "variant_id",
        "models",
        "max_tokens",
        "input_modalities",
        "output_modalities",
        "features",
        "execution_capabilities",
        "limits",
        "supported_profiles",
        "claim_provenance",
        "extensions",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown effective capability field(s): {', '.join(sorted(unknown))}")
    intent = raw.get("intent")
    if not isinstance(intent, str) or not intent:
        raise ValueError("intent is required")

    raw_limits = raw.get("limits", {})
    if not isinstance(raw_limits, dict):
        raise ValueError("limits must be an object")
    limits: dict[str, CapabilityLimit] = {}
    for identifier, raw_limit in raw_limits.items():
        if not isinstance(identifier, str) or not isinstance(raw_limit, dict):
            raise ValueError("each limit must be a named object")
        if set(raw_limit) != {"value", "unit"}:
            raise ValueError("each limit requires only value and unit")
        value = raw_limit["value"]
        unit = raw_limit["unit"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError("limit value must be a non-negative number")
        if unit not in {"tokens", "items", "bytes", "milliseconds", "dimensions"}:
            raise ValueError("limit unit is unsupported")
        limits[identifier] = CapabilityLimit(float(value), unit)

    provenance = None
    raw_provenance = raw.get("claim_provenance")
    if raw_provenance is not None:
        if not isinstance(raw_provenance, dict):
            raise ValueError("claim_provenance must be an object")
        source = raw_provenance.get("source")
        if source not in {
            "heuristic_fallback",
            "operator_assertion",
            "provider_metadata",
            "runtime_introspection",
            "conformance_probe",
        }:
            raise ValueError("claim_provenance source is unsupported")
        for key in ("observed_at", "valid_until"):
            value = raw_provenance.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"claim_provenance {key} must be a string")
                _parse_time(value)
        provenance = CapabilityClaimProvenance(
            source=source,
            observed_at=raw_provenance.get("observed_at"),
            valid_until=raw_provenance.get("valid_until"),
            evidence_ref=raw_provenance.get("evidence_ref"),
        )

    raw_extensions = raw.get("extensions", {})
    if not isinstance(raw_extensions, dict):
        raise ValueError("extensions must be an object")
    extensions: dict[str, CapabilityExtension] = {}
    for identifier, raw_extension in raw_extensions.items():
        if not isinstance(raw_extension, dict) or set(raw_extension) != {"required", "value"}:
            raise ValueError("each extension requires only required and value")
        required = raw_extension["required"]
        if not isinstance(required, bool):
            raise ValueError("extension required must be boolean")
        extensions[identifier] = CapabilityExtension(required=required, value=raw_extension["value"])

    phase = raw.get("phase")
    max_tokens = raw.get("max_tokens")
    if phase is not None and (not isinstance(phase, int) or isinstance(phase, bool) or phase < 1):
        raise ValueError("phase must be a positive integer")
    if max_tokens is not None and (not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1):
        raise ValueError("max_tokens must be a positive integer")

    return EffectiveCapability(
        intent=intent,
        version=raw.get("version"),
        phase=phase,
        variant_id=raw.get("variant_id"),
        models=_string_tuple(raw.get("models"), "models"),
        max_tokens=max_tokens,
        input_modalities=_string_tuple(raw.get("input_modalities"), "input_modalities"),
        output_modalities=_string_tuple(raw.get("output_modalities"), "output_modalities"),
        features=_string_tuple(raw.get("features"), "features"),
        execution_capabilities=_string_tuple(raw.get("execution_capabilities"), "execution_capabilities"),
        limits=limits,
        supported_profiles=_string_tuple(raw.get("supported_profiles"), "supported_profiles"),
        claim_provenance=provenance,
        extensions=extensions,
    )


def effective_capabilities_from_advertisement(raw: Mapping[str, Any]) -> tuple[EffectiveCapability, ...]:
    """Validate and parse the v1 advertisement wrapper."""

    if set(raw) != {"schema_version", "capabilities"}:
        raise ValueError("advertisement requires only schema_version and capabilities")
    if raw.get("schema_version") != EFFECTIVE_CAPABILITY_SCHEMA_VERSION:
        raise ValueError("unsupported effective capability schema_version")
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("capabilities must be a non-empty array")
    parsed = tuple(effective_capability_from_dict(item) for item in capabilities)
    identities = [(item.intent, item.variant_id) for item in parsed]
    if len(identities) != len(set(identities)):
        raise ValueError("effective capability variants must be unique within an advertisement")
    return parsed


def resolve_effective_capabilities(
    *,
    explicit: Sequence[EffectiveCapability] = (),
    introspected: Sequence[EffectiveCapability] = (),
    heuristic: Sequence[EffectiveCapability] = (),
) -> tuple[EffectiveCapability, ...]:
    """Apply source precedence without combining incompatible evidence sets.

    Explicit operator/provider configuration wins, followed by runtime
    introspection. Heuristic fallback is used only when no stronger evidence is
    present and must be labelled ``heuristic_fallback``.
    """

    if explicit:
        return tuple(explicit)
    if introspected:
        return tuple(introspected)
    if any(item.claim_provenance is None or item.claim_provenance.source != "heuristic_fallback" for item in heuristic):
        raise ValueError("heuristic capability evidence must be labelled heuristic_fallback")
    return tuple(heuristic)


def _values(candidate: EffectiveCapability, capability_class: str) -> tuple[str, ...] | None:
    return {
        "input_modality": candidate.input_modalities,
        "output_modality": candidate.output_modalities,
        "feature": candidate.features,
        "execution_capability": candidate.execution_capabilities,
        "profile": candidate.supported_profiles,
    }.get(capability_class)


def _refusal(code: str) -> EffectiveCapabilityMatch:
    return EffectiveCapabilityMatch(eligible=False, refusal=code)


def match_effective_capabilities(
    capabilities: Sequence[EffectiveCapability],
    request: CapabilityRequirements,
    vocabulary: Mapping[str, Sequence[str]],
    evaluated_at: datetime,
    policy_denials: Sequence[CapabilityRequirement] = (),
) -> EffectiveCapabilityMatch:
    """Classify an already policy-scoped request against complete variants."""

    denied = set(policy_denials)
    for requirement in request.requires:
        if (
            requirement.capability_class not in vocabulary
            or requirement.identifier not in vocabulary[requirement.capability_class]
        ):
            return _refusal(REFUSAL_REQUIRED_UNKNOWN)
        if requirement in denied:
            return _refusal(REFUSAL_POLICY_DENIED)

    candidates = [
        candidate
        for candidate in capabilities
        if candidate.intent == request.intent
        and all(
            (values := _values(candidate, requirement.capability_class)) is not None
            and requirement.identifier in values
            for requirement in request.requires
        )
    ]
    if not candidates:
        return _refusal(REFUSAL_REQUIRED_UNSUPPORTED)

    at = evaluated_at.astimezone(UTC)
    fresh = [
        candidate
        for candidate in candidates
        if candidate.claim_provenance is None
        or candidate.claim_provenance.valid_until is None
        or _parse_time(candidate.claim_provenance.valid_until) >= at
    ]
    if not fresh:
        return _refusal(REFUSAL_REQUIRED_STALE)

    matching_limits = [
        candidate
        for candidate in fresh
        if all(
            (actual := candidate.limits.get(required.identifier)) is not None
            and actual.unit == required.unit
            and (
                (required.operator == "gte" and actual.value >= required.value)
                or (required.operator == "lte" and actual.value <= required.value)
                or (required.operator == "eq" and actual.value == required.value)
            )
            for required in request.limits
        )
    ]
    if not matching_limits:
        return _refusal(REFUSAL_LIMIT_UNSATISFIED)

    preference_unavailable = any(
        preference.capability_class not in vocabulary
        or preference.identifier not in vocabulary[preference.capability_class]
        or not any(
            (values := _values(candidate, preference.capability_class)) is not None and preference.identifier in values
            for candidate in matching_limits
        )
        for preference in request.prefers
    )
    preserved_extensions = tuple(
        sorted({identifier for candidate in matching_limits for identifier in candidate.extensions})
    )
    return EffectiveCapabilityMatch(
        eligible=True,
        variant_ids=tuple(candidate.variant_id for candidate in matching_limits),
        preference_unavailable=preference_unavailable,
        preserved_extensions=preserved_extensions,
    )
