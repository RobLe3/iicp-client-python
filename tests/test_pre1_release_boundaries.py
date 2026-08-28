from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import iicp_client

ROOT = Path(__file__).parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
QUALITY_RUNNER = (ROOT / "scripts" / "run_sdk_quality.py").read_text(encoding="utf-8")
RELEASE_WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_minimum_python_version_is_declared_and_candidate_remains_pre1() -> None:
    assert PROJECT["requires-python"] == ">=3.11,<3.15"
    assert sys.version_info >= (3, 11)
    assert PROJECT["version"].split(".", 1)[0] == "0"


def test_package_version_self_report_matches_candidate_contract() -> None:
    assert PROJECT["name"] == "iicp-client"
    assert iicp_client.__version__ == PROJECT["version"]


def test_offline_candidate_contract_pins_locked_release_inputs() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "iicp-client"' in lock
    assert f'version = "{PROJECT["version"]}"' in lock
    assert '"uv", "run", "--isolated", "--python", runtime, "--locked"' in QUALITY_RUNNER
    assert "python -m build" in RELEASE_WORKFLOW
    assert "python -m venv /tmp/iicp-release-smoke" in RELEASE_WORKFLOW
    assert "pip install dist/*.whl" in RELEASE_WORKFLOW
