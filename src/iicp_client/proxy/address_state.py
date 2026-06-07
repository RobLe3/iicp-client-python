# SPDX-License-Identifier: Apache-2.0
"""Runtime holder for the proxy's observed external IP (DIR-ADDR-02)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AddressState:
    observed_source_ip: str | None = field(default=None)
    endpoint: str | None = field(default=None)
    node_id: str | None = field(default=None)

    def update_from_ack(self, ack: dict) -> None:
        self.observed_source_ip = ack.get("observed_source_ip")
        self.node_id = ack.get("node_id")

    def update_from_me(self, me: dict) -> None:
        self.observed_source_ip = me.get("observed_source_ip")
        self.node_id = me.get("node_id")
        self.endpoint = me.get("endpoint")


# Module-level singleton — set once at startup, read by status commands
_state: AddressState = AddressState()


def get_address_state() -> AddressState:
    return _state
