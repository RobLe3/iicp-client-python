"""IICP client-side policy guardrails.

The client is not a legal compliance engine, but it can fail closed for
intent URNs that are structurally aligned with EU AI Act prohibited practices.
This guard runs before discovery so refused tasks are not routed to unknown
remote nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

from iicp_client.errors import IicpError

POLICY_REFUSAL_CODE = "IICP-POLICY-001"


@dataclass(frozen=True)
class ProhibitedIntentRule:
    """Keyword rule for a prohibited/restricted intent family."""

    rule_id: str
    label: str
    fragments: tuple[str, ...]


PROHIBITED_INTENT_RULES: tuple[ProhibitedIntentRule, ...] = (
    ProhibitedIntentRule(
        "eu-ai-act-social-scoring",
        "social scoring",
        ("social-scoring", "social_scoring", "social:scoring"),
    ),
    ProhibitedIntentRule(
        "eu-ai-act-criminal-risk",
        "individual criminal risk prediction",
        ("criminal-risk", "criminal_risk", "criminal:risk", "predict-crime"),
    ),
    ProhibitedIntentRule(
        "eu-ai-act-workplace-education-emotion",
        "workplace or education emotion recognition",
        (
            "emotion:workplace",
            "emotion:education",
            "workplace-monitoring",
            "education-monitoring",
            "worker-monitoring",
        ),
    ),
    ProhibitedIntentRule(
        "eu-ai-act-protected-trait-biometric",
        "biometric protected-trait classification",
        ("protected-trait", "protected_trait", "biometric:protected"),
    ),
    ProhibitedIntentRule(
        "eu-ai-act-untargeted-face-scraping",
        "untargeted facial image scraping for recognition databases",
        ("untargeted-scraping", "untargeted_scraping", "face-scraping", "facial-scraping"),
    ),
    ProhibitedIntentRule(
        "eu-ai-act-realtime-remote-biometric-id",
        "real-time remote biometric identification",
        ("remote-biometric:realtime", "realtime-remote-biometric", "real-time-remote-biometric"),
    ),
    ProhibitedIntentRule(
        "eu-ai-act-nonconsensual-sexual-deepfake",
        "non-consensual sexual deepfake or CSAM generation",
        ("nonconsensual-sexual", "non-consensual-sexual", "child-sexual-abuse", "csam"),
    ),
)


def prohibited_intent_reason(intent: str) -> str | None:
    """Return a human-readable refusal reason, or ``None`` when allowed."""

    normalized = intent.strip().lower()
    for rule in PROHIBITED_INTENT_RULES:
        if any(fragment in normalized for fragment in rule.fragments):
            return f"{rule.label} ({rule.rule_id})"
    return None


def ensure_intent_allowed(intent: str) -> None:
    """Raise ``IicpError`` if the intent is refused before discovery/routing."""

    reason = prohibited_intent_reason(intent)
    if reason is None:
        return

    raise IicpError(
        code=POLICY_REFUSAL_CODE,
        message=(
            "Intent refused by IICP client policy before discovery/routing: "
            f"{reason}. Use a lawful, documented, human-reviewed compliance path "
            "outside the public mesh for restricted/high-risk workflows."
        ),
        component="sdk",
        retryable=False,
    )
