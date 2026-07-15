"""Portable consumer for CIP attribution, counter, time, and tie fixtures."""
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def attribution(v):
    q = v.get("querying_node_id")
    if not q:
        return {"action": "award", "attribution": "legacy_unattributed", "trust_weight": 0.0}
    if q == v.get("serving_node_id"):
        return {"action": "exclude", "attribution": "self_node", "trust_weight": 0.0}
    if not v.get("querying_exists", False):
        return {"action": "reject", "attribution": "unknown_querying_node", "trust_weight": 0.0, "error": "IICP-E027"}
    serving, querying = v.get("serving_operator"), v.get("querying_operator")
    if serving and querying and serving == querying:
        return {"action": "exclude", "attribution": "self_operator", "trust_weight": 0.0}
    if serving and querying:
        return {"action": "award", "attribution": "attributed_cross_operator", "trust_weight": 1.0}
    return {"action": "award", "attribution": "attributed_cross_node_unverified_operator", "trust_weight": 0.5}


def receipt_time(v):
    def parse(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    try:
        completed, observed, expires = parse(v["completed_at"]), parse(v["observed_at"]), parse(v["expires_at"])
    except (KeyError, TypeError, ValueError):
        return {"action": "reject", "error": "IICP-E027"}
    return {"action": "reject", "error": "IICP-E027"} if expires > completed + timedelta(seconds=300) or observed > expires else {"action": "accept"}


def test_cip_economic_fixture():
    data = json.loads((ROOT / "parity/cip-economic-attribution-v0.json").read_text())
    for case in data["attribution_cases"]:
        assert attribution(case["input"]) == case["expected"], case["name"]
    for case in data["heartbeat_cases"]:
        v = case["input"]
        counted = min(max(0, int(v["tasks_success"])), 300)
        failed = max(0, int(v["tasks_failed"]))
        assert {"counted_success": counted, "completed_increment": counted, "lifetime_jobs_increment": counted + failed} == case["expected"], case["name"]
    for case in data["receipt_time_cases"]:
        assert receipt_time(case["input"]) == case["expected"], case["name"]
    for case in data["selection_tie_cases"]:
        eligible = [n for n in case["input"]["nodes"] if n["eligible"]]
        selected = min(eligible, key=lambda n: (-n["score"], n["node_id"]), default=None)
        assert {"selected_node_id": selected["node_id"] if selected else None} == case["expected"], case["name"]
