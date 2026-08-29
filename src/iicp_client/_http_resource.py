"""Finite resource boundary for the supported HTTP ``POST /v1/task`` binding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

MAX_HTTP_TASK_BODY_BYTES = 1_048_576


@dataclass(frozen=True)
class HttpTaskBodyError(Exception):
    status: int
    code: str
    message: str
    close_connection: bool = False


class _HeadersLike(Protocol):
    def get(self, name: str, default: Any = None) -> Any: ...


class _BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def readline(self, size: int = -1) -> bytes: ...


def encode_task_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def bounded_request_json(value: Any) -> bytes:
    encoded = encode_task_json(value)
    if len(encoded) > MAX_HTTP_TASK_BODY_BYTES:
        raise HttpTaskBodyError(
            413,
            "request_too_large",
            f"encoded task request exceeds {MAX_HTTP_TASK_BODY_BYTES} bytes",
        )
    return encoded


def bounded_response_json(value: Any) -> tuple[int, bytes]:
    encoded = encode_task_json(value)
    if len(encoded) <= MAX_HTTP_TASK_BODY_BYTES:
        return 200, encoded
    return 500, encode_task_json(
        {
            "error": {
                "code": "response_too_large",
                "message": f"encoded task response exceeds {MAX_HTTP_TASK_BODY_BYTES} bytes",
            }
        }
    )


def _header_values(headers: _HeadersLike, name: str) -> list[str]:
    getter = getattr(headers, "get_all", None)
    raw = getter(name) if callable(getter) else None
    if raw is None:
        value = headers.get(name)
        raw = [] if value is None else [value]
    values: list[str] = []
    for item in raw:
        values.extend(part.strip() for part in str(item).split(","))
    return values


def _validate_content_encoding(headers: _HeadersLike) -> None:
    encodings = [value.lower() for value in _header_values(headers, "Content-Encoding")]
    if encodings and any(value != "identity" for value in encodings):
        raise HttpTaskBodyError(
            415,
            "unsupported_content_encoding",
            "supported HTTP task binding accepts identity encoding only",
            True,
        )


def _content_length(headers: _HeadersLike) -> int | None:
    values = _header_values(headers, "Content-Length")
    if not values:
        return None
    if any(not value.isascii() or not value.isdigit() for value in values):
        raise HttpTaskBodyError(400, "invalid_http_body", "invalid Content-Length", True)
    parsed = [int(value, 10) for value in values]
    if len(set(parsed)) != 1:
        raise HttpTaskBodyError(400, "invalid_http_body", "conflicting Content-Length", True)
    return parsed[0]


def _read_exact(stream: _BinaryReader, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise HttpTaskBodyError(400, "invalid_http_body", "request body ended before declared length", True)
    return data


def _read_chunked(stream: _BinaryReader) -> bytes:
    body = bytearray()
    while True:
        line = stream.readline(8194)
        if not line.endswith(b"\r\n") or len(line) > 8193:
            raise HttpTaskBodyError(400, "invalid_http_body", "malformed chunk header", True)
        size_text = line[:-2].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise HttpTaskBodyError(400, "invalid_http_body", "malformed chunk size", True) from exc
        if size < 0:
            raise HttpTaskBodyError(400, "invalid_http_body", "malformed chunk size", True)
        if size == 0:
            trailer_bytes = 0
            while True:
                trailer = stream.readline(8194)
                trailer_bytes += len(trailer)
                if trailer == b"\r\n":
                    return bytes(body)
                if not trailer.endswith(b"\r\n") or trailer_bytes > 8192:
                    raise HttpTaskBodyError(400, "invalid_http_body", "malformed chunk trailer", True)
        if len(body) + size > MAX_HTTP_TASK_BODY_BYTES:
            raise HttpTaskBodyError(
                413,
                "request_too_large",
                f"encoded task request exceeds {MAX_HTTP_TASK_BODY_BYTES} bytes",
                True,
            )
        body.extend(_read_exact(stream, size))
        if _read_exact(stream, 2) != b"\r\n":
            raise HttpTaskBodyError(400, "invalid_http_body", "malformed chunk terminator", True)


def read_task_request_body(headers: _HeadersLike, stream: _BinaryReader) -> bytes:
    _validate_content_encoding(headers)
    content_length = _content_length(headers)
    transfer = [value.lower() for value in _header_values(headers, "Transfer-Encoding")]
    if transfer and content_length is not None:
        raise HttpTaskBodyError(
            400,
            "invalid_http_body",
            "Content-Length and Transfer-Encoding cannot be combined",
            True,
        )
    if transfer:
        if transfer != ["chunked"]:
            raise HttpTaskBodyError(400, "invalid_http_body", "unsupported Transfer-Encoding", True)
        return _read_chunked(stream)
    if content_length is None:
        return b""
    if content_length > MAX_HTTP_TASK_BODY_BYTES:
        raise HttpTaskBodyError(
            413,
            "request_too_large",
            f"encoded task request exceeds {MAX_HTTP_TASK_BODY_BYTES} bytes",
            True,
        )
    return _read_exact(stream, content_length)


def validate_response_headers(headers: _HeadersLike) -> None:
    _validate_content_encoding(headers)
    content_length = _content_length(headers)
    if content_length is not None and content_length > MAX_HTTP_TASK_BODY_BYTES:
        raise HttpTaskBodyError(
            500,
            "response_too_large",
            f"encoded task response exceeds {MAX_HTTP_TASK_BODY_BYTES} bytes",
            True,
        )


def append_response_chunk(buffer: bytearray, chunk: bytes) -> None:
    if len(buffer) + len(chunk) > MAX_HTTP_TASK_BODY_BYTES:
        raise HttpTaskBodyError(
            500,
            "response_too_large",
            f"encoded task response exceeds {MAX_HTTP_TASK_BODY_BYTES} bytes",
            True,
        )
    buffer.extend(chunk)
