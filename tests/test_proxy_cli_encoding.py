"""Redirected Windows consoles must not prevent proxy startup."""
import io
from argparse import Namespace
from unittest.mock import Mock

from iicp_client import cli


def test_proxy_startup_supports_cp1252_stdout(monkeypatch, tmp_path):
    run = Mock()
    monkeypatch.setattr('uvicorn.run', run)
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding='cp1252', errors='strict')
    monkeypatch.setattr(cli.sys, 'stdout', output)
    assert cli._cmd_proxy(Namespace(config=str(tmp_path / "absent.toml"), host='127.0.0.1', port=9484)) == 0
    output.flush()
    assert b'iicp-node proxy ->' in buffer.getvalue()
    run.assert_called_once()
