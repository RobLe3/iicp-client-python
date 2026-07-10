# SPDX-License-Identifier: Apache-2.0
"""Fail-closed MCP tool-risk policy shared by the built-in gateway (#601)."""
from __future__ import annotations

from dataclasses import dataclass
import re

TOOL_RISK_KEYWORDS: dict[str, set[str]] = {
    "shell_exec": {"bash", "shell", "exec", "run_command", "command", "eval", "python_exec"},
    "data_read": {"read_document", "query_database", "list_resource", "dataset_read", "record_lookup"},
    "file_read": {"read_file", "list_dir", "cat", "open_file", "file_read", "list_files"},
    "file_write": {"write_file", "delete_file", "remove_file", "edit_file", "save_file", "mkdir", "rmdir"},
    "network_fetch": {"fetch", "crawl", "http", "web_request", "search_web", "url"},
    "browser_control": {"browser", "computer_use", "click", "type", "screenshot", "navigate"},
    "credential_access": {"secret", "credential", "token", "ssh_key", "wallet", "password"},
    "system_control": {"systemctl", "launchctl", "service_restart", "install_package", "firewall", "reboot", "shutdown"},
    "physical_world": {"robot", "drone", "actuator", "iot_control", "medical_device", "industrial_control"},
    "regulated_decision": {"credit_score", "hire", "employment", "benefit_eligibility", "diagnose", "triage_patient"},
}
DANGEROUS_TOOL_RISKS = frozenset(TOOL_RISK_KEYWORDS)
SAFE_SANDBOX_PROFILES = frozenset({"1", "true", "strict", "container", "sandbox"})


def tool_risk_label(tool_name: str) -> str:
    safe = re.sub(r"[^a-z0-9_:-]", "_", tool_name.lower())
    for label, needles in TOOL_RISK_KEYWORDS.items():
        if safe in needles or any(needle in safe for needle in needles):
            return label
    return "benign_read"


@dataclass(frozen=True)
class McpToolPolicy:
    allow_dangerous_tools: bool = False
    authz_policy: str = ""
    sandbox_profile: str = ""
    audit_redaction: bool = False

    @property
    def dangerous_ready(self) -> bool:
        return (
            self.allow_dangerous_tools
            and bool(self.authz_policy.strip())
            and self.sandbox_profile.strip().lower() in SAFE_SANDBOX_PROFILES
            and self.audit_redaction
        )

    def allows(self, tool_name: str) -> bool:
        return tool_risk_label(tool_name) not in DANGEROUS_TOOL_RISKS or self.dangerous_ready

    def receipt(self, tool_name: str, decision: str, argument_count: int = 0) -> dict[str, object]:
        return {
            "tool_name": re.sub(r"[^a-zA-Z0-9_:-]", "_", tool_name)[:96],
            "tool_risk": tool_risk_label(tool_name),
            "decision": decision,
            "authz_policy": self.authz_policy[:96] if self.authz_policy else None,
            "sandbox_profile": self.sandbox_profile[:32] if self.sandbox_profile else None,
            "audit_redacted": self.audit_redaction,
            "argument_count": max(0, int(argument_count)),
            "argument_content": "excluded",
        }
