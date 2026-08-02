# SDK quality evidence

The release-quality lane is local-first. It exercises CPython 3.11, 3.12 and
3.13, verifies the checked-in dependency lock, runs Ruff and the complete test
suite, enforces a measured 75% coverage floor, audits installed dependencies,
builds the wheel and tests a clean installation.

Mypy currently reports 74 findings in 17 legacy files under the release's
Python 3.11 environment. The checked-in baseline
groups those findings by file and error code. The gate permits reductions but
rejects a new category or a higher count. This is a debt ratchet, not a claim
that the full source tree is type-clean. Reduce the baseline only alongside a
reviewed code correction; never regenerate it merely to pass a release.

Run:

```bash
python3 scripts/run_sdk_quality.py --output build/python.json
```

The result implements `iicp.sdk-quality-evidence.v1` and contains no commands,
paths, test output, credentials or application data. It is a release-candidate
input, not publication permission.
