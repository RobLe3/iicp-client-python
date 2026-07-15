"""DNS-aware provider endpoint validation and connection pinning (#667)."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

import httpcore
import httpx

from iicp_client.errors import IicpError

_BLOCKED_SUFFIXES = (".local", ".internal", ".lan", ".test", ".invalid", ".localhost")
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
        "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
        "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "100::/64",
        "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8",
    )
)


def private_endpoints_allowed() -> bool:
    return os.getenv("IICP_PROXY_ALLOW_LOOPBACK_NODES", "").strip().lower() in {"1", "true", "yes"}


def hostname_allowed(host: str, *, allow_private: bool = False) -> bool:
    if not host:
        return False
    if allow_private:
        return True
    normalized = host.rstrip(".").lower()
    if normalized in {"localhost", "0.0.0.0", "::1", "::"}:
        return False
    if normalized.endswith(_BLOCKED_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(normalized)
        return True
    except ValueError:
        return "." in normalized or ":" in normalized


def address_allowed(
    address: str | ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool = False,
) -> bool:
    if allow_private:
        return True
    parsed = ipaddress.ip_address(address) if isinstance(address, str) else address
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return not any(parsed in network for network in _BLOCKED_NETWORKS if parsed.version == network.version)


@dataclass(frozen=True)
class ResolvedEndpoint:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


async def resolve_endpoint(url: str, *, allow_private: bool | None = None) -> ResolvedEndpoint:
    allow = private_endpoints_allowed() if allow_private is None else allow_private
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IicpError("IICP-ENDPOINT-REFUSED", "provider endpoint must use HTTP or HTTPS", "sdk")
    if parsed.username or parsed.password:
        raise IicpError("IICP-ENDPOINT-REFUSED", "provider endpoint must not contain user info", "sdk")
    host = parsed.hostname.rstrip(".").lower()
    if not hostname_allowed(host, allow_private=allow):
        raise IicpError("IICP-ENDPOINT-REFUSED", "provider hostname is prohibited by network policy", "sdk")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        literal = ipaddress.ip_address(host)
        addresses = (str(literal),)
    except ValueError:
        try:
            records = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        except OSError as exc:
            raise IicpError("IICP-ENDPOINT-REFUSED", "provider hostname resolution failed", "sdk") from exc
        addresses = tuple(sorted({record[4][0] for record in records}))
    if not addresses:
        raise IicpError("IICP-ENDPOINT-REFUSED", "provider hostname returned no addresses", "sdk")
    if any(not address_allowed(address, allow_private=allow) for address in addresses):
        raise IicpError("IICP-ENDPOINT-REFUSED", "provider hostname resolved to a prohibited address", "sdk")
    return ResolvedEndpoint(url=url, host=host, port=port, addresses=addresses)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, host: str, address: str) -> None:
        self._host = host
        self._address = address
        self._delegate = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # type: ignore[no-untyped-def]
        if host.rstrip(".").lower() != self._host:
            raise OSError("unpinned host refused")
        return await self._delegate.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # type: ignore[no-untyped-def]
        raise OSError("unix sockets are not valid provider endpoints")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Bind TCP to one validated address while preserving URL host and TLS SNI."""

    def __init__(self, endpoint: ResolvedEndpoint, *, verify: ssl.SSLContext | bool = True) -> None:
        super().__init__(verify=verify)
        ssl_context = verify if isinstance(verify, ssl.SSLContext) else ssl.create_default_context()
        if verify is False:
            ssl_context = ssl._create_unverified_context()  # noqa: SLF001 - mirrors HTTPX debug mode
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=_PinnedBackend(endpoint.host, endpoint.addresses[0]),
        )
