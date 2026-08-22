"""Fail-closed restricted trust-domain directory operation boundary."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from iicp_client.errors import IicpError

PROFILE_ID = "urn:iicp:profile:restricted-trust-domain:v1"
DECISION_SCHEMA = "iicp.restricted-trust-domain.directory-decision.v0"

def _refused(message: str) -> IicpError:
    return IicpError("restricted_directory_decision_refused", message, "directory", retryable=False)

@dataclass(frozen=True)
class SecretRef:
    """Reference to membership material; the value is never serialized."""
    kind: Literal["environment", "file"]
    value: str

    def resolve(self) -> str:
        secret = ""
        if self.kind == "environment":
            secret = os.environ.get(self.value, "")
        elif self.kind == "file":
            path = Path(self.value)
            if not path.is_symlink() and path.is_file() and not path.stat().st_mode & 0o077:
                secret = path.read_text(encoding="utf-8").strip()
        if not secret:
            raise _refused("restricted directory membership credential is unavailable")
        return secret

@dataclass(frozen=True)
class RestrictedDirectoryContext:
    domain_id: str
    authority_id: str
    subject_id: str
    subject_kind: Literal["node", "client", "directory"]
    minimum_membership_generation: int
    membership_credential: SecretRef

    def validate(self) -> None:
        if (not self.domain_id.strip() or not self.authority_id.strip() or not self.subject_id.strip()
                or self.subject_kind not in {"node", "client", "directory"}
                or self.minimum_membership_generation < 1):
            raise _refused("restricted directory context is incomplete")
        self.membership_credential.resolve()

    def headers(self) -> dict[str, str]:
        return {"X-IICP-Membership": self.membership_credential.resolve(), "X-IICP-Subject-Id": self.subject_id}

@dataclass(frozen=True)
class RestrictedEligibility:
    domain_id: str
    authority_id: str
    membership_generation: int
    membership_expires_at: int

def validate_decision(body: dict[str, Any], context: RestrictedDirectoryContext, operation: str) -> RestrictedEligibility:
    raw = body.get("restricted_domain_decision")
    expected = {"schema", "profile", "decision", "operation", "domain_id", "authority_id", "subject_kind", "membership_generation", "membership_expires_at"}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise _refused("restricted directory decision is missing or malformed")
    generation, expiry = raw["membership_generation"], raw["membership_expires_at"]
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or not isinstance(expiry, int) or isinstance(expiry, bool)):
        raise _refused("restricted directory decision is malformed")
    if (raw["schema"] != DECISION_SCHEMA or raw["profile"] != PROFILE_ID or raw["decision"] != "eligible"
            or raw["operation"] != operation or raw["domain_id"] != context.domain_id
            or raw["authority_id"] != context.authority_id or raw["subject_kind"] != context.subject_kind
            or generation < context.minimum_membership_generation or expiry <= int(time.time())):
        raise _refused("restricted directory decision does not match the request context")
    return RestrictedEligibility(raw["domain_id"], raw["authority_id"], generation, expiry)
