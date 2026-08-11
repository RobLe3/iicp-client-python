from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_mypy_baseline import observed_errors  # noqa: E402
from run_sdk_quality import RUNTIMES  # noqa: E402


def test_mypy_baseline_is_content_free_and_internally_consistent() -> None:
    baseline = json.loads((ROOT / "quality/mypy-baseline.json").read_text())
    assert baseline["schema"] == "iicp.python-mypy-baseline.v1"
    assert baseline["error_count"] == sum(baseline["errors"].values())
    assert all(key.startswith("src/iicp_client/") and "|" in key for key in baseline["errors"])


def test_mypy_parser_groups_by_file_and_error_code() -> None:
    output = "src/iicp_client/a.py:7: error: Bad value  [arg-type]\n"
    assert observed_errors(output) == {"src/iicp_client/a.py|arg-type": 1}


def test_quality_runner_uses_the_shared_content_free_schema() -> None:
    source = (ROOT / "scripts/run_sdk_quality.py").read_text()
    assert '"schema": "iicp.sdk-quality-evidence.v1"' in source
    assert RUNTIMES == ("3.11", "3.12", "3.13")
    assert '"commands"' not in source
    assert '"output"' not in source


def test_pull_request_quality_enforces_mypy_no_regression() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text()
    assert "python scripts/check_mypy_baseline.py" in workflow
