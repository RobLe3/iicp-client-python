from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from iicp_client.dispatch_ticket_trust import FileTrustBundleStore, TrustBundle, TrustBundleStoreError
from iicp_client.windows_private_path import windows_private_path


def test_encoded_input_and_tool_failure_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"OK"))
    monkeypatch.setattr(subprocess, "run", run)
    path = tmp_path / "quoted'$;path"
    windows_private_path(path, "file-check")
    args, kwargs = run.call_args
    script = base64.b64decode(args[0][-1]).decode("utf-16le")
    assert str(path) not in script
    assert kwargs["timeout"] == 10
    with pytest.raises(PermissionError):
        windows_private_path(path, "bad'")
    with pytest.raises(PermissionError):
        windows_private_path(Path("relative"), "file-check")
    run.side_effect = subprocess.TimeoutExpired("security-tool", 10)
    with pytest.raises(PermissionError):
        windows_private_path(path, "file-check")
    run.side_effect = FileNotFoundError()
    with pytest.raises(PermissionError):
        windows_private_path(path, "file-check")
    run.side_effect = None
    run.return_value = subprocess.CompletedProcess([], 0, b"EXISTS")
    with pytest.raises(FileExistsError):
        windows_private_path(path, "file-create")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL/reparse semantics")
def test_windows_store_rejects_broad_acl_and_junction(tmp_path: Path) -> None:
    path = tmp_path / "private" / "bundle.state"
    store = FileTrustBundleStore(path)
    bundle = TrustBundle.from_dict({"bundle_version": 1, "keys": []})
    store.install(bundle)
    before = path.read_bytes()
    tool = str(Path(os.environ["SystemRoot"]) / "System32/icacls.exe")
    subprocess.run([tool, str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True, timeout=10)
    with pytest.raises(TrustBundleStoreError):
        store.load()
    assert path.read_bytes() == before
    subprocess.run([tool, str(path), "/remove:g", "*S-1-1-0"], check=True, capture_output=True, timeout=10)
    assert store.load() is not None
    alias = tmp_path / "alias"
    encode = lambda p: base64.b64encode(str(p).encode()).decode()  # noqa: E731
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$a=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encode(alias)}')); "
        f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encode(path.parent)}')); "
        "New-Item -ItemType Junction -Path $a -Target $p | Out-Null"
    )
    powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode()],
                   check=True, capture_output=True, timeout=10)
    try:
        with pytest.raises(TrustBundleStoreError):
            FileTrustBundleStore(alias / "bundle.state").load()
    finally:
        alias.rmdir()
