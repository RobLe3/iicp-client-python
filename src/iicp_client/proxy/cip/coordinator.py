# SPDX-License-Identifier: Apache-2.0
"""Phase 5A CIP Coordinator — consumer dispatch per S.12 §2.2.

After iter-1402 refactor (#333 Priority #2): this module is now a thin coordinator
that focuses on the ledger-forwarding async path (submit_award). The dispatch gates,
request validation, and receipt/replay primitives moved to focused sibling modules:

  - `proxy.cip.gates`    — CIPDispatchConfig, CIPStrategy, DispatchResult, DispatchDecision,
                            decide_dispatch, validate_cip_request_fields,
                            compute_worker_timeout_s, cip_exhaustion_result, build_cip_envelope
  - `proxy.cip.receipts` — CIPReceipt, CIPWorkerReceipt, ReplayCache,
                            sign_receipt, verify_receipt_signature, make_session_key

This file re-exports those names so existing callers (`proxy.config`, `proxy.main`,
`proxy.routing.fallback`, `proxy.cip.dispatch`, tests) need no changes — back-compat
preserved at the module-public-API level.

TC-9d coordinator wiring: after a worker completes a remote task, the coordinator
MUST verify session binding and replay status before forwarding the CIPWorkerReceipt
to the directory's credit ledger (§7, ADR-012). submit_award() implements this gate.
"""
from __future__ import annotations

import httpx

from iicp_client.proxy.cip.gates import (  # noqa: F401 — re-exported for back-compat
    CIPDispatchConfig,
    CIPPrivacyConfig,
    CIPStrategy,
    DispatchDecision,
    DispatchResult,
    build_cip_envelope,
    cip_exhaustion_result,
    compute_worker_timeout_s,
    decide_dispatch,
    validate_cip_request_fields,
)
from iicp_client.proxy.cip.receipts import (  # noqa: F401 — re-exported for back-compat
    CIPReceipt,
    CIPWorkerReceipt,
    ReplayCache,
    make_session_key,
    sign_receipt,
    verify_receipt_signature,
)
from iicp_client.proxy.otel_tracer import cip_award_span


async def submit_award(
    *,
    receipt: CIPWorkerReceipt,
    expected_session_key: str | None,
    replay_cache: ReplayCache,
    directory_url: str,
    node_token: str,
    tokens_per_credit: float = 1000.0,
) -> bool:
    """TC-9d: verify and submit a credit award to the directory ledger.

    Coordinator-side checks before forwarding (§7, ADR-012):
      1. TC-9b: nonce replay check — reject if this nonce was seen before
      2. Session binding — reject if cip_session_key does not match expected
      3. POST /api/v1/credits/award — directory re-validates HMAC with node_hmac_key

    Returns True when the directory accepts the award (HTTP 200), False otherwise.
    Errors are non-raising; callers MUST check the return value.
    """
    # TC-9b: replay guard — coordinator-local check before network call
    if replay_cache.is_replay(receipt.nonce):
        return False

    # Session binding — receipt must carry the session key from this dispatch
    if expected_session_key is not None and receipt.cip_session_key != expected_session_key:
        return False

    amount = receipt.tokens_used / tokens_per_credit

    payload: dict = {
        "node_id": receipt.worker_node_id,
        "task_id": receipt.task_id,
        "tokens_used": receipt.tokens_used,
        "nonce": receipt.nonce,
        "expires_at": receipt.expires_at,
        "signature": receipt.signature,
        "amount": amount,
    }
    if receipt.response_hash is not None:
        payload["response_hash"] = receipt.response_hash
    if receipt.cip_session_key is not None:
        payload["cip_session_key"] = receipt.cip_session_key
    if receipt.cip_parent_task_id is not None:
        payload["cip_parent_task_id"] = receipt.cip_parent_task_id

    try:
        with cip_award_span(  # TRACE-08
            task_id=receipt.task_id,
            tokens_used=receipt.tokens_used,
            amount=amount,
        ):
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{directory_url}/api/v1/credits/award",
                    json=payload,
                    headers={"Authorization": f"Bearer {node_token}"},
                )
                return r.status_code == 200
    except Exception:
        return False
