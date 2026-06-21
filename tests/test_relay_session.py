# ADR-016: IICP client SDK conformance — ADR-041 tier-3 / #341 relay R1
"""Unit tests for RelaySessionRegistry and encode helpers (relay_session.py + iicp_tcp.py)."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from unittest.mock import AsyncMock, MagicMock

import cbor2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iicp_client.iicp_tcp import (
    MsgType,
    decode_relay_bind,
    decode_relay_response,
    encode_relay_ack,
    encode_relay_bind,
    encode_relay_call,
)
from iicp_client.relay_session import (
    RelayAcceptServer,
    RelaySessionRegistry,
    RelayWorkerSession,
)

# ── encode/decode helpers ──────────────────────────────────────────────────────


class TestRelayFrameHelpers:
    def test_encode_decode_relay_bind_roundtrip(self):
        raw = encode_relay_bind(
            "w-001", "urn:iicp:intent:llm:chat:v1", ["qwen2.5:0.5b", "phi3:mini"]
        )
        worker_id, intent, models = decode_relay_bind(raw)
        assert worker_id == "w-001"
        assert intent == "urn:iicp:intent:llm:chat:v1"
        assert models == ["qwen2.5:0.5b", "phi3:mini"]

    def test_encode_relay_ack_is_cbor_map(self):
        raw = encode_relay_ack("w-001")
        import cbor2
        body = cbor2.loads(raw)
        assert body[1] == "ok"
        assert body[2] == "w-001"

    def test_encode_relay_call_contains_call_id_and_payload(self):
        raw = encode_relay_call("call-abc", {"messages": []})
        import json

        import cbor2
        body = cbor2.loads(raw)
        assert body[15] == "call-abc"
        payload = json.loads(body[5])
        assert "messages" in payload

    def test_decode_relay_response_extracts_call_id_and_result(self):
        import json

        import cbor2
        result = {"choices": [{"message": {"content": "hi"}}]}
        raw = cbor2.dumps({15: "call-abc", 5: json.dumps(result).encode()}, canonical=True)
        call_id, decoded = decode_relay_response(raw)
        assert call_id == "call-abc"
        assert decoded["choices"][0]["message"]["content"] == "hi"

    def test_decode_relay_bind_empty_models(self):
        raw = encode_relay_bind("w-002", "urn:x", [])
        _, _, models = decode_relay_bind(raw)
        assert models == []

    def test_relay_bind_and_ack_have_correct_msg_types(self):
        assert MsgType.RELAY_BIND == 0x0B
        assert MsgType.RELAY_ACK == 0x0C


# ── RelaySessionRegistry ─────────────────────────────────────────────────────


class TestRelaySessionRegistry:
    def test_bind_and_get(self):
        reg = RelaySessionRegistry()
        writer = MagicMock()
        session = RelayWorkerSession("w-001", writer)
        reg.bind("w-001", session)
        assert reg.get("w-001") is session

    def test_get_missing_returns_none(self):
        reg = RelaySessionRegistry()
        assert reg.get("nobody") is None

    def test_unbind_removes_entry(self):
        reg = RelaySessionRegistry()
        writer = MagicMock()
        session = RelayWorkerSession("w-001", writer)
        reg.bind("w-001", session)
        reg.unbind("w-001")
        assert reg.get("w-001") is None

    def test_is_bound_reflects_state(self):
        reg = RelaySessionRegistry()
        writer = MagicMock()
        session = RelayWorkerSession("w-001", writer)
        assert not reg.is_bound("w-001")
        reg.bind("w-001", session)
        assert reg.is_bound("w-001")
        reg.unbind("w-001")
        assert not reg.is_bound("w-001")

    def test_bound_worker_ids_lists_bound(self):
        reg = RelaySessionRegistry()
        writer = MagicMock()
        reg.bind("a", RelayWorkerSession("a", writer))
        reg.bind("b", RelayWorkerSession("b", writer))
        ids = reg.bound_worker_ids()
        assert set(ids) == {"a", "b"}


# ── RelayWorkerSession.on_response ───────────────────────────────────────────


class TestRelayWorkerSessionOnResponse:
    def test_on_response_resolves_pending_future(self):
        loop = asyncio.new_event_loop()

        async def _run():
            writer = MagicMock()
            writer.drain = AsyncMock()
            writer.write = MagicMock()
            session = RelayWorkerSession("w-001", writer)
            # Manually register a future
            fut = loop.create_future()
            session._pending["call-xyz"] = fut
            session.on_response("call-xyz", {"result": "ok"})
            result = await asyncio.wait_for(fut, timeout=1.0)
            assert result == {"result": "ok"}

        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_on_response_ignores_unknown_call_id(self):
        writer = MagicMock()
        session = RelayWorkerSession("w-001", writer)
        # Should not raise
        session.on_response("unknown-call", {"result": "ok"})


# ── RelayAcceptServer bind hardening (#510) ───────────────────────────────────
# Behavior tests: these fail if the alive-session rebind rejection is reverted.

_HEADER = struct.Struct("!4sBBBBI")
_MT_INIT = 0x01
_MT_ACK = 0x02
_MT_CALL = 0x05
_MT_RESPONSE = 0x06
_MT_RELAY_BIND = 0x0B
_MT_RELAY_ACK = 0x0C


def _frame(msg_type: int, payload: bytes) -> bytes:
    return _HEADER.pack(b"IICP", 0x01, msg_type, 0, 0, len(payload)) + payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(12)
    _, _, mt, _, _, plen = _HEADER.unpack(header)
    payload = await reader.readexactly(plen) if plen else b""
    return mt, payload


async def _start_server(registry: RelaySessionRegistry, **opts) -> tuple[RelayAcceptServer, int]:
    srv = RelayAcceptServer(registry, host="127.0.0.1", port=0, **opts)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    return srv, port


async def _bind_worker(
    port: int, worker_id: str, bind_ticket: str | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    """Wire-level worker: INIT/ACK + RELAY_BIND; returns the RELAY_ACK body."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_frame(_MT_INIT, cbor2.dumps({1: 0x01})))
    await writer.drain()
    mt, _ = await asyncio.wait_for(_read_frame(reader), timeout=5.0)
    assert mt == _MT_ACK
    bind = {1: worker_id, 2: "urn:iicp:intent:llm:chat:v1", 3: []}
    if bind_ticket:
        bind[4] = bind_ticket
    writer.write(_frame(_MT_RELAY_BIND, cbor2.dumps(bind)))
    await writer.drain()
    mt, payload = await asyncio.wait_for(_read_frame(reader), timeout=5.0)
    assert mt == _MT_RELAY_ACK
    return reader, writer, cbor2.loads(payload)


def _signed_ticket(worker_id: str, relay_id: str) -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    payload = base64.urlsafe_b64encode(json.dumps({
        "v": 1, "typ": "relay-bind-ticket", "iss": "test",
        "sub": worker_id, "aud": relay_id, "iat": 1, "exp": 9_999_999_999,
    }, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = private.sign(b"iicp:relay-bind-ticket:v1\n" + payload.encode()).hex()
    return f"{payload}.{sig}", public_hex


class TestRelayBindHardening:
    """#510 interim hardening: alive-session rebind rejection + reconnect."""

    def test_hijack_bind_of_alive_session_is_rejected(self):
        async def _run():
            reg = RelaySessionRegistry()
            srv, port = await _start_server(reg)
            try:
                reader_a, writer_a, ack_a = await _bind_worker(port, "w-hijack")
                assert ack_a[1] == "ok"
                session_a = reg.get("w-hijack")
                assert session_a is not None

                # Attacker on socket B binds the same worker_id while A is alive.
                _reader_b, writer_b, ack_b = await _bind_worker(port, "w-hijack")
                assert ack_b[1] == "error", "second bind of an alive worker must be rejected"

                # A's session remains installed and still receives dispatches.
                assert reg.get("w-hijack") is session_a
                assert session_a.is_alive()

                dispatch = asyncio.ensure_future(
                    session_a.forward_task({"ping": 1}, timeout=5.0)
                )
                mt, payload = await asyncio.wait_for(_read_frame(reader_a), timeout=5.0)
                assert mt == _MT_CALL, "dispatch must arrive on worker A's socket"
                body = cbor2.loads(payload)
                call_id = body[15]
                writer_a.write(
                    _frame(
                        _MT_RESPONSE,
                        cbor2.dumps({15: call_id, 5: json.dumps({"pong": True}).encode()}),
                    )
                )
                await writer_a.drain()
                result = await asyncio.wait_for(dispatch, timeout=5.0)
                assert result == {"pong": True}

                writer_a.close()
                writer_b.close()
            finally:
                await srv.stop()

        asyncio.run(_run())

    def test_rebind_after_socket_death_succeeds(self):
        async def _run():
            reg = RelaySessionRegistry()
            srv, port = await _start_server(reg)
            try:
                _reader_a, writer_a, ack_a = await _bind_worker(port, "w-reconnect")
                assert ack_a[1] == "ok"

                writer_a.close()
                # Wait until the relay observes the dead socket
                # (unbound, or bound-but-dead).
                for _ in range(200):
                    s = reg.get("w-reconnect")
                    if s is None or not s.is_alive():
                        break
                    await asyncio.sleep(0.01)

                _reader_b, writer_b, ack_b = await _bind_worker(port, "w-reconnect")
                assert ack_b[1] == "ok", "rebind after socket death must succeed"
                assert reg.is_bound("w-reconnect")
                assert reg.get("w-reconnect").is_alive()

                writer_b.close()
            finally:
                await srv.stop()

        asyncio.run(_run())

    def test_strict_bind_ticket_accepts_valid_and_rejects_wrong_worker(self):
        async def _run():
            reg = RelaySessionRegistry()
            good_ticket, pub_hex = _signed_ticket("w-ticket", "relay-test")
            bad_ticket, _ = _signed_ticket("attacker", "relay-test")
            srv, port = await _start_server(
                reg,
                require_bind_ticket=True,
                bind_ticket_public_key_hex=pub_hex,
                relay_node_id="relay-test",
            )
            try:
                _reader_a, writer_a, ack_a = await _bind_worker(port, "w-ticket", good_ticket)
                assert ack_a[1] == "ok"
                writer_a.close()
                await writer_a.wait_closed()
                for _ in range(200):
                    s = reg.get("w-ticket")
                    if s is None or not s.is_alive():
                        break
                    await asyncio.sleep(0.01)

                _reader_b, writer_b, ack_b = await _bind_worker(port, "w-ticket", bad_ticket)
                assert ack_b[1] == "error"
                assert ack_b[3] == "relay bind ticket invalid"
                writer_b.close()
            finally:
                await srv.stop()

        asyncio.run(_run())
