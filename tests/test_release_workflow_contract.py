from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manual_release_uses_metadata_compatible_isolated_toolchain() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'IICP_PYTHON_BUILD_VERSION: "1.5.0"' in workflow
    assert 'IICP_TWINE_VERSION: "6.2.0"' in workflow
    assert 'IICP_HATCHLING_VERSION: "1.31.0"' in workflow
    assert 'IICP_UV_VERSION: "0.11.6"' in workflow
    assert 'uv run --locked --extra dev ruff check src tests scripts' in workflow
    assert 'uv run --locked --extra dev pytest -q' in workflow
    assert 'python -m pip install -e ".[dev]" ||' not in workflow
    assert 'PIP_CONSTRAINT="$constraints" python -m build' in workflow
    assert workflow.index('PIP_CONSTRAINT="$constraints" python -m build') < workflow.index("twine check dist/*")
