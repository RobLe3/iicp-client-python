# iicp-client — Python SDK

Official Python client library for the [IICP protocol](https://iicp.network) (Intent-based Inter-agent Communication Protocol).

Implements **ADR-016 §1** — SDK conformance rules SDK-01 through SDK-06.

---

## Install

```bash
pip install iicp-client
```

Requires Python ≥ 3.11 and [httpx](https://www.python-httpx.org/).

---

## Quickstart

```python
import asyncio
from iicp_client import IicpClient, ClientConfig, TaskRequest

async def main():
    client = IicpClient(ClientConfig(
        directory_url="https://iicp.network/api",
        timeout_ms=30_000,
    ))

    # Discover nodes capable of LLM chat
    nodes = await client.discover_async("urn:iicp:intent:llm:chat:v1")
    if not nodes.nodes:
        print("No nodes available")
        return

    # Submit a chat task to the best node
    node = nodes.nodes[0]
    response = await client.chat_async(
        node=node,
        messages=[{"role": "user", "content": "Hello from IICP!"}],
    )
    print(response.choices[0].message["content"])

asyncio.run(main())
```

### Synchronous API

```python
from iicp_client import IicpClient

client = IicpClient()
nodes  = client.discover("urn:iicp:intent:llm:chat:v1")
resp   = client.chat(node=nodes.nodes[0], messages=[{"role": "user", "content": "Hi"}])
print(resp.choices[0].message["content"])
```

---

## Configuration

```python
from iicp_client import ClientConfig

config = ClientConfig(
    directory_url="https://iicp.network/api",  # directory endpoint
    timeout_ms=30_000,                          # max 120_000 (SDK-04)
    region="eu-central",                        # prefer nodes in this region
    node_token="your-token",                    # optional auth token
)
```

| Field | Default | Description |
|-------|---------|-------------|
| `directory_url` | `https://iicp.network/api` | IICP directory endpoint |
| `timeout_ms` | `30000` | Request timeout (max 120 000 ms) |
| `region` | `None` | Preferred node region |
| `node_token` | `None` | Bearer token for authenticated nodes |

---

## Discover options

```python
from iicp_client import DiscoverOptions

nodes = await client.discover_async(
    "urn:iicp:intent:llm:chat:v1",
    DiscoverOptions(
        region="eu-central",
        model="phi3:mini",        # request a specific model
        min_reputation=0.7,       # only well-regarded nodes
        limit=5,
    )
)
```

---

## Error handling

```python
from iicp_client import IicpClient, IicpError

try:
    resp = await client.submit_async(request)
except IicpError as e:
    print(f"Error [{e.code}]: {e.message}  (HTTP {e.status_code})")
```

Errors are typed with a `code` field matching the IICP error reference (e.g. `task_timeout`, `no_nodes_available`). See the [error reference](https://iicp.network/docs/error-reference).

---

## Conformance

This SDK targets the **IICP SDK conformance tier** (`iicp:sdk:v1`, spec S.14).
See [Conformance badges](https://iicp.network/conformance) for how to obtain a signed badge for your implementation.

| ADR | Rule | Status |
|-----|------|--------|
| ADR-016 | SDK-01 discover → select → submit pipeline | ✓ |
| ADR-016 | SDK-02 task_id auto-generated (UUID v4) | ✓ |
| ADR-016 | SDK-03 intent validation (URN pattern) | ✓ |
| ADR-016 | SDK-04 timeout_ms ≤ 120 000 enforced | ✓ |
| ADR-016 | SDK-05 retry on 429/503 with back-off | ✓ |
| ADR-016 | SDK-06 W3C traceparent propagation | ✓ |

---

## Development

```bash
# Install in editable mode with dev deps
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src tests
```

---

## Links

- Protocol spec: [iicp.network/spec](https://iicp.network/spec)
- Node setup: [iicp.network/docs/node-setup](https://iicp.network/docs/node-setup)
- Error reference: [iicp.network/docs/error-reference](https://iicp.network/docs/error-reference)
- Conformance: [iicp.network/conformance](https://iicp.network/conformance)
- GitHub issues: [github.com/RobLe3/iicp-client-python](https://github.com/RobLe3/iicp-client-python/issues)

---

**License**: Apache 2.0 · © IICP Working Group
