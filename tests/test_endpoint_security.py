from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from iicp_client._http import post_json
from iicp_client.endpoint_security import address_allowed, hostname_allowed, resolve_endpoint
from iicp_client.errors import IicpError


def test_address_policy_covers_mapped_and_private_classes() -> None:
    assert address_allowed("93.184.216.34")
    assert not address_allowed("127.0.0.1")
    assert not address_allowed("169.254.169.254")
    assert not address_allowed("::ffff:127.0.0.1")
    assert not address_allowed("fd00::1")
    assert address_allowed("10.0.0.5", allow_private=True)


def test_hostname_policy_blocks_local_names() -> None:
    assert hostname_allowed("provider.example.com")
    assert not hostname_allowed("localhost")
    assert not hostname_allowed("provider.internal")
    assert not hostname_allowed("ollama")


@pytest.mark.asyncio
async def test_literal_endpoint_resolves_without_dns() -> None:
    endpoint = await resolve_endpoint("https://93.184.216.34/v1")
    assert endpoint.addresses == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_literal_private_endpoint_is_refused() -> None:
    with pytest.raises(IicpError, match="prohibited address"):
        await resolve_endpoint("http://169.254.169.254/latest")


class _ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/task")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


@pytest.mark.asyncio
async def test_private_provider_requires_opt_in_and_uses_pinned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/redirect"
    try:
        monkeypatch.delenv("IICP_PROXY_ALLOW_LOOPBACK_NODES", raising=False)
        with pytest.raises(IicpError, match="prohibited address"):
            await post_json(url, {}, pin_provider_endpoint=True, tls_verify=False)

        monkeypatch.setenv("IICP_PROXY_ALLOW_LOOPBACK_NODES", "1")
        response, _elapsed = await post_json(url, {}, pin_provider_endpoint=True, tls_verify=False)
        assert response == {"ok": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_shared_fixture_matches_python_policy() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "endpoint-security-v1.json").read_text())
    for vector in fixture["address_vectors"]:
        actual = all(address_allowed(address, allow_private=vector["allow_private"]) for address in vector["addresses"])
        assert actual is vector["allowed"], vector["id"]
    for vector in fixture["hostname_vectors"]:
        assert hostname_allowed(vector["host"]) is vector["allowed"], vector["id"]
    for vector in fixture["resolution_attempt_vectors"]:
        actual = [
            "allow" if all(address_allowed(address, allow_private=vector["allow_private"]) for address in attempt) else "refuse"
            for attempt in vector["attempts"]
        ]
        assert actual == vector["expected"], vector["id"]
    for vector in fixture["redirect_vectors"]:
        safe_target = all(
            address_allowed(address, allow_private=vector["allow_private"])
            for address in vector["target_addresses"]
        )
        actual = (
            "follow_after_revalidation"
            if vector["status"] in {307, 308} and vector["same_origin"] and safe_target
            else "refuse"
        )
        assert actual == vector["expected"], vector["id"]
