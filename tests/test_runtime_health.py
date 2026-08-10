import json
from pathlib import Path

from iicp_client.runtime_health import ClassificationInput, RuntimeHealth, classify, write_snapshot


def test_canonical_fixture():
    data = json.loads((Path(__file__).parent / "fixtures/runtime-health-v1.json").read_text())
    assert len(data["scenarios"]) == 12
    for s in data["scenarios"]:
        assert classify(ClassificationInput(**s["input"])) == s["expected"], s["id"]


def test_snapshot_private(tmp_path):
    h = RuntimeHealth()
    h.mark_running()
    h.advance_runtime()
    p = tmp_path / "run" / "health.json"
    write_snapshot(p, h.snapshot())
    assert json.loads(p.read_text())["liveness"] == "live"
    if __import__("os").name == "posix":
        assert p.stat().st_mode & 0o777 == 0o600


def test_healthcheck_cli_exit_semantics(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from iicp_client.cli import _cmd_healthcheck

    monkeypatch.setenv("HOME", str(tmp_path))
    health = RuntimeHealth()
    health.mark_running()
    health.advance_runtime()
    path = tmp_path / ".iicp" / "run" / "test-node" / "health-v1.json"
    write_snapshot(path, health.snapshot())
    assert _cmd_healthcheck(Namespace(node="test-node", json=True, ready=False)) == 0
    assert json.loads(capsys.readouterr().out)["health_schema_version"] == 1
    assert _cmd_healthcheck(Namespace(node="missing", json=False, ready=False)) == 2
