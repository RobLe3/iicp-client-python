"""Local signed node-policy manifest producer (#588)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from iicp_client.identity import OperatorIdentity


def canonical_manifest(manifest: dict[str, Any]) -> bytes:
    copy = json.loads(json.dumps(manifest))
    signature = copy.get("signature")
    if isinstance(signature, dict):
        signature.pop("signature", None)
    else:
        copy.pop("signature", None)
    return json.dumps(copy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_and_sign_policy_manifest(
    path: str | Path,
    operator: OperatorIdentity,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read policy manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("policy manifest must be a JSON object")
    raw.pop("signature", None)
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        public = base64.b64decode(operator.operator_id, validate=True)
    except ValueError as exc:
        raise ValueError(f"operator_id is not valid base64: {exc}") from exc
    signing_key = operator.signing_key()
    if len(public) != 32 or public != signing_key.public_key().public_bytes_raw():
        raise ValueError("operator_id does not match the operator signing key")
    raw["signature"] = {
        "algorithm": "Ed25519",
        "key_id": hashlib.sha256(public).hexdigest()[:12],
        "public_key": operator.operator_id,
        "signed_at": instant.isoformat().replace("+00:00", "Z"),
        "expires_at": (instant + timedelta(days=90)).isoformat().replace("+00:00", "Z"),
    }
    raw["signature"]["signature"] = base64.b64encode(
        signing_key.sign(canonical_manifest(raw))
    ).decode()
    return raw
