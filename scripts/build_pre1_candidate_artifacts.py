#!/usr/bin/env python3
"""Build and prove the Python pre-stable wheel/sdist artifact fragment."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import pre1_artifact_common as common

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "client-python"
TARGETS = {
    "linux-x86_64",
    "linux-aarch64",
    "macos-x86_64",
    "macos-arm64",
    "windows-x86_64",
}


def describe() -> dict:
    return {
        "schema": "iicp.pre1-artifact-builder-description.v1",
        "component": COMPONENT,
        "targets": sorted(TARGETS),
        "artifact_identities": [["wheel", "any"], ["sdist", "any"]],
        "gates": sorted(common.GATES),
        "requires_clean_source": True,
        "non_authorizing": True,
    }


def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def venv_cli(root: Path) -> Path:
    return root / ("Scripts/iicp-node.exe" if sys.platform == "win32" else "bin/iicp-node")


def build(destination: Path, requested_target: str | None) -> dict:
    common.safe_output(destination)
    target = common.require_target(requested_target, TARGETS)
    commit = common.require_clean_source(ROOT)
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = manifest["project"]["version"]
    requires = manifest["project"]["requires-python"]
    if requires != ">=3.11,<3.15":
        raise ValueError("Python package support boundary differs from the qualification policy")
    run_root = Path(tempfile.mkdtemp(prefix="iicp-pre1-python-", dir=destination.parent))
    staging = run_root / "fragment"
    staging.mkdir()
    try:
        common.run(["uv", "sync", "--locked", "--extra", "dev"], ROOT)
        common.run(["uv", "run", "--locked", "--extra", "dev", "pytest", "-q"], ROOT)
        dist = run_root / "dist"
        dist.mkdir()
        common.run(
            [
                "uv",
                "run",
                "--locked",
                "--extra",
                "dev",
                "python",
                "-m",
                "build",
                "--outdir",
                str(dist),
            ],
            ROOT,
        )
        wheels = list(dist.glob("*.whl"))
        sdists = list(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Python build did not produce exactly one wheel and one sdist")
        wheel, sdist = wheels[0], sdists[0]

        requirements = run_root / "requirements.txt"
        common.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            ROOT,
        )
        wheelhouse = run_root / "wheelhouse"
        wheelhouse.mkdir()
        common.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--dest",
                str(wheelhouse),
                "--requirement",
                str(requirements),
            ],
            ROOT,
        )

        online = run_root / "online"
        common.run([sys.executable, "-m", "venv", str(online)], ROOT)
        common.run(
            [
                str(venv_python(online)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "--requirement",
                str(requirements),
            ],
            ROOT,
        )
        common.run(
            [
                str(venv_python(online)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                str(wheel),
            ],
            ROOT,
        )
        online_version = common.output([str(venv_cli(online)), "--version"], ROOT)
        if version not in online_version:
            raise ValueError("online Python package self-report differs")

        offline = run_root / "offline"
        common.run([sys.executable, "-m", "venv", str(offline)], ROOT)
        common.run(
            [
                str(venv_python(offline)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--require-hashes",
                "--requirement",
                str(requirements),
            ],
            ROOT,
        )
        common.run(
            [
                str(venv_python(offline)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                str(wheel),
            ],
            ROOT,
        )
        offline_version = common.output([str(venv_cli(offline)), "--version"], ROOT)
        if offline_version != online_version or version not in offline_version:
            raise ValueError("offline Python package self-report differs")

        copied_wheel = staging / wheel.name
        copied_sdist = staging / sdist.name
        shutil.copyfile(wheel, copied_wheel)
        shutil.copyfile(sdist, copied_sdist)
        fragment = common.emit_fragment(
            staging,
            component=COMPONENT,
            source_commit=commit,
            source_version=version,
            build_target=target,
            artifacts=[
                common.artifact("wheel", "any", copied_wheel),
                common.artifact("sdist", "any", copied_sdist),
            ],
            lock_inputs_sha256=common.files_sha256(ROOT, [ROOT / "pyproject.toml", ROOT / "uv.lock"]),
            dependency_cache_sha256=common.tree_sha256(wheelhouse),
            toolchains={
                "python": common.output([sys.executable, "--version"], ROOT),
                "uv": common.output(["uv", "--version"], ROOT),
            },
        )
        common.publish_staging(staging, destination)
        return fragment
    finally:
        common.clean_failed_staging(run_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target")
    args = parser.parse_args()
    if args.describe:
        print(json.dumps(describe(), indent=2, sort_keys=True))
        return 0
    if args.output is None:
        parser.error("--output is required unless --describe is used")
    try:
        value = build(args.output.resolve(), args.target)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
