# SPDX-License-Identifier: Apache-2.0
"""Quick-Tunnel escalation — #520 rung 5 of the NAT ladder.

When every NAT variant fails (no direct endpoint, no UPnP pinhole, no IPv6
GUA, no relay-capable peer in the directory), the node can still become
publicly reachable with ZERO account, domain, or router changes: spawn
``cloudflared tunnel --url http://127.0.0.1:<port>`` and register the issued
``https://*.trycloudflare.com`` URL as the endpoint.

Lifecycle is fully automatic ("automagical", maintainer 2026-06-12):
  setup     — detect the cloudflared binary (never auto-installed; supply-chain
              discipline — one actionable hint when missing)
  initiate  — spawn, parse the public URL from process output (≤20 s)
  supervise — watchdog thread; unexpected death → respawn (bounded) and hand
              the NEW url to the caller for re-registration
  tear down — close() terminates the child; also runs via atexit so a normal
              process exit never leaves an orphaned tunnel

Proven live 2026-06-12: a real /v1/task completed through a Quick Tunnel, and
a browser node became directory-LISTED via a tunnel-exposed relay (#452).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# cloudflared usually prints the URL within ~5 s; 20 s covers slow first runs.
TUNNEL_START_TIMEOUT = 20.0
# Bounded self-healing: this many CONSECUTIVE failed respawns (without the tunnel
# recovering to a healthy state in between) → give up. Resets to 0 once a respawned
# tunnel passes a health check, so a long-running relay heals indefinitely. (#538)
MAX_RESPAWNS = 3
# Active liveness check of the tunnel's OWN public URL — catches the failure mode the
# process-exit watcher misses: cloudflared still running but the edge connection
# dropped, so the URL is unreachable while the node looks healthy (the recurring
# dead-endpoint bug, #538). Probe every interval; after this many consecutive
# failures, force a tunnel restart (terminate → respawn → new URL → re-register).
TUNNEL_HEALTH_INTERVAL_S = 30.0
TUNNEL_HEALTH_MAX_FAILS = 2
TUNNEL_VERIFY_TIMEOUT_S = 30.0
TUNNEL_DOH_TIMEOUT_S = 5.0
TUNNEL_RATE_LIMIT_COOLDOWN_S = 15 * 60.0
TUNNEL_CREATE_MIN_INTERVAL_S = 120.0
TUNNEL_CREATE_LEASE_S = 45.0
TUNNEL_DEAD_RETRY_INITIAL_S = 30.0
TUNNEL_DEAD_RETRY_MAX_S = 300.0

_quick_tunnel_rate_limit_until = 0.0


class TunnelState(StrEnum):
    READY = "ready"
    TWILIGHT = "twilight"
    RECOVERING = "recovering"
    DEAD = "dead"


class TunnelDeadAction(StrEnum):
    STOP = "stop"
    RETRY = "retry"


def _dead_retry_delay(attempt: int) -> float:
    exponent = max(0, min(attempt - 1, 4))
    return min(TUNNEL_DEAD_RETRY_INITIAL_S * (2**exponent), TUNNEL_DEAD_RETRY_MAX_S)


def _rate_limit_cooldown_s() -> float:
    try:
        value = float(os.environ.get("IICP_TUNNEL_RATE_LIMIT_COOLDOWN_S", ""))
        return value if value > 0 else TUNNEL_RATE_LIMIT_COOLDOWN_S
    except ValueError:
        return TUNNEL_RATE_LIMIT_COOLDOWN_S


def _quick_tunnel_rate_limit_state_path() -> Path:
    override = os.environ.get("IICP_TUNNEL_RATE_LIMIT_STATE_FILE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("IICP_HOME") or (Path.home() / ".iicp")).expanduser()
    return home / "state" / "quick_tunnel_rate_limit.json"


def _quick_tunnel_create_state_path() -> Path:
    override = os.environ.get("IICP_TUNNEL_CREATE_STATE_FILE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("IICP_HOME") or (Path.home() / ".iicp")).expanduser()
    return home / "state" / "quick_tunnel_create_gate.json"


def _quick_tunnel_create_lock_path() -> Path:
    override = os.environ.get("IICP_TUNNEL_CREATE_LOCK_FILE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("IICP_HOME") or (Path.home() / ".iicp")).expanduser()
    return home / "state" / "quick_tunnel_create.lock"


def _tunnel_create_min_interval_s() -> float:
    try:
        value = float(os.environ.get("IICP_TUNNEL_CREATE_MIN_INTERVAL_S", ""))
        return max(0.0, value)
    except ValueError:
        return TUNNEL_CREATE_MIN_INTERVAL_S


def _tunnel_create_lease_s() -> float:
    try:
        value = float(os.environ.get("IICP_TUNNEL_CREATE_LEASE_S", ""))
        return value if value > 0 else TUNNEL_CREATE_LEASE_S
    except ValueError:
        return TUNNEL_CREATE_LEASE_S


def _read_json_field_float(path: Path, field: str) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0.0
    except Exception as exc:  # noqa: BLE001 — corrupted state must not break serving
        logger.debug("Ignoring unreadable Quick Tunnel state %s: %s", path, exc)
        return 0.0
    try:
        return float(data.get(field, 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _read_persistent_rate_limit_until() -> float:
    return _read_json_field_float(
        _quick_tunnel_rate_limit_state_path(), "quick_tunnel_rate_limited_until"
    )


def _persistent_rate_limit_remaining_s() -> float:
    until = _read_persistent_rate_limit_until()
    remaining = max(0.0, until - time.time())
    if remaining <= 0:
        _clear_persistent_rate_limit_if_safe()
    return remaining


def _persist_rate_limit_until(until_wall_epoch_s: float, cooldown_s: float) -> None:
    path = _quick_tunnel_rate_limit_state_path()
    payload = {
        "quick_tunnel_rate_limited_until": until_wall_epoch_s,
        "cooldown_s": cooldown_s,
        "reason": "cloudflare_quick_tunnel_rate_limit",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 — in-process cooldown still protects this process
        logger.warning("Could not persist Quick Tunnel cooldown state %s: %s", path, exc)


def _clear_persistent_rate_limit_if_safe() -> None:
    path = _quick_tunnel_rate_limit_state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not clear expired Quick Tunnel cooldown state %s: %s", path, exc)


def _clear_create_gate_if_safe() -> None:
    path = _quick_tunnel_create_state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not clear expired Quick Tunnel create-gate state %s: %s", path, exc)


def _quick_tunnel_rate_limit_remaining_s() -> float:
    return max(0.0, _quick_tunnel_rate_limit_until - time.monotonic(), _persistent_rate_limit_remaining_s())


def _mark_quick_tunnel_rate_limited() -> float:
    global _quick_tunnel_rate_limit_until
    cooldown = _rate_limit_cooldown_s()
    _quick_tunnel_rate_limit_until = time.monotonic() + cooldown
    _persist_rate_limit_until(time.time() + cooldown, cooldown)
    return cooldown


def _quick_tunnel_create_gate_remaining_s() -> float:
    until = _read_json_field_float(
        _quick_tunnel_create_state_path(), "quick_tunnel_create_not_before"
    )
    remaining = max(0.0, until - time.time())
    if remaining <= 0:
        _clear_create_gate_if_safe()
    return remaining


def _mark_quick_tunnel_create_attempt() -> None:
    interval = _tunnel_create_min_interval_s()
    if interval <= 0:
        _clear_create_gate_if_safe()
        return
    path = _quick_tunnel_create_state_path()
    payload = {
        "quick_tunnel_create_not_before": time.time() + interval,
        "interval_s": interval,
        "pid": os.getpid(),
        "reason": "host_wide_quick_tunnel_creation_pacing",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist Quick Tunnel create-gate state %s: %s", path, exc)


class _QuickTunnelCreateLease:
    def __init__(self, path: Path | None):
        self.path = path

    def close(self) -> None:
        if self.path is None:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("Could not release Quick Tunnel create lock %s", self.path)


def _acquire_quick_tunnel_create_lease() -> _QuickTunnelCreateLease:
    path = _quick_tunnel_create_lock_path()
    lease = _tunnel_create_lease_s()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — fail open; the create gate still helps
        logger.warning("Could not create Quick Tunnel lock directory %s: %s", path.parent, exc)
        return _QuickTunnelCreateLease(None)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            expires = _read_json_field_float(path, "expires_at")
            remaining = max(0.0, expires - time.time())
            if remaining > 0:
                raise RuntimeError(
                    f"accountless Quick Tunnel creation held by another local IICP node for "
                    f"{remaining:.0f}s; falling back to the previous reachability method"
                ) from None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        except Exception as exc:  # noqa: BLE001 — fail open; do not prevent serving
            logger.warning("Could not acquire Quick Tunnel create lock %s: %s", path, exc)
            return _QuickTunnelCreateLease(None)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "expires_at": time.time() + lease,
                    "lease_s": lease,
                    "reason": "host_wide_quick_tunnel_creation_lock",
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
        return _QuickTunnelCreateLease(path)
    raise RuntimeError(
        f"accountless Quick Tunnel creation held by another local IICP node for "
        f"{lease:.0f}s; falling back to the previous reachability method"
    )


def _cloudflared_output_is_rate_limited(lines: deque[str]) -> bool:
    joined = " ".join(lines).lower()
    return (
        "429" in joined
        or "too many requests" in joined
        or "error code: 1015" in joined
        or "rate limit" in joined
    )


def _reset_quick_tunnel_rate_limit_for_tests(*, clear_persistent: bool = False) -> None:
    global _quick_tunnel_rate_limit_until
    _quick_tunnel_rate_limit_until = 0.0
    if clear_persistent:
        _clear_persistent_rate_limit_if_safe()
        _clear_create_gate_if_safe()
        try:
            _quick_tunnel_create_lock_path().unlink()
        except FileNotFoundError:
            pass


def _trycloudflare_host(url: str) -> str | None:
    if not url.strip().startswith("https://"):
        return None
    host = url.strip()[len("https://") :].split("/", 1)[0]
    if not host.endswith(".trycloudflare.com"):
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return host


def _error_message_is_likely_dns(message: str) -> bool:
    msg = message.lower()
    return (
        "dns" in msg
        or "failed to lookup address" in msg
        or "nodename nor servname" in msg
        or "name or service not known" in msg
        or "temporary failure in name resolution" in msg
        or "enotfound" in msg
        or "eai_again" in msg
    )


def _doh_has_answer(host: str, record_type: str) -> bool:
    req = urllib.request.Request(
        f"https://cloudflare-dns.com/dns-query?name={host}&type={record_type}",
        headers={"accept": "application/dns-json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TUNNEL_DOH_TIMEOUT_S) as resp:  # noqa: S310
            if not (200 <= resp.status < 300):
                return False
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — DoH is only a false-negative guard
        return False
    return body.get("Status") == 0 and bool(body.get("Answer"))


def _trycloudflare_published_via_doh(url: str) -> bool:
    host = _trycloudflare_host(url)
    if not host:
        return False
    return _doh_has_answer(host, "A") or _doh_has_answer(host, "AAAA")


def _tunnel_url_reachable(url: str) -> bool:
    """GET ``<url>/iicp/health`` through the Cloudflare edge back to the local node —
    the same path a browser consumer takes — so it detects an edge-drop, not just a
    local-process death. Local resolvers can lag freshly-created accountless
    ``trycloudflare.com`` records; if local DNS fails but Cloudflare DoH already
    publishes the hostname, keep the tunnel alive so we do not create→verify→kill-loop
    fresh public URLs.
    """
    probe = url.rstrip("/") + "/iicp/health"
    try:
        with urllib.request.urlopen(probe, timeout=8) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        if _error_message_is_likely_dns(str(exc)) and _trycloudflare_published_via_doh(url):
            logger.warning(
                "Local DNS has not resolved %s yet, but Cloudflare DoH already "
                "publishes it — keeping tunnel alive.",
                url,
            )
            return True
        return False


def _wait_until_reachable(
    url: str,
    probe: Callable[[str], bool],
    timeout: float = TUNNEL_VERIFY_TIMEOUT_S,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if probe(url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)

INSTALL_HINT = (
    "cloudflared not found — install it to become reachable without router "
    "changes (zero-account Quick Tunnel): "
    "macOS `brew install cloudflared` · Linux: https://pkg.cloudflare.com · "
    "Windows `winget install Cloudflare.cloudflared`"
)


def cloudflared_path() -> str | None:
    """Locate the cloudflared binary, or None (we never auto-install it)."""
    return shutil.which("cloudflared")


class QuickTunnel:
    """A running Quick Tunnel: public ``url`` → ``http://127.0.0.1:<port>``."""

    def __init__(self, process: subprocess.Popen, url: str, local_port: int, binary: str) -> None:
        self.process = process
        self.url = url
        self.local_port = local_port
        self._binary = binary
        self._closed = False
        self._respawns = 0
        self._watchdog: threading.Thread | None = None
        atexit.register(self.close)

    # ── supervise ────────────────────────────────────────────────────────────

    def watch(self, on_new_url: Callable[[str], None], on_dead: Callable[[], None]) -> None:
        """Start the watchdog: on unexpected exit, respawn (bounded) and call
        ``on_new_url(new_url)`` — Quick Tunnel URLs rotate per process, so the
        caller MUST re-register. After MAX_RESPAWNS, ``on_dead()`` fires once.

        Callbacks run on the watchdog thread; marshal to your loop if needed.
        """

        def _run() -> None:
            health_fails = 0
            last_health = time.monotonic()
            while not self._closed:
                # Wait until the process exits OR the tunnel URL goes unreachable
                # (edge-drop) for too long. Poll instead of process.wait() so the
                # health check can run in between.
                while not self._closed:
                    if self.process.poll() is not None:
                        break  # process exited — crash or our health-triggered terminate
                    now = time.monotonic()
                    # #538 — edge-drop detection: cloudflared can stay alive while its
                    # tunnel becomes unreachable. Probe the public URL; restart on a
                    # sustained failure so the dead endpoint can't persist.
                    if now - last_health >= TUNNEL_HEALTH_INTERVAL_S:
                        last_health = now
                        if _tunnel_url_reachable(self.url):
                            health_fails = 0
                            # Recovered/steady → forget prior respawns so a relay's
                            # lifetime edge-drops never exhaust MAX_RESPAWNS.
                            self._respawns = 0
                        else:
                            health_fails += 1
                            if health_fails >= TUNNEL_HEALTH_MAX_FAILS:
                                logger.warning(
                                    "Quick Tunnel %s unreachable %d× while cloudflared is "
                                    "up (edge dropped) — restarting tunnel.",
                                    self.url,
                                    health_fails,
                                )
                                if self.process.poll() is None:
                                    self.process.terminate()
                                health_fails = 0
                                # poll() sees the exit on the next loop → respawn below.
                    time.sleep(0.2)
                if self._closed:
                    return
                self._respawns += 1
                if self._respawns > MAX_RESPAWNS:
                    logger.error(
                        "Quick Tunnel: %d consecutive respawns failed to recover a healthy "
                        "tunnel — giving up. Node is no longer publicly reachable; restart "
                        "`iicp-node serve` to recover.",
                        self._respawns - 1,
                    )
                    on_dead()
                    return
                logger.warning(
                    "Quick Tunnel down — respawning (%d/%d)…",
                    self._respawns,
                    MAX_RESPAWNS,
                )
                try:
                    fresh = open_quick_tunnel(self.local_port, binary=self._binary)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Quick Tunnel respawn failed: %s", exc)
                    on_dead()
                    return
                self.process = fresh.process
                self.url = fresh.url
                health_fails = 0
                last_health = time.monotonic()
                logger.info("Quick Tunnel back up at %s — re-registering.", self.url)
                on_new_url(self.url)

        self._watchdog = threading.Thread(target=_run, name="quick-tunnel-watchdog", daemon=True)
        self._watchdog.start()

    def watch_elastic(
        self,
        on_new_url: Callable[[str], None],
        on_state: Callable[[TunnelState], None],
        on_dead: Callable[[], TunnelDeadAction | str | None],
        *,
        probe: Callable[[str], bool] = _tunnel_url_reachable,
        health_interval: float = TUNNEL_HEALTH_INTERVAL_S,
        verify_timeout: float = TUNNEL_VERIFY_TIMEOUT_S,
        dead_retry_delay: Callable[[int], float] = _dead_retry_delay,
    ) -> None:
        """Public-URL keepalive with twilight/recovery states.

        ``on_new_url`` fires only after the fresh tunnel URL has passed
        ``/iicp/health`` through the public edge, so callers can heartbeat
        ``available:false`` while a tunnel is stale or rebuilding.
        """

        def _run() -> None:
            health_fails = 0
            dead_retries = 0
            last_health = time.monotonic()
            state = TunnelState.READY
            on_state(state)

            def set_state(next_state: TunnelState) -> None:
                nonlocal state
                if state != next_state:
                    state = next_state
                    on_state(next_state)

            def sleep_until_closed(delay: float) -> bool:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if self._closed:
                        return True
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
                return self._closed

            def handle_dead() -> bool:
                nonlocal dead_retries, health_fails
                set_state(TunnelState.DEAD)
                action = on_dead()
                if action not in (TunnelDeadAction.RETRY, TunnelDeadAction.RETRY.value):
                    return False
                dead_retries += 1
                delay = dead_retry_delay(dead_retries)
                logger.warning(
                    "Quick Tunnel dead-state retry policy active — retrying in %.0fs.",
                    delay,
                )
                if sleep_until_closed(delay):
                    return False
                self._respawns = 0
                health_fails = 0
                set_state(TunnelState.RECOVERING)
                return True

            while not self._closed:
                while not self._closed:
                    if self.process.poll() is not None:
                        break
                    now = time.monotonic()
                    if now - last_health >= health_interval:
                        last_health = now
                        if probe(self.url):
                            health_fails = 0
                            self._respawns = 0
                            set_state(TunnelState.READY)
                        else:
                            health_fails += 1
                            set_state(TunnelState.TWILIGHT)
                            if health_fails >= TUNNEL_HEALTH_MAX_FAILS:
                                logger.warning(
                                    "Quick Tunnel %s unreachable %d× while cloudflared is "
                                    "up (twilight) — rebuilding tunnel.",
                                    self.url,
                                    health_fails,
                                )
                                set_state(TunnelState.RECOVERING)
                                if self.process.poll() is None:
                                    self.process.terminate()
                                health_fails = 0
                    time.sleep(0.2)
                if self._closed:
                    return
                set_state(TunnelState.RECOVERING)
                self._respawns += 1
                if self._respawns > MAX_RESPAWNS:
                    logger.error(
                        "Quick Tunnel: %d consecutive respawns failed to recover a healthy "
                        "tunnel — giving up. Node is no longer publicly reachable; restart "
                        "`iicp-node serve` to recover.",
                        self._respawns - 1,
                    )
                    if handle_dead():
                        continue
                    return
                logger.warning("Quick Tunnel down — respawning (%d/%d)…", self._respawns, MAX_RESPAWNS)
                try:
                    fresh = open_quick_tunnel(self.local_port, binary=self._binary)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Quick Tunnel respawn failed: %s", exc)
                    if handle_dead():
                        continue
                    return
                self.process = fresh.process
                self.url = fresh.url
                health_fails = 0
                logger.info("Quick Tunnel candidate at %s — verifying public health.", self.url)
                if _wait_until_reachable(self.url, probe, verify_timeout):
                    last_health = time.monotonic()
                    self._respawns = 0
                    dead_retries = 0
                    set_state(TunnelState.READY)
                    logger.info("Quick Tunnel verified at %s — re-registering.", self.url)
                    on_new_url(self.url)
                else:
                    logger.warning("Quick Tunnel candidate %s stayed unreachable — rebuilding.", self.url)
                    if self.process.poll() is None:
                        self.process.terminate()

        self._watchdog = threading.Thread(target=_run, name="quick-tunnel-elastic-watchdog", daemon=True)
        self._watchdog.start()

    # ── tear down ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Terminate the tunnel child. Idempotent; also registered via atexit."""
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        logger.info("Quick Tunnel closed.")


def open_quick_tunnel(
    local_port: int,
    timeout: float = TUNNEL_START_TIMEOUT,
    binary: str | None = None,
) -> QuickTunnel:
    """Spawn cloudflared and return the running tunnel with its public URL.

    Raises FileNotFoundError when cloudflared is absent (caller prints
    INSTALL_HINT once) and RuntimeError when no URL appears within ``timeout``.
    """
    remaining = _quick_tunnel_rate_limit_remaining_s()
    if remaining > 0:
        raise RuntimeError(
            f"accountless Quick Tunnel creation paused for {remaining:.0f}s after "
            "Cloudflare rate limiting; retry later or configure a named tunnel / "
            "IICP_PUBLIC_ENDPOINT"
        )
    create_remaining = _quick_tunnel_create_gate_remaining_s()
    if create_remaining > 0:
        raise RuntimeError(
            f"accountless Quick Tunnel creation paced for {create_remaining:.0f}s to avoid "
            "Cloudflare rate limits; falling back to the previous reachability method "
            "while the tunnel budget recovers"
        )

    resolved = binary or cloudflared_path()
    if not resolved:
        raise FileNotFoundError(INSTALL_HINT)
    create_lease = _acquire_quick_tunnel_create_lease()
    _mark_quick_tunnel_create_attempt()
    try:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [resolved, "tunnel", "--url", f"http://127.0.0.1:{local_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        create_lease.close()
        raise
    # Read on a thread: readline() on the main thread would block forever if
    # the child prints nothing, defeating the deadline. The same thread keeps
    # draining after the URL is found so the child never stalls on a full pipe
    # (cloudflared logs continuously).
    lines: queue.Queue[str | None] = queue.Queue()
    url_found = threading.Event()  # once set, the reader drains without queueing

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not url_found.is_set():
                lines.put(line)
        lines.put(None)  # EOF sentinel

    threading.Thread(target=_reader, name="quick-tunnel-read", daemon=True).start()

    deadline = time.monotonic() + timeout
    url: str | None = None
    last_lines: deque[str] = deque(maxlen=6)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            break
        if line is None:
            break  # process exited before printing a URL
        last_lines.append(line.strip())
        m = _URL_RE.search(line)
        if m:
            url = m.group(0)
            url_found.set()
            break
    if url is None:
        create_lease.close()
        proc.terminate()
        reason = f"cloudflared produced no tunnel URL within {timeout:.0f}s (exit={proc.poll()})"
        if last_lines:
            reason += "; last cloudflared output: " + " | ".join(last_lines)
        if _cloudflared_output_is_rate_limited(last_lines):
            cooldown = _mark_quick_tunnel_rate_limited()
            reason += (
                f"; accountless Quick Tunnel rate limit detected — pausing tunnel "
                f"creation for {cooldown:.0f}s"
            )
        raise RuntimeError(reason)
    create_lease.close()
    logger.info("Quick Tunnel up: %s → http://127.0.0.1:%d", url, local_port)
    return QuickTunnel(proc, url, local_port, resolved)
