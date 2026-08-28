from __future__ import annotations

import json
from pathlib import Path

import pytest

from iicp_client.identity import NodeIdentity, load_node, node_path, save_node


def test_malformed_node_configuration_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    path = node_path("malformed")
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_node("malformed")


def test_missing_node_configuration_is_explicitly_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    assert load_node("missing") is None


def test_permission_denied_config_write_leaves_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    node = NodeIdentity.generate(
        operator_id="operator-test",
        name="permission-denied",
        backend_url="http://127.0.0.1:11434",
        model="test-model",
    )
    destination = node_path(node.name)
    original = Path.write_text

    def refuse(path: Path, *_args: object, **_kwargs: object) -> int:
        if path == destination:
            raise PermissionError("simulated permission denied")
        return original(path, *_args, **_kwargs)

    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(PermissionError, match="permission denied"):
        save_node(node)
    assert not destination.exists()
