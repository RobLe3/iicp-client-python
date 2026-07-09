# SPDX-License-Identifier: Apache-2.0
"""End-to-end function test for `iicp-node proxy` (ADR-050, maintainer req).

Launches the REAL `iicp-node proxy` process against a mock directory + mock node and
drives a real HTTP request through each compat surface (OpenAI / Ollama / Anthropic),
asserting the full path: parse → discover → IICP /v1/task → translate → response,
including the `Server: iicp-proxy` self-identification. Complements the in-process
fixture conformance (tests/proxy/*). #482 / WQ-074.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _MockHandler(BaseHTTPRequestHandler):
    """Mock directory (/api/v1/discover) + mock node (/v1/task) on one port."""

    def log_message(self, *_a):  # silence
        pass

    def _json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/v1/discover"):
            self._json({
                "nodes": [{
                    "node_id": "mock-node-1",
                    "endpoint": self.server.node_endpoint,  # type: ignore[attr-defined]
                    "region": "test",
                    "score": 1.0,
                    "available": True,
                }],
                "count": 1,
                "query_ms": 1,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path == "/api/v1/dispatch/ticket":
            n = int(self.headers.get("content-length", 0))
            self.rfile.read(n)
            self._json({
                "ticket": "e2e-route-ticket-not-forwarded",
                "ticket_id_prefix": "e2e-ticket-1",
                "node_id": "mock-node-1",
                "route": {
                    "endpoint": self.server.node_endpoint,  # type: ignore[attr-defined]
                    "region": "test",
                    "available": True,
                },
            }, status=201)
        elif self.path == "/v1/task":
            n = int(self.headers.get("content-length", 0))
            self.rfile.read(n)
            self._json({
                "task_id": "t-e2e",
                "status": "success",
                "result": {"choices": [{"message": {"role": "assistant", "content": "E2E reply"}}], "usage": {}},
            })
        else:
            self.send_response(404)
            self.end_headers()


def _post(url: str, body: dict, timeout: float = 10.0):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


@pytest.mark.timeout(40)
def test_e2e_all_surfaces_through_real_proxy_process():
    mock_port = _free_port()
    proxy_port = _free_port()

    srv = ThreadingHTTPServer(("127.0.0.1", mock_port), _MockHandler)
    srv.node_endpoint = f"http://127.0.0.1:{mock_port}"  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = {
        "IICP_PROXY_DIRECTORY_URL": f"http://127.0.0.1:{mock_port}/api",
        "IICP_PROXY_ALLOW_LOOPBACK_NODES": "1",  # E2E: let the proxy reach the loopback mock node
        "IICP_NODE_TOKEN": "",
        "PATH": os.environ.get("PATH", ""),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "iicp_client.cli", "proxy", "--port", str(proxy_port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{proxy_port}"
    try:
        # readiness — poll /status
        ready = False
        for _ in range(80):
            try:
                with urllib.request.urlopen(base + "/status", timeout=1) as r:  # noqa: S310
                    if r.status == 200:
                        ready = True
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        if not ready:
            out = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"iicp-node proxy did not become ready on :{proxy_port}\n{out}")

        msgs = [{"role": "user", "content": "hi"}]

        # OpenAI
        with _post(base + "/v1/chat/completions", {"model": "iicp", "messages": msgs}) as r:
            assert r.status == 200
            assert r.headers.get("Server") == "iicp-proxy"
            assert r.headers.get("X-IICP-Generated-By-AI") == "true"
            d = json.loads(r.read())
            assert d["choices"][0]["message"]["content"] == "E2E reply"

        # Ollama (non-stream)
        with _post(base + "/api/chat", {"model": "iicp", "stream": False, "messages": msgs}) as r:
            assert r.status == 200
            assert r.headers.get("Server") == "iicp-proxy"
            assert r.headers.get("X-IICP-Generated-By-AI") == "true"
            d = json.loads(r.read())
            assert d["message"]["content"] == "E2E reply"

        # Anthropic
        with _post(base + "/v1/messages", {"model": "iicp", "max_tokens": 32, "messages": msgs}) as r:
            assert r.status == 200
            assert r.headers.get("Server") == "iicp-proxy"
            assert r.headers.get("X-IICP-Generated-By-AI") == "true"
            d = json.loads(r.read())
            assert d["content"][0]["text"] == "E2E reply"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()
