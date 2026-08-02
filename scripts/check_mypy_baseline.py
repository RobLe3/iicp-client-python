#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
from pathlib import Path

ERROR = re.compile(r"^([^:]+):\d+: error: .*\[([^]]+)\]$")


def observed_errors(output: str) -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for line in output.splitlines():
        match = ERROR.match(line)
        if match:
            counts[f"{match.group(1)}|{match.group(2)}"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=Path("quality/mypy-baseline.json"))
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    result = subprocess.run(
        ["mypy", "src"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
    )
    observed = observed_errors(result.stdout)
    allowed = collections.Counter(baseline["errors"])
    regressions = {key: count for key, count in observed.items() if count > allowed.get(key, 0)}
    if regressions:
        print(f"mypy baseline failed: {len(regressions)} category increase(s)")
        return 1
    print(f"mypy baseline passed: {sum(observed.values())}/{baseline['error_count']} findings remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
