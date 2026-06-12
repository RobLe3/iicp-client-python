# SPDX-License-Identifier: Apache-2.0
"""Self-updater P1 — read-only version check (#521 WQ-089).

This phase is deliberately inert: it tells the operator whether a newer
release exists and prints the exact upgrade command. No download, no install,
no restart — those are P2/P3 (opt-in, signed). Zero risk surface; answers the
"a user several versions behind shouldn't have to worry" goal by making the
gap visible at a glance.
"""

from __future__ import annotations

import json
import urllib.request

PYPI_URL = "https://pypi.org/pypi/iicp-client/json"


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
