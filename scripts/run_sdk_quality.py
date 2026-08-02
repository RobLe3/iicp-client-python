#!/usr/bin/env python3
"""Run Python SDK release-quality gates and emit content-free evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

RUNTIMES = ("3.11", "3.12", "3.13")
COVERAGE_MINIMUM = 75.0


def run(argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )


def uv(runtime: str, *argv: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(
        ["uv", "run", "--isolated", "--python", runtime, "--locked", "--extra", "dev", *argv],
        capture=capture,
    )


def require(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode:
        raise RuntimeError(f"{label} failed")


def git_value(*argv: str) -> str:
    result = run(["git", *argv], capture=True)
    require(result, "git evidence")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if git_value("status", "--porcelain=v1", "--untracked-files=all"):
        print("SDK quality evidence stopped: worktree is not clean")
        return 2

    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    commit = git_value("rev-parse", "HEAD")
    runtime_results = []
    try:
        require(run(["uv", "lock", "--check"]), "dependency lock")
        for runtime in RUNTIMES:
            require(uv(runtime, "pytest", "-q"), f"Python {runtime}")
            runtime_results.append({"name": runtime, "status": "pass"})
        require(uv("3.11", "ruff", "check", "src", "tests", "scripts"), "Ruff")
        require(uv("3.11", "python", "scripts/check_mypy_baseline.py"), "mypy baseline")
        with tempfile.TemporaryDirectory(prefix="iicp-python-quality-") as temporary:
            temp = Path(temporary)
            coverage_json = temp / "coverage.json"
            require(
                uv(
                    "3.12",
                    "pytest",
                    "-q",
                    "--cov=iicp_client",
                    f"--cov-fail-under={COVERAGE_MINIMUM}",
                    f"--cov-report=json:{coverage_json}",
                ),
                "coverage",
            )
            coverage = json.loads(coverage_json.read_text(encoding="utf-8"))["totals"]["percent_covered"]
            require(uv("3.12", "pip-audit"), "dependency audit")
            dist = temp / "dist"
            require(uv("3.12", "python", "-m", "build", "--outdir", str(dist)), "locked build")
            wheel = next(dist.glob("*.whl"))
            venv = temp / "clean-install"
            require(run(["uv", "venv", "--python", "3.12", str(venv)]), "clean venv")
            python = venv / "bin" / "python"
            require(run(["uv", "pip", "install", "--python", str(python), str(wheel)]), "clean install")
            require(run([str(python), "-c", "import iicp_client; print(iicp_client.__version__)"]), "import smoke")
    except (RuntimeError, StopIteration, OSError, json.JSONDecodeError) as error:
        print(f"SDK quality evidence failed: {error}")
        return 1

    evidence = {
        "schema": "iicp.sdk-quality-evidence.v1",
        "sdk": "python",
        "version": version,
        "commit": commit,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "pass",
        "runtimes": runtime_results,
        "gates": {
            "static_analysis": {"status": "pass"},
            "coverage": {"status": "pass", "percent": round(coverage, 3), "minimum_percent": COVERAGE_MINIMUM},
            "dependency_audit": {"status": "pass"},
            "locked_build": {"status": "pass"},
            "clean_install": {"status": "pass"},
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"Python SDK quality evidence passed for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
