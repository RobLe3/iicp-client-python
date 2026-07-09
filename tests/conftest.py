"""Cross-suite compatibility defaults for staged ticketed dispatch."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_directory_ticket_default(monkeypatch, request):
    """Existing tests model pre-ticket directories unless they explicitly test tickets."""

    if request.node.get_closest_marker("ticketed_dispatch"):
        monkeypatch.delenv("IICP_ROUTE_DISCOVERY_MODE", raising=False)
        return
    monkeypatch.setenv("IICP_ROUTE_DISCOVERY_MODE", "legacy")
