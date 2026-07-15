"""Canonical request-to-route/execution projection shared by all submit paths."""

from __future__ import annotations

from typing import Any

from iicp_client.types import ClientConfig, DiscoverOptions, RouteConstraints, TaskRequest


def project_route_options(request: TaskRequest, config: ClientConfig) -> DiscoverOptions:
    """Project prompt-free routing criteria without leaking task payload fields.

    Explicit ``route_constraints`` win over compatibility fields carried by the
    historical ``TaskConstraints`` shape, then client defaults apply.
    """

    route = request.route_constraints or RouteConstraints()
    return DiscoverOptions(
        region=route.region or request.constraints.region or config.region,
        qos=route.qos or request.constraints.qos,
        model=route.model or request.constraints.model,
        min_reputation=route.min_reputation
        if route.min_reputation is not None
        else request.constraints.min_reputation,
        limit=route.limit,
        browser_usable_only=route.browser_usable_only,
        profile_request=route.profile_request or config.profile_request,
    )


def project_execution_constraints(request: TaskRequest) -> dict[str, Any]:
    """Return only provider-facing execution controls."""

    constraints: dict[str, Any] = {
        "timeout_ms": request.constraints.timeout_ms,
        "qos": request.constraints.qos,
    }
    if request.constraints.model:
        constraints["model"] = request.constraints.model
    return constraints
