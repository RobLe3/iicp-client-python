import socket
import threading
import time
from unittest.mock import Mock, patch

import pytest

from iicp_client.native_preface import peek_protocol_prefix


def test_fragmented_magic_is_not_consumed(monkeypatch):
    monkeypatch.delattr(socket, 'MSG_WAITALL', raising=False)
    reader, writer = socket.socketpair()
    def send():
        for byte in b'IICP':
            writer.sendall(bytes([byte]))
            time.sleep(0.01)
    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert peek_protocol_prefix(reader, b'IICP', timeout=2) == b'IICP'
        assert reader.recv(4) == b'IICP'
    finally:
        thread.join()
        reader.close()
        writer.close()


def test_partial_prefix_timeout_is_bounded():
    conn = Mock()
    conn.recv.return_value = b'I'
    with patch('iicp_client.native_preface.time.monotonic', side_effect=[0, 0, 2]):
        with pytest.raises(TimeoutError):
            peek_protocol_prefix(conn, b'IICP', timeout=1)
    assert conn.recv.call_count == 1


@pytest.mark.parametrize('prefix', [b'', b'GET ', b'P'])
def test_eof_and_http_prefixes_return_without_wait(prefix):
    conn = Mock()
    conn.recv.return_value = prefix
    assert peek_protocol_prefix(conn, b'IICP', timeout=1) == prefix
    conn.recv.assert_called_once_with(4, socket.MSG_PEEK)
