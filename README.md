# iicp-client · Python SDK

[![CI](https://github.com/RobLe3/iicp-client-python/actions/workflows/ci.yml/badge.svg)](https://github.com/RobLe3/iicp-client-python/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Protocol](https://img.shields.io/badge/IICP-v1.7-indigo.svg)](https://iicp.network/spec)
[![PyPI](https://img.shields.io/badge/PyPI-iicp--client-blue?logo=pypi&logoColor=white)](https://pypi.org/project/iicp-client/)

Official Python client library for the [IICP protocol](https://iicp.network) — route AI agent tasks by intent across a self-organising mesh of provider nodes. No central broker. No hardcoded endpoints.

```
urn:iicp:intent:llm:chat:v1  →  discover  →  select  →  submit
```

---

## Install

```bash
pip install iicp-client
```

Requires **Python ≥ 3.11** and [`httpx`](https://www.python-httpx.org/).

---

## Quickstart

```python
import asyncio
from iicp_client import IicpClient, ChatMessage

async def main():
    client = IicpClient()

    # chat_async discovers, selects best node, and submits in one call
    response = await client.chat_async(
        messages=[ChatMessage(role="user", content="Hello from IICP!")],
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

Synchronous wrapper for scripts and notebooks:

```python
from iicp_client import IicpClient, ChatMessage

client   = IicpClient()
response = client.chat([ChatMessage(role="user", content="Hello from IICP!")])
print(response.choices[0].message.content)
```

---

## Configuration

```python
from iicp_client import ClientConfig

config = ClientConfig(
    directory_url = "https://iicp.network",  # IICP directory
    timeout_ms    = 30_000,                  # max 120 000 (SDK-04)
    region        = "eu-central",            # prefer nodes in region
)
```

| Field | Default | Description |
|-------|---------|-------------|
| `directory_url` | `"https://iicp.network"` | IICP directory endpoint |
| `timeout_ms` | `30000` | Request timeout — max 120 000 ms |
| `region` | `None` | Preferred node region |
| `max_retries` | `3` | Retry count for transient errors |

---

## Discover options

```python
from iicp_client import DiscoverOptions

node_list = await client.discover_async(
    "urn:iicp:intent:llm:chat:v1",
    DiscoverOptions(
        region         = "eu-central",
        model          = "phi3:mini",
        min_reputation = 0.7,
        limit          = 5,
    )
)
nodes = node_list.nodes  # list of Node objects
```

---

## Error handling

```python
from iicp_client import IicpClient, IicpError, ChatMessage

client = IicpClient()
try:
    response = client.chat([ChatMessage(role="user", content="hi")])
except IicpError as e:
    print(f"[{e.code}] {e.message}  (HTTP {e.http_status})")
```

Error codes match the [IICP error reference](https://iicp.network/docs/error-reference) — e.g. `task_timeout`, `capacity_exceeded`, `no_nodes_available`.

---

## Serving as a provider node

```python
import asyncio
from iicp_client import IicpNode, NodeConfig

async def my_handler(task):
    return {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}

async def main():
    node = IicpNode(NodeConfig(
        node_id="my-node-001",
        endpoint="http://my.public.host:8020",
        intent="urn:iicp:intent:llm:chat:v1",
        model="llama3:8b",
    ))
    token = await node.register()
    stop = node.serve(my_handler, port=8020, node_token=token)
    try:
        await asyncio.Event().wait()  # run until stopped
    finally:
        stop()

asyncio.run(main())
```

---

## NAT traversal — v0.7.0

IICP nodes pick the best available NAT path automatically (ADR-041):

| Tier | Method | Requirement |
|------|--------|-------------|
| 0 | Direct — publicly routable | Open port 8020 |
| 1 | UPnP/IGD port mapping | Home router with UPnP |
| 2 | IPv6 firewall pinhole | IPv6 + UPnP/IGD2 |
| 3 | **Relay-as-last-resort** | A relay operator in the mesh |

**Relay-as-last-resort** lets a node behind CGNAT stay reachable by binding an outbound
channel to a public relay node that forwards inbound tasks down it.

### Running a relay-capable node (relay operator)

```python
node = IicpNode(NodeConfig(
    node_id="relay-eu-01",
    endpoint="http://relay.example.com:8020",
    intent="urn:iicp:intent:llm:chat:v1",
    relay_capable=True,      # accept RELAY_BIND on port 9485
    relay_accept_port=9485,  # TCP port for CGNAT workers
    enable_mesh=True,        # gossip relay_capable=True to peers
))
```

### Node behind CGNAT (connects outbound to relay)

```python
node = IicpNode(NodeConfig(
    node_id="cgnat-worker-001",
    endpoint="http://placeholder",        # overwritten on bind
    intent="urn:iicp:intent:llm:chat:v1",
    relay_worker_endpoint="relay.example.com:9485",  # outbound target
    # or: env IICP_RELAY_WORKER_ENDPOINT=relay.example.com:9485
))
```

When the worker binds, it deregisters its placeholder endpoint and re-registers with the
relay's public address (`transport_method=turn_relay`), making it discoverable.

### Relay election (R3)

When multiple relay-capable peers are known from gossip, the SDK elects the best one
deterministically: lowest `relay_load`, with `SHA-256(workerId:relayId)` as a tiebreak
so multiple workers spread uniformly across the relay pool.

```python
from iicp_client import PeerManager

pm = PeerManager("https://iicp.network/api", enable_mesh=True)
# elect_relay() is called automatically by node.serve() on tier-3 detection.
elected = pm.elect_relay("my-worker-id")
if elected:
    print(f"Elected relay: {elected['_relay_host']}:{elected['_relay_port']}")
```

---

## SDK conformance

| Rule | Description | Status |
|------|-------------|--------|
| SDK-01 | discover → select → submit pipeline with node retry | ✓ |
| SDK-02 | `task_id` auto-generated (UUID v4) | ✓ |
| SDK-03 | Intent URN pattern validation | ✓ |
| SDK-04 | `timeout_ms` capped at 120 000 ms | ✓ |
| SDK-05 | Retry on 429 / 503 with exponential back-off | ✓ |
| SDK-06 | W3C `traceparent` propagation | ✓ |

Conformance tier: `iicp:sdk:v1` (spec S.14) · [Request a badge](https://iicp.network/conformance)

---

## Development

```bash
pip install -e ".[dev]"   # install with dev deps
pytest tests/ -v          # run 213 unit tests
ruff check src tests       # lint
```

---

## Links

- [Protocol spec](https://iicp.network/spec) — full IICP specification
- [Node setup guide](https://iicp.network/docs/node-setup) — run your own node
- [Error reference](https://iicp.network/docs/error-reference) — all error codes
- [iicp-client-typescript](https://github.com/RobLe3/iicp-client-typescript) — TypeScript SDK
- [iicp-client-rust](https://github.com/RobLe3/iicp-client-rust) — Rust SDK

---

Apache 2.0 · [iicp.network](https://iicp.network)
