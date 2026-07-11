# SPDX-License-Identifier: Apache-2.0
"""#460 — `iicp-node operator rename <name>` CLI.

Behavior: the command signs the canonical rename bytes with the OPERATOR's own key
and POSTs {operator_pub, display_name, ts, sig} to /v1/operator/rename; operator_pub
equals the operator_id; the signature verifies; the secret/contact are NEVER sent; and
the local operator.json display_name is updated on success. Fails without the wiring.
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from iicp_client.cli import main
from iicp_client.delegation import canonical_operator_self_service_bytes, canonical_rename_bytes
from iicp_client.identity import OperatorIdentity, load_operator, save_operator

_captured: dict = {}


def _serve_once(status: int, body: str) -> int:
    """Single-shot mock of POST /v1/operator/rename that records the request body."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            _captured["payload"] = json.loads(self.rfile.read(length) or b"{}")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_args):  # silence
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return port


def test_rename_signs_with_operator_key_and_updates_local(tmp_path, monkeypatch):
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    _captured.clear()
    op = OperatorIdentity.generate(display_name="Old Name", contact="me@example.com")
    save_operator(op)

    port = _serve_once(200, '{"display_name":"New Name"}')
    rc = main(["operator", "rename", "New Name", "--directory-url", f"http://127.0.0.1:{port}"])
    assert rc == 0

    payload = _captured["payload"]
    # operator_pub IS the operator_id (== base64 ed25519 pubkey, #464).
    assert payload["operator_pub"] == op.operator_id
    assert payload["display_name"] == "New Name"
    # The signature verifies against the operator pubkey over the canonical rename bytes.
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(op.operator_id))
    pub.verify(
        base64.b64decode(payload["sig"]),
        canonical_rename_bytes("New Name", op.operator_id, payload["ts"]),
    )
    # Secret / contact are NEVER transmitted.
    assert "operator_secret" not in payload
    assert "contact" not in payload
    # Local operator.json reflects the new name for the next `serve`.
    assert load_operator().display_name == "New Name"


def test_rename_errors_on_directory_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    save_operator(OperatorIdentity.generate(display_name="Old"))
    port = _serve_once(404, '{"error":{"code":"IICP-E044","message":"unknown operator"}}')
    rc = main(["operator", "rename", "Ghost", "--directory-url", f"http://127.0.0.1:{port}"])
    assert rc == 1
    # A rejected rename must NOT mutate the local identity.
    assert load_operator().display_name == "Old"


def test_dsr_export_is_challenge_signed_and_saved_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    _captured.clear()
    op = OperatorIdentity.generate(display_name="Rights Test")
    save_operator(op)
    export = '{"schema":"iicp.dsr.export.v1","tracking_id":"dsr-test","retention_notice":"ledger retained"}'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/challenge"):
                response = '{"nonce":"nonce-1234567890123456"}'
            else:
                _captured["payload"] = body
                response = export
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response.encode())

        def log_message(self, *_args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=lambda: [srv.handle_request(), srv.handle_request()], daemon=True).start()
    out = tmp_path / "rights.json"
    rc = main(["operator", "dsr", "export", "--directory-url", f"http://127.0.0.1:{port}", "--output", str(out)])
    assert rc == 0
    assert out.exists()
    assert json.loads(out.read_text()) == json.loads(export)
    assert (out.stat().st_mode & 0o777) == 0o600
    payload = _captured["payload"]
    assert payload["operator_pub"] == op.operator_id
    assert "operator_secret" not in payload
    assert "contact" not in payload
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(op.operator_id))
    pub.verify(
        base64.b64decode(payload["sig"]),
        canonical_operator_self_service_bytes("dsr_export", payload),
    )


def test_cli_version_uses_runtime_package_version(capsys):
    from iicp_client import __version__

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"iicp-node {__version__}"
