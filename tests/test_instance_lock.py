"""#405 — single-instance lock per node_id."""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

import pytest

from iicp_client.instance_lock import InstanceLock, NodeAlreadyServingError


def test_windows_liveness_probe_never_calls_os_kill():
    with (
        mock.patch("iicp_client.instance_lock._WINDOWS", True),
        mock.patch(
            "iicp_client.instance_lock._pid_alive_windows", return_value=True
        ) as windows_probe,
        mock.patch("iicp_client.instance_lock.os.kill") as kill,
    ):
        from iicp_client.instance_lock import _pid_alive

        assert _pid_alive(1234) is True
    windows_probe.assert_called_once_with(1234)
    kill.assert_not_called()


def test_live_foreign_pid_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    # a real, same-user, signalable live process holding the lock
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        run = tmp_path / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / "dup.pid").write_text(str(child.pid))
        with pytest.raises(NodeAlreadyServingError):
            InstanceLock.acquire("dup", force=False)
        # --force takes over
        InstanceLock.acquire("dup", force=True)
    finally:
        child.terminate()
        child.wait()


def test_distinct_nodes_and_release(tmp_path, monkeypatch):
    monkeypatch.setenv("IICP_HOME", str(tmp_path))
    a = InstanceLock.acquire("node-a", force=False)
    b = InstanceLock.acquire("node-b", force=False)  # distinct → no conflict
    assert a and b
    a.release()
    # re-acquirable after release
    InstanceLock.acquire("node-a", force=False)
