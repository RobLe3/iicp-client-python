"""#405 — single-instance lock per node_id.

Two `iicp-node serve` processes for the SAME node_id fight: each registration
rotates the directory-issued token and invalidates the other's, so they enter a
401 -> re-register war that makes the node flap in the directory. This guard
holds a pidfile at ``~/.iicp/run/<node_id>.pid``; a second LIVE process for the
same node_id is refused (unless ``force``). Distinct node_ids are unaffected — a
fleet of N nodes runs fine (each has its own lock).

Fail-open: any filesystem error degrades to a no-op lock — the guard must never
prevent a node from starting.
"""

from __future__ import annotations

import os
from pathlib import Path

_WINDOWS = os.name == "nt"


def _run_dir() -> Path:
    base = Path(os.environ.get("IICP_HOME") or (Path.home() / ".iicp"))
    return base / "run"


def _pid_alive_windows(pid: int) -> bool:
    """Query a Windows process without using ``os.kill(pid, 0)``.

    Python maps every non-console-control ``os.kill`` signal on Windows,
    including zero, to ``TerminateProcess``. A Unix-style liveness probe would
    therefore kill the node it was checking.
    """

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    # These APIs exist only on Windows, while mypy is normally run against the
    # host platform's ctypes stubs.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error == error_invalid_parameter:
            return False
        # A protected process is still alive; unknown query failures fail
        # closed so a second node cannot start a token-rotation fight.
        return True
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists. PermissionError means it exists
    (we just may not signal it) — treat as alive to be safe."""
    if _WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class NodeAlreadyServingError(RuntimeError):
    """Raised when another live process already serves this node_id."""


class InstanceLock:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    @classmethod
    def acquire(cls, node_id: str, force: bool = False) -> InstanceLock:
        """Acquire the per-node_id lock. Raises NodeAlreadyServingError if another
        LIVE process holds it and ``force`` is False. Fails open on I/O error."""
        try:
            d = _run_dir()
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{node_id}.pid"
        except OSError:
            return cls(None)  # fail open
        if not force and path.exists():
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pid = None
            if pid is not None and pid != os.getpid() and _pid_alive(pid):
                raise NodeAlreadyServingError(
                    f"node_id {node_id} is already being served by PID {pid}. "
                    f"Stop that process, choose a different --node, or pass --force to take over."
                )
        try:
            path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            return cls(None)
        return cls(path)

    def release(self) -> None:
        if self._path is not None:
            try:
                self._path.unlink()
            except OSError:
                pass
