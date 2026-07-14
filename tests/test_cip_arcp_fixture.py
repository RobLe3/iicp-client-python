"""Cross-SDK consumer for the pre-normative CIP/ARCP research fixtures."""
from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _cip(value):
    replicas = value.get("replicas")
    if not isinstance(replicas, int) or isinstance(replicas, bool) or not 1 <= replicas <= 10:
        return {"envelope": "reject", "execution": "reject", "error": "IICP-E028"}
    quorum = value.get("quorum")
    if quorum is not None and (not isinstance(quorum, int) or isinstance(quorum, bool) or quorum < 1 or quorum > replicas):
        return {"envelope": "reject", "execution": "reject", "error": "IICP-E028"}
    out = {"envelope": "accept"}
    if value.get("sensitivity") == "high" and not value.get("send_sensitive_prompts", False):
        return {**out, "execution": "local", "remote_eligible": False}
    if str(value.get("intent", "")).startswith(("urn:iicp:intent:mcp:", "urn:iicp:intent:tool:")):
        return {**out, "execution": "reject", "remote_eligible": False}
    policy, operator_max = value.get("policy"), min(10, max(1, value.get("operator_max_replicas", 10)))
    if policy is None:
        return {**out, "execution": "accept", "quorum": None} if replicas == 1 else {**out, "execution": "reject", "error": "IICP-E028"}
    if policy == "best_of_n":
        return {**out, "execution": "accept", "quorum": None} if 2 <= replicas <= operator_max else {**out, "execution": "reject", "error": "IICP-E028"}
    if policy == "majority_vote":
        if replicas < 3 or replicas % 2 == 0:
            return {**out, "execution": "reject", "error": "IICP-E025"}
        if replicas > operator_max:
            return {**out, "execution": "reject", "error": "IICP-E028"}
        return {**out, "execution": "accept", "quorum": quorum if quorum is not None else replicas // 2 + 1}
    if policy == "map_reduce" and "map_reduce" not in value.get("implemented_modes", []):
        return {**out, "execution": "unsupported", "advertise": False}
    return {**out, "execution": "reject", "error": "IICP-E028"}


def _schema(value, schema):
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if schema.get("type") in kinds and (not isinstance(value, kinds[schema["type"]]) or schema["type"] in {"integer", "number"} and isinstance(value, bool)):
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        return not any(k not in value for k in schema.get("required", [])) and not (schema.get("additionalProperties") is False and any(k not in props for k in value)) and all(k not in value or _schema(value[k], v) for k, v in props.items())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value >= schema.get("minimum", float("-inf")) and value <= schema.get("maximum", float("inf"))
    return True


def _evaluate(case):
    kind, candidate = case["evaluator"], case.get("candidate")
    if kind == "exact_match":
        passed = unicodedata.normalize("NFC", str(candidate)).strip() == unicodedata.normalize("NFC", str(case["expected_value"])).strip()
        return {"passed": passed, "score": float(passed)}
    if kind == "numeric_tolerance":
        actual, expected = Decimal(str(candidate)), Decimal(str(case["expected_value"]))
        passed = abs(actual - expected) <= max(Decimal(case.get("absolute_tolerance", "0")), abs(expected) * Decimal(case.get("relative_tolerance", "0")))
        return {"passed": passed, "score": float(passed)}
    if kind == "json_schema_subset":
        passed = _schema(candidate, case["schema"]); return {"passed": passed, "score": float(passed)}
    if kind == "constraints":
        checks = []
        for c in case["constraints"]:
            actual = candidate.get(c["path"])
            checks.append(actual == c["value"] if c["op"] == "equals" else actual in c["value"] if c["op"] == "in" else len(actual) >= c["value"] if c["op"] == "min_items" else len(actual) <= c["value"])
        passed = bool(checks) and all(checks); return {"passed": passed, "score": float(passed)}
    if kind == "unit_test_summary":
        total = candidate["passed"] + candidate["failed"]
        return {"passed": total > 0 and candidate["failed"] == 0 and bool(candidate["suite_digest"]), "score": round(candidate["passed"] / total if total else 0, 6)}
    raise AssertionError(kind)


def test_cip_conformance_fixture():
    fixture = json.loads((ROOT / "parity/cip-conformance-v0.json").read_text())
    assert all(_cip(case["input"]) == case["expected"] for case in fixture["cases"])
    vector = fixture["canonical_receipt_vectors"][0]
    assert hashlib.sha256(vector["canonical_result_json"].encode()).hexdigest() == vector["response_hash"]
    assert hmac.new(vector["hmac_key_utf8"].encode(), vector["canonical_message"].encode(), hashlib.sha256).hexdigest() == vector["signature_hmac_sha256"]


def test_arcp_evaluator_fixture():
    fixture = json.loads((ROOT / "parity/arcp-evaluator-v0.json").read_text())
    for case in fixture["cases"]:
        assert _evaluate(case) == case["expected"], case["name"]
