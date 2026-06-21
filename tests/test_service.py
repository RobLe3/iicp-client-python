from __future__ import annotations

from iicp_client import cli
from iicp_client.service import render_launchd, render_systemd


def test_launchd_unit_runs_foreground_serve_with_hourly_auto_update(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("IICP_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("IICP_AUTO_UPDATE_INTERVAL_S", raising=False)
    unit = render_launchd("mynode")

    assert unit.platform == "launchd"
    assert "network.iicp.node.mynode.plist" in str(unit.path)
    assert "<string>serve</string>" in unit.content
    assert "<string>--node</string>" in unit.content
    assert "<string>mynode</string>" in unit.content
    assert "<key>IICP_AUTO_UPDATE</key><string>1</string>" in unit.content
    assert "<key>IICP_AUTO_UPDATE_INTERVAL_S</key><string>3600</string>" in unit.content
    assert "<key>KeepAlive</key><true/>" in unit.content
    assert "--daemon" not in unit.content


def test_systemd_unit_runs_foreground_serve_with_hourly_auto_update(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("IICP_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("IICP_AUTO_UPDATE_INTERVAL_S", raising=False)
    unit = render_systemd("mynode")

    assert unit.platform == "systemd"
    assert "network.iicp.node.mynode.service" in str(unit.path)
    assert "ExecStart=iicp-node serve --node mynode" in unit.content
    assert "Environment=IICP_AUTO_UPDATE=1" in unit.content
    assert "Environment=IICP_AUTO_UPDATE_INTERVAL_S=3600" in unit.content
    assert "Restart=on-failure" in unit.content
    assert "--daemon" not in unit.content


def test_service_install_dry_run_prints_unit_hints_and_no_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = cli.main(["service", "install", "--node", "mynode", "--platform", "systemd", "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "ExecStart=iicp-node serve --node mynode" in out
    assert "IICP_AUTO_UPDATE_INTERVAL_S=3600" in out
    assert "status:" in out
    assert "restart:" in out
    assert "logs:" in out
    assert "no classic --daemon fork" in out
    assert not (tmp_path / ".config" / "systemd" / "user" / "network.iicp.node.mynode.service").exists()
