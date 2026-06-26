# SPDX-License-Identifier: Apache-2.0
"""Self-updater for provider nodes (#521 WQ-089).

`iicp-node update` still supports the safe read-only version check, but normal
long-running `iicp-node serve` processes now also run a default-on background
loop: check PyPI hourly (first check within five minutes), `pip install
--upgrade` when a newer stable release exists, and re-exec the process so the
node comes back on the new code in covered service paths. The loop is
failure-isolated and opt-out via `IICP_AUTO_UPDATE=0`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from datetime import UTC, datetime

PYPI_URL = "https://pypi.org/pypi/iicp-client/json"
DEFAULT_AUTO_UPDATE_INTERVAL_S = 3600

_status_lock = threading.Lock()
_status: dict[str, str | int | bool | None] = {
    "sdk_latest_seen": None,
    "sdk_update_last_checked_at": None,
    "sdk_update_error_class": None,
}


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable tuple. Non-numeric/pre-release
    suffixes (e.g. '1.2.3rc1') truncate at the first non-numeric segment —
    good enough for the stable-channel compare P1 does."""
    out: list[int] = []
    for part in v.strip().lstrip("vV").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        out.append(int(num))
    return tuple(out)


def is_outdated(current: str, latest: str) -> bool:
    """True when `latest` is strictly newer than `current`."""
    return parse_version(latest) > parse_version(current)


def latest_pypi_version(timeout: float = 5.0) -> str | None:
    """Fetch the latest published version from PyPI, or None on any error."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
            data = json.loads(resp.read().decode())
        v = data.get("info", {}).get("version")
        return str(v) if v else None
    except Exception:  # noqa: BLE001 — offline / registry blip → treat as "unknown"
        return None


def check_update(current: str, latest: str | None) -> dict:
    """Produce a structured update verdict for the CLI to render.

    Returns: {current, latest, outdated, command} — `command` is the exact
    upgrade line for whichever install method the operator used (pip)."""
    outdated = bool(latest) and is_outdated(current, latest)  # type: ignore[arg-type]
    return {
        "current": current,
        "latest": latest,
        "outdated": outdated,
        "command": "pip install -U iicp-client",
    }


# ── P2 — background self-updater (#521) ─────────────────────────────────────────
# A node running `serve` periodically checks the registry and, when a newer
# release is published, upgrades itself and re-execs so it comes back on the new
# version. This removes the dependency on manual upgrades in covered service
# paths. Nodes older than the hardened 0.7.67 serve wiring may need one manual
# upgrade/restart first. Default-on; opt out with IICP_AUTO_UPDATE=0.
# Loop-safe by construction: after a successful upgrade the running version equals
# `latest`, so the next tick is a no-op.


def perform_self_update(spec: str = "iicp-client", timeout: float = 600.0) -> bool:
    """`pip install --upgrade` the package in a subprocess. True on success."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", spec],
            check=True,
            timeout=timeout,
        )
        return True
    except Exception:  # noqa: BLE001 — any failure → "did not upgrade", retry next tick
        return False


def reexec_cli() -> None:
    """Re-exec the current command so the just-upgraded package is loaded. Replaces
    the process image (all threads); returns only if exec failed."""
    try:
        os.execvp(sys.argv[0], sys.argv)  # noqa: S606 — re-running our own argv
    except Exception:  # noqa: BLE001 — fall back to the module entrypoint
        os.execv(sys.executable, [sys.executable, "-m", "iicp_client.cli", *sys.argv[1:]])


def auto_update_enabled() -> bool:
    """Default-on; IICP_AUTO_UPDATE=0 (or false/no) opts out."""
    return os.environ.get("IICP_AUTO_UPDATE", "1").strip().lower() not in {"0", "false", "no", "off"}


def auto_update_interval_s(default: int = DEFAULT_AUTO_UPDATE_INTERVAL_S) -> int:
    """Check cadence in seconds (default 1h), floored at 5 min."""
    try:
        return max(300, int(os.environ.get("IICP_AUTO_UPDATE_INTERVAL_S", str(default))))
    except ValueError:
        return default


def auto_update_initial_delay_s(interval: int) -> int:
    """Delay before the first background check; never later than five minutes."""
    return min(interval, 300)


def auto_update_tick(
    current: str,
    latest: str | None,
    enabled: bool,
    upgrade_fn,
    reexec_fn,
    log_fn,
) -> str:
    """One evaluation of the auto-update rule. Pure orchestration — all I/O is
    injected so the decision is unit-testable. Returns the action taken:
    'disabled' | 'unknown' | 'current' | 'upgraded' | 'upgrade-failed'."""
    if not enabled:
        return "disabled"
    if latest is None:
        return "unknown"
    if not is_outdated(current, latest):
        return "current"
    log_fn(f"auto-update: newer release {latest} available (running {current}) — upgrading…")
    if upgrade_fn():
        log_fn(f"auto-update: upgraded to {latest}; restarting to apply…")
        reexec_fn()  # normally does not return (process replaced)
        return "upgraded"
    log_fn("auto-update: upgrade failed; staying on current version, will retry next check")
    return "upgrade-failed"


def record_update_check(latest: str | None, error_class: str | None = None) -> None:
    """Record the latest updater check for heartbeat observability."""
    with _status_lock:
        _status["sdk_latest_seen"] = latest
        _status["sdk_update_last_checked_at"] = datetime.now(UTC).isoformat()
        _status["sdk_update_error_class"] = error_class


def auto_update_status_payload() -> dict[str, str | int | bool | None]:
    """Optional heartbeat fields that let the directory see updater health."""
    with _status_lock:
        snapshot = dict(_status)
    snapshot["auto_update_enabled"] = auto_update_enabled()
    snapshot["auto_update_interval_s"] = auto_update_interval_s()
    return snapshot


def start_auto_update_loop(
    current: str,
    *,
    stop_event: threading.Event | None = None,
    latest_fn=latest_pypi_version,
    upgrade_fn=perform_self_update,
    reexec_fn=reexec_cli,
    log_fn=print,
) -> threading.Event | None:
    """Start the default-on background updater for long-running node processes.

    Returns the stop event when a loop was started; None when auto-update is disabled.
    """
    if not auto_update_enabled():
        return None
    stop = stop_event or threading.Event()
    interval = auto_update_interval_s()

    def _loop() -> None:
        wait = auto_update_initial_delay_s(interval)
        while not stop.wait(wait):
            wait = interval
            try:
                latest = latest_fn()
                record_update_check(latest, None if latest is not None else "latest_unknown")
                auto_update_tick(current, latest, True, upgrade_fn, reexec_fn, log_fn)
            except Exception as exc:  # noqa: BLE001 — updater must never take down the node
                record_update_check(None, exc.__class__.__name__)

    threading.Thread(target=_loop, daemon=True, name="iicp-auto-update").start()
    return stop
