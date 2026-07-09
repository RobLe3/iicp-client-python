# SPDX-License-Identifier: Apache-2.0
"""Async client for the IICP directory service.

Phase 6 (ADR-013) federated directory support: discover() handles
HTTP 307 Temporary Redirect responses from the Genesis Seed under load,
following the Location header to a replica directory. Conformance with
DIR-FED-05 (transparently follow 307) and DIR-FED-06 (≤3 consecutive redirects).
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from iicp_client.proxy.otel_tracer import proxy_discover_span

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0
_MAX_REDIRECT_CHAIN = 3   # DIR-FED-06: never follow >3 consecutive redirects
_DEFAULT_RETRY_AFTER = 5  # seconds, per spec §6.2 when Retry-After header absent


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_default_certs()
    return ctx


class DirectoryClient:
    def __init__(
        self,
        base_url: str,
        timeout_ms: int = 5000,
        registry: Any | None = None,  # ReplicaRegistry | None — optional cross-check
        did_resolver: Any | None = None,  # DidResolver | None — optional sig verify
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_ms / 1000.0
        # Phase 6: cache of redirect targets — host → (target_url, expiry_monotonic_seconds)
        # Per DIR-FED-05 §6.2: cache the redirected host for Retry-After duration
        self._redirect_cache: dict[str, tuple[str, float]] = {}
        # P6-4.2a: optional ReplicaRegistry for cross-checking redirect-target trust
        # against the seed's published list. Without it, we trust the seed's
        # X-IICP-Replica-Trust header verbatim; with it, we degrade to the lower of
        # (seed-claimed, registry-published) and reject targets absent from the
        # registry entirely.
        self._registry = registry
        # P6-4.2b-ii: optional DidResolver for verifying X-IICP-Replica-Sig on
        # responses from replica directories. Genesis Seed responses are exempt
        # (TLS+DNS trust). When set, replica responses without a valid sig are
        # REJECTED — proxy refuses to use unverifiable node lists.
        self._did_resolver = did_resolver
        self._route_discovery_mode = os.getenv("IICP_ROUTE_DISCOVERY_MODE", "auto").strip().lower()
        if self._route_discovery_mode not in {"auto", "ticketed", "legacy"}:
            self._route_discovery_mode = "auto"

    async def _ticketed_routes(self, params: dict[str, Any], limit: int) -> list[dict[str, Any]] | None:
        """Return ticketed routes, or None only when an older directory lacks the endpoint."""

        excluded: list[str] = []
        routes: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self._timeout, verify=_tls_context()) as client:
            for _ in range(max(1, min(limit, 10))):
                resp = await client.post(
                    f"{self._base_url}/v1/dispatch/ticket",
                    json={**params, "exclude_node_id_prefixes": excluded.copy()},
                )
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                error_code = body.get("error", {}).get("code") if isinstance(body, dict) else None
                if resp.status_code == 201:
                    route = body.get("route")
                    if not isinstance(route, dict) or not isinstance(body.get("node_id"), str):
                        raise httpx.HTTPStatusError("Malformed ticketed route", request=resp.request, response=resp)
                    route = {
                        **route,
                        "node_id": body["node_id"],
                        "dispatch_ticket_id_prefix": body.get("ticket_id_prefix"),
                    }
                    routes.append(route)
                    excluded.append(body["node_id"][:8])
                    continue
                if resp.status_code == 404 and error_code == "no_route_available":
                    break
                if resp.status_code in {404, 405, 501} or (
                    resp.status_code == 503 and error_code == "not_configured"
                ):
                    return None
                resp.raise_for_status()
        return routes

    def _redirect_target_or_none(self, base_url: str) -> str | None:
        """Return cached redirect target if still valid; else None and evict stale."""
        entry = self._redirect_cache.get(base_url)
        if entry is None:
            return None
        target_url, expiry = entry
        if time.monotonic() > expiry:
            del self._redirect_cache[base_url]
            return None
        return target_url

    def _remember_redirect(self, base_url: str, target_url: str, retry_after_seconds: int) -> None:
        self._redirect_cache[base_url] = (
            target_url,
            time.monotonic() + max(1, retry_after_seconds),
        )

    def _process_redirect(self, resp: httpx.Response, current_base: str, redirect_count: int, span: Any) -> str:
        """Extract Location + cache it on first hop. Returns new base URL to retry.

        Security: Location header MUST resolve to an https:// URL (or be relative).
        Rejects http://, file://, ftp://, etc. — prevents SSRF via malicious replica
        redirect (bug-311 finding; consistent with iter-281 LoadRedirect https:// guard).
        """
        location = resp.headers.get("Location")
        if not location:
            span.set_attribute("iicp.discover.error", "redirect_no_location")
            resp.raise_for_status()
            return current_base
        retry_after = int(resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER))
        trust = resp.headers.get("X-IICP-Replica-Trust", "low")
        reason = resp.headers.get("X-IICP-Redirect-Reason", "load")
        logger.info(
            "Discover redirected (DIR-FED-05) — to=%s trust=%s reason=%s retry_after=%ds (chain=%d)",
            location, trust, reason, retry_after, redirect_count,
        )
        span.set_attribute("iicp.discover.redirect_to", location)
        span.set_attribute("iicp.discover.redirect_trust", trust)
        parsed = urlparse(location)
        if parsed.scheme and parsed.netloc:
            # Reject non-https schemes — prevents SSRF + guarantees TLS for the redirect.
            if parsed.scheme != "https":
                logger.warning(
                    "Discover redirect rejected — non-https scheme '%s' in Location: %s",
                    parsed.scheme, location,
                )
                span.set_attribute("iicp.discover.error", "redirect_non_https_scheme")
                # ignore the redirect; retry on current base will hit 307 again → loop guard catches it
                return current_base
            new_base = f"https://{parsed.netloc}"
            # P6-4.2a: cross-check the redirect target against the trusted-replicas
            # registry. The seed claims X-IICP-Replica-Trust=<tier> in the header,
            # but a misconfigured or malicious seed could over-claim. The registry
            # is the operator's canonical statement of which endpoints are vouched
            # for and at what tier.
            if self._registry is not None:
                effective_trust = self._reconcile_trust(new_base, trust, span)
                if effective_trust is None:
                    # Registry says: this replica is not on the trusted list.
                    # Refuse the redirect and retry on the seed (loop guard catches
                    # repeated 307s).
                    return current_base
                trust = effective_trust
                span.set_attribute("iicp.discover.redirect_trust_effective", trust)
            if redirect_count == 1:
                self._remember_redirect(self._base_url, new_base, retry_after)
            return new_base
        return current_base  # relative redirect

    _TIER_RANK = {"low": 0, "medium": 1, "high": 2}

    def _reconcile_trust(self, redirect_target: str, seed_claimed: str, span: Any) -> str | None:
        """Cross-check seed-claimed trust against the registry. Returns the
        effective trust tier, or None if the target isn't in the registry
        (caller must refuse the redirect).

        Uses sync best-effort trust_tier_of() — does NOT auto-refresh the
        registry mid-redirect. Callers that want fresh registry data must
        await registry.refresh() before discover().
        """
        published = self._registry.trust_tier_of(redirect_target)
        # trust_tier_of() returns 'low' for unknown; distinguish "unknown" from
        # "explicitly low" via lookup_sync presence check.
        is_known = redirect_target.rstrip("/") in self._registry._entries_by_endpoint  # noqa: SLF001 — sibling API
        if not is_known:
            logger.warning(
                "Discover redirect target %s NOT in trusted-replicas registry — "
                "refusing redirect (seed-claimed trust=%s)",
                redirect_target, seed_claimed,
            )
            span.set_attribute("iicp.discover.redirect_rejected", "not_in_registry")
            return None
        # Both known. Use the lower of (seed-claimed, registry-published).
        claimed_rank = self._TIER_RANK.get(seed_claimed, 0)
        published_rank = self._TIER_RANK.get(published, 0)
        if claimed_rank > published_rank:
            logger.warning(
                "Seed over-claimed trust for %s: claimed=%s registry=%s — using registry",
                redirect_target, seed_claimed, published,
            )
            span.set_attribute("iicp.discover.trust_downgraded", "seed_overclaim")
            return published
        return seed_claimed

    async def _verify_replica_response(
        self,
        resp: httpx.Response,
        replica_base: str,
        params: dict[str, Any],
        span: Any,
    ) -> bool:
        """Verify X-IICP-Replica-Sig on a response from a replica (P6-4.2b-ii / DIR-FED-20).

        Returns True iff the response is genuinely signed by the registered
        replica's published Ed25519 key. False (with structured log under
        IICP-SEC-REPLICA-01) on any failure — missing header, malformed
        signature, key not resolvable, signature mismatch.
        """
        from iicp_client.proxy.clients.replica_sig_verifier import verify_replica_sig

        sig = resp.headers.get("X-IICP-Replica-Sig")
        snapshot_seq = resp.headers.get("X-IICP-Snapshot-Seq")
        if not sig or not snapshot_seq:
            logger.warning(
                "IICP-SEC-REPLICA-01: replica %s response missing X-IICP-Replica-Sig "
                "or X-IICP-Snapshot-Seq — rejecting",
                replica_base,
            )
            span.set_attribute("iicp.discover.sig_reject", "missing_headers")
            return False

        pub_key = await self._did_resolver.public_key(replica_base)
        if pub_key is None:
            logger.warning(
                "IICP-SEC-REPLICA-01: cannot resolve DID key for replica %s — rejecting",
                replica_base,
            )
            span.set_attribute("iicp.discover.sig_reject", "no_did_key")
            return False

        # Build query string in the same order params were sent — the
        # canonicalizer on both sides sorts independently so order here
        # doesn't matter for the bytes-fed-to-hash, but matters for the
        # canonicalize function input.
        from urllib.parse import urlencode
        query_str = urlencode(params)
        request_path = str(resp.request.url.path)
        path = "/api" + request_path.split("/api", 1)[-1] if "/api" in request_path else "/v1/discover"
        # resp.request.url.path on a replica URL is already /v1/discover (no /api
        # prefix on external) — but the server signed with the prefix it served.
        # The replica directory's middleware uses request->getPathInfo() which
        # includes /api on Laravel. Use the actual request URL path verbatim.
        path = str(resp.request.url.path)

        if verify_replica_sig(
            "GET", path, query_str, snapshot_seq, resp.content, sig, pub_key
        ):
            span.set_attribute("iicp.discover.sig_verified", True)
            return True
        logger.warning(
            "IICP-SEC-REPLICA-01: signature verify FAILED for replica %s — rejecting",
            replica_base,
        )
        span.set_attribute("iicp.discover.sig_reject", "verify_failed")
        return False

    async def _fetch_discover_once(self, base: str, params: dict[str, Any]) -> httpx.Response:
        """One non-redirect-following HTTP GET to base/v1/discover."""
        async with httpx.AsyncClient(
            timeout=self._timeout,
            verify=_tls_context(),
            follow_redirects=False,
        ) as client:
            return await client.get(f"{base.rstrip('/')}/v1/discover", params=params)

    async def discover(
        self,
        intent: str | None = None,
        region: str | None = None,
        limit: int = 5,
        models: list[str] | None = None,
        cip_capable: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if intent:
            params["intent"] = intent
        if region:
            params["region"] = region
        if models:
            params["model"] = models[0] if len(models) == 1 else ",".join(models)
        # S.12 §5.2: filter for CIP-capable workers (allow_remote_inference=true) when dispatching.
        # Laravel boolean validation accepts 1/0, not "true"/"false" strings.
        if cip_capable is not None:
            params["cip_capable"] = 1 if cip_capable else 0

        if intent and self._route_discovery_mode != "legacy":
            ticketed = await self._ticketed_routes(params, limit)
            if ticketed is not None:
                return ticketed
            if self._route_discovery_mode == "ticketed":
                raise RuntimeError("Directory does not support ticketed dispatch")

        with proxy_discover_span(intent or "") as span:
            current_base = self._redirect_target_or_none(self._base_url) or self._base_url
            for redirect_count in range(_MAX_REDIRECT_CHAIN + 1):
                resp = await self._fetch_discover_once(current_base, params)
                if resp.status_code != 307:
                    resp.raise_for_status()
                    # P6-4.2b-ii: verify replica signature if this response came
                    # from a non-seed source AND we have a verifier wired in.
                    if self._did_resolver is not None and current_base != self._base_url:
                        if not await self._verify_replica_response(resp, current_base, params, span):
                            # IICP-SEC-REPLICA-01 — replica response failed sig verify;
                            # reject and let the redirect loop retry the seed
                            current_base = self._base_url
                            continue
                    nodes = resp.json().get("nodes", [])
                    span.set_attribute("iicp.discover.node_count", len(nodes))
                    if current_base != self._base_url:
                        span.set_attribute("iicp.discover.replica_used", current_base)
                    return nodes
                current_base = self._process_redirect(resp, current_base, redirect_count + 1, span)
            # Exceeded max chain — DIR-FED-06
            span.set_attribute("iicp.discover.error", "redirect_loop")
            raise httpx.HTTPStatusError(
                message="Exceeded max consecutive redirects (DIR-FED-06)",
                request=resp.request, response=resp,
            )

    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Register this node with the directory; returns the ACK body."""
        async with httpx.AsyncClient(timeout=self._timeout, verify=_tls_context()) as client:
            resp = await client.post(
                f"{self._base_url}/v1/register",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def me(self, node_token: str) -> dict[str, Any]:
        """Fetch the directory's current view of this node (DIR-ADDR-04)."""
        async with httpx.AsyncClient(timeout=self._timeout, verify=_tls_context()) as client:
            resp = await client.get(
                f"{self._base_url}/v1/me",
                headers={"Authorization": f"Bearer {node_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def credit_balance(self, node_token: str) -> float | None:
        """Fetch this consumer's S-Credit balance (directory GET /v1/credits/balance).

        Best-effort for the §10.1 affordability gate (decision B-A): a balance the
        proxy can compare against the routing cost before a remote CIP dispatch.
        Returns ``None`` on any failure (no token, non-200, network/timeout) — the
        affordability gate treats ``None`` as "balance unknown" and skips, so a
        directory hiccup degrades gracefully to the prior behaviour rather than
        blocking inference.
        """
        if not node_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=self._timeout, verify=_tls_context()) as client:
                resp = await client.get(
                    f"{self._base_url}/v1/credits/balance",
                    headers={"Authorization": f"Bearer {node_token}"},
                )
                resp.raise_for_status()
                balance = resp.json().get("balance")
                return float(balance) if balance is not None else None
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("Credit-balance fetch failed (gate skipped): %s", exc)
            return None

    async def bootstrap(self, limit: int = 5) -> list[dict[str, Any]]:
        """Fetch seed peer list from directory (Phase 2)."""
        async with httpx.AsyncClient(timeout=self._timeout, verify=_tls_context()) as client:
            resp = await client.get(
                f"{self._base_url}/v1/bootstrap",
                params={"limit": limit},
            )
            resp.raise_for_status()
            return resp.json().get("peers", [])


def check_observed_ip_vs_endpoint(
    observed_ip: str,
    endpoint: str,
) -> bool:
    """Return True if observed IP matches the endpoint host; warn if not."""
    if not observed_ip or not endpoint:
        return True
    try:
        host = urlparse(endpoint).hostname or ""
        resolved = socket.gethostbyname(host)
        if resolved != observed_ip:
            logger.warning(
                "Observed external IP %s does not match endpoint host %s (resolved: %s) — "
                "this node may be behind NAT or a misconfigured reverse proxy.",
                observed_ip,
                host,
                resolved,
            )
            return False
        return True
    except Exception:
        return True
