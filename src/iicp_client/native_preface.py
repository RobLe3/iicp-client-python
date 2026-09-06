"""Bounded, non-consuming classification for experimental native multiplexing."""
import socket
import time


def peek_protocol_prefix(conn: socket.socket, magic: bytes, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("incomplete protocol prefix")
        conn.settimeout(remaining)
        prefix = conn.recv(len(magic), socket.MSG_PEEK)
        if not prefix or len(prefix) == len(magic) or not magic.startswith(prefix):
            return prefix
        # Peeking leaves partial bytes readable, so select alone would spin.
        # Bound both the wait and retry rate until the entire magic arrives.
        time.sleep(min(0.005, remaining))
