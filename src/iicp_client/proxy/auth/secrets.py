# SPDX-License-Identifier: Apache-2.0
"""Load the node token from the environment (or OS keychain as fallback)."""
from __future__ import annotations

import os


class MissingNodeTokenError(RuntimeError):
    pass


def load_node_token(env_var: str = "IICP_NODE_TOKEN") -> str:
    token = os.environ.get(env_var, "").strip()
    if not token:
        raise MissingNodeTokenError(
            f"Node token not found. Set the {env_var} environment variable."
        )
    return token
