"""OS supervisor unit rendering for `iicp-node service` (#551).

The node itself stays a foreground process (`iicp-node serve --node <name>`).
These helpers generate launchd/systemd units that let the OS own persistence,
restart-on-failure and logs. No classic detach/fork daemonization is used.
"""
from __future__ import annotations

import os
import platform as _platform
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ServiceUnit:
    platform: str
    name: str
    path: Path
    content: str
    status_hint: str
    restart_hint: str
    uninstall_hint: str
    log_hint: str


def sanitize_name(value: str) -> str:
    cleaned = _SAFE.sub("-", value.strip()).strip("-.")
    if not cleaned:
        raise ValueError("service/node name must contain at least one safe character")
    return cleaned[:80]


def service_label(node: str, name: str | None = None) -> str:
    return sanitize_name(name or f"network.iicp.node.{sanitize_name(node)}")


def _env_value(key: str, default: str) -> str:
    return os.environ.get(key, default)


def detect_platform(requested: str = "auto") -> str:
    if requested != "auto":
        if requested not in {"launchd", "systemd"}:
            raise ValueError("platform must be auto, launchd or systemd")
        return requested
    if _platform.system() == "Darwin":
        return "launchd"
    return "systemd"


def render_launchd(node: str, *, name: str | None = None, executable: str = "iicp-node") -> ServiceUnit:
    label = service_label(node, name)
    home = Path.home()
    log_dir = Path(_env_value("IICP_LOG_DIR", str(home / ".iicp" / "logs"))).expanduser()
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    env = {
        "IICP_NODE_NAME": node,
        "IICP_AUTO_UPDATE": _env_value("IICP_AUTO_UPDATE", "1"),
        "IICP_AUTO_UPDATE_INTERVAL_S": _env_value("IICP_AUTO_UPDATE_INTERVAL_S", "3600"),
        "IICP_LOG_DIR": str(log_dir),
    }
    env_xml = "\n".join(f"    <key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in env.items())
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(executable)}</string>
    <string>serve</string>
    <string>--node</string>
    <string>{escape(node)}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
{env_xml}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{escape(str(log_dir / (label + ".out.log")))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log_dir / (label + ".err.log")))}</string>
</dict>
</plist>
'''
    return ServiceUnit(
        "launchd", label, plist, content,
        f"launchctl print gui/$(id -u)/{label}",
        f"launchctl kickstart -k gui/$(id -u)/{label}",
        f"launchctl bootout gui/$(id -u) {shlex.quote(str(plist))}; rm -f {shlex.quote(str(plist))}",
        (
            f"tail -f {shlex.quote(str(log_dir / (label + '.out.log')))} "
            f"{shlex.quote(str(log_dir / (label + '.err.log')))}"
        ),
    )


def render_systemd(node: str, *, name: str | None = None, executable: str = "iicp-node") -> ServiceUnit:
    label = service_label(node, name)
    home = Path.home()
    unit_dir = home / ".config" / "systemd" / "user"
    unit_path = unit_dir / f"{label}.service"
    log_dir = Path(_env_value("IICP_LOG_DIR", str(home / ".iicp" / "logs"))).expanduser()
    env = {
        "IICP_NODE_NAME": node,
        "IICP_AUTO_UPDATE": _env_value("IICP_AUTO_UPDATE", "1"),
        "IICP_AUTO_UPDATE_INTERVAL_S": _env_value("IICP_AUTO_UPDATE_INTERVAL_S", "3600"),
        "IICP_LOG_DIR": str(log_dir),
    }
    env_lines = "\n".join(f"Environment={k}={shlex.quote(v)}" for k, v in env.items())
    content = f'''[Unit]
Description=IICP node {node}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} serve --node {shlex.quote(node)}
{env_lines}
Restart=on-failure
RestartSec=30
WorkingDirectory={shlex.quote(str(home))}

[Install]
WantedBy=default.target
'''
    return ServiceUnit(
        "systemd", label, unit_path, content,
        f"systemctl --user status {label}.service",
        f"systemctl --user restart {label}.service",
        (
            f"systemctl --user disable --now {label}.service; "
            f"rm -f {shlex.quote(str(unit_path))}; systemctl --user daemon-reload"
        ),
        f"journalctl --user -u {label}.service -f",
    )


def render_unit(
    node: str,
    *,
    name: str | None = None,
    platform: str = "auto",
    executable: str = "iicp-node",
) -> ServiceUnit:
    chosen = detect_platform(platform)
    if chosen == "launchd":
        return render_launchd(node, name=name, executable=executable)
    return render_systemd(node, name=name, executable=executable)
