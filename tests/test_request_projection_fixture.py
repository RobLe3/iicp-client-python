from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from iicp_client.request_projection import project_execution_constraints, project_route_options
from iicp_client.types import (
    ClientConfig,
    ProfileRequest,
    RouteConstraints,
    TaskConstraints,
    TaskRequest,
)


def _profile(value):
    return ProfileRequest(**value) if value else None


def test_shared_sdk_request_projection_fixture() -> None:
    path = Path(__file__).resolve().parents[1] / "parity/sdk-request-projection-v0.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "0a89ae1ee02aca25f7989576b0ab88640bf382bf2d13e37e489798c81d010d8c"
    fixture = json.loads(path.read_text())
    for case in fixture["cases"]:
        config_data = dict(case["config"])
        config_data["profile_request"] = _profile(config_data.get("profile_request"))
        config = ClientConfig(**config_data)

        task = case["task"]
        constraints = TaskConstraints(**task["constraints"])
        route_data = task.get("route_constraints")
        route = None
        if route_data is not None:
            route_data = dict(route_data)
            route_data["profile_request"] = _profile(route_data.get("profile_request"))
            route = RouteConstraints(**route_data)

        request = TaskRequest(
            intent="urn:iicp:intent:llm:chat:v1",
            payload={},
            constraints=constraints,
            route_constraints=route,
        )
        actual_route = asdict(project_route_options(request, config))
        assert actual_route == case["expected"]["route_options"], case["name"]
        assert project_execution_constraints(request) == case["expected"]["execution_constraints"], case["name"]
