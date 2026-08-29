from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from iicp_client._http import post_json
from iicp_client._http_resource import (
    MAX_HTTP_TASK_BODY_BYTES,
    HttpTaskBodyError,
    bounded_request_json,
    bounded_response_json,
    encode_task_json,
    read_task_request_body,
)
from iicp_client.errors import IicpError


class _Headers(dict[str, str]):
    def __init__(self, values: dict[str, list[str]]) -> None:
        super().__init__({key: items[-1] for key, items in values.items()})
        self._values = values

    def get_all(self, name: str) -> list[str] | None:
        for key, values in self._values.items():
            if key.lower() == name.lower():
                return values
        return None


class _NoRead(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        raise AssertionError("declared oversize body must not be read")


def _json_value_with_size(size: int) -> dict[str, str]:
    overhead = len(encode_task_json({"padding": ""}))
    assert size >= overhead
    value = {"padding": "x" * (size - overhead)}
    assert len(encode_task_json(value)) == size
    return value


def _chunked(payload: bytes) -> bytes:
    return f"{len(payload):X}\r\n".encode() + payload + b"\r\n0\r\n\r\n"


def test_request_exact_limit_is_accepted_and_limit_plus_one_is_rejected() -> None:
    exact = _json_value_with_size(MAX_HTTP_TASK_BODY_BYTES)
    assert len(bounded_request_json(exact)) == MAX_HTTP_TASK_BODY_BYTES
    with pytest.raises(HttpTaskBodyError, match="request_too_large"):
        bounded_request_json(_json_value_with_size(MAX_HTTP_TASK_BODY_BYTES + 1))


def test_shared_fixture_matches_implementation_boundary() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "parity/http-task-resource-boundary-v1.json").read_text()
    )
    assert fixture["max_encoded_request_bytes"] == MAX_HTTP_TASK_BODY_BYTES
    assert fixture["max_encoded_response_bytes"] == MAX_HTTP_TASK_BODY_BYTES
    assert fixture["supported_content_encodings"] == ["identity"]


def test_declared_oversize_is_rejected_before_read() -> None:
    headers = _Headers({"Content-Length": [str(MAX_HTTP_TASK_BODY_BYTES + 1)]})
    with pytest.raises(HttpTaskBodyError) as caught:
        read_task_request_body(headers, _NoRead())
    assert caught.value.status == 413
    assert caught.value.code == "request_too_large"


def test_conflicting_content_length_and_unsupported_encoding_are_rejected() -> None:
    with pytest.raises(HttpTaskBodyError) as conflicting:
        read_task_request_body(_Headers({"Content-Length": ["12", "13"]}), io.BytesIO())
    assert (conflicting.value.status, conflicting.value.code) == (400, "invalid_http_body")
    with pytest.raises(HttpTaskBodyError) as encoded:
        read_task_request_body(
            _Headers({"Content-Length": ["0"], "Content-Encoding": ["gzip"]}),
            io.BytesIO(),
        )
    assert (encoded.value.status, encoded.value.code) == (415, "unsupported_content_encoding")


def test_chunked_body_is_counted_incrementally() -> None:
    exact = b"x" * MAX_HTTP_TASK_BODY_BYTES
    headers = _Headers({"Transfer-Encoding": ["chunked"]})
    assert read_task_request_body(headers, io.BytesIO(_chunked(exact))) == exact
    with pytest.raises(HttpTaskBodyError) as caught:
        read_task_request_body(headers, io.BytesIO(_chunked(exact + b"x")))
    assert (caught.value.status, caught.value.code) == (413, "request_too_large")


def test_truncated_declared_body_is_rejected() -> None:
    with pytest.raises(HttpTaskBodyError) as caught:
        read_task_request_body(_Headers({"Content-Length": ["4"]}), io.BytesIO(b"{}"))
    assert (caught.value.status, caught.value.code) == (400, "invalid_http_body")


def test_generated_response_is_bounded() -> None:
    status, exact = bounded_response_json(_json_value_with_size(MAX_HTTP_TASK_BODY_BYTES))
    assert status == 200
    assert len(exact) == MAX_HTTP_TASK_BODY_BYTES
    status, replacement = bounded_response_json(_json_value_with_size(MAX_HTTP_TASK_BODY_BYTES + 1))
    assert status == 500
    assert len(replacement) < MAX_HTTP_TASK_BODY_BYTES
    assert json.loads(replacement)["error"]["code"] == "response_too_large"


@pytest.mark.asyncio
async def test_client_rejects_oversize_request_before_network() -> None:
    with pytest.raises(IicpError) as caught:
        await post_json(
            "http://127.0.0.1:9/v1/task",
            _json_value_with_size(MAX_HTTP_TASK_BODY_BYTES + 1),
            tls_verify=False,
        )
    assert caught.value.code == "request_too_large"
    assert caught.value.retryable is False
    assert caught.value.http_status == 413


@pytest.mark.asyncio
async def test_client_aborts_declared_oversize_response_as_non_retryable() -> None:
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(4096)
        writer.write(
            (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {MAX_HTTP_TASK_BODY_BYTES + 1}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(IicpError) as caught:
            await post_json(
                f"http://127.0.0.1:{port}/v1/task",
                {"task_id": "bounded"},
                tls_verify=False,
            )
        assert caught.value.code == "response_too_large"
        assert caught.value.retryable is False
        assert caught.value.http_status == 500
    finally:
        server.close()
        await server.wait_closed()
