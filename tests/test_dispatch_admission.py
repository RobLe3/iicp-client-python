from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
from pathlib import Path

import pytest

from iicp_client.dispatch_admission import (
    DispatchAdmissionClaim,
    DispatchAdmissionStorageError,
    SqliteDispatchAdmissionStore,
    evaluate_dispatch_admission,
)


def fixture() -> dict:
    return json.loads((Path(__file__).parents[1] / "parity" / "dispatch-admission-v2.json").read_text())


def claim(raw: dict) -> DispatchAdmissionClaim:
    return DispatchAdmissionClaim(**raw)


def run_case(store: SqliteDispatchAdmissionStore, case: dict) -> str:
    request = claim(case["claim"])
    for prior in case.get("prior", []):
        assert evaluate_dispatch_admission(
            store,
            request,
            expected_provider_id=request.provider_id,
            expected_intent=request.intent,
            now=prior.get("now", case["now"]),
            trust_verified=True,
        ).accepted
        if prior.get("terminal_state"):
            store.transition(request.jti, prior["terminal_state"], now=case["now"])
    if case.get("reopen"):
        store = SqliteDispatchAdmissionStore(store.path)
    return evaluate_dispatch_admission(
        store,
        request,
        expected_provider_id=case["expected_provider_id"],
        expected_intent=case["expected_intent"],
        now=case["now"],
        trust_verified=case.get("trust_verified", True),
        clock_skew_s=fixture()["defaults"]["clock_skew_s"],
    ).code


def test_shared_admission_fixture(tmp_path: Path) -> None:
    for case in fixture()["cases"]:
        store = SqliteDispatchAdmissionStore(tmp_path / f"{case['id']}.sqlite3")
        assert run_case(store, case) == case["expected"]


def _consume_process(path: str, raw_claim: dict, start: multiprocessing.synchronize.Event, queue) -> None:
    store = SqliteDispatchAdmissionStore(path)
    start.wait()
    decision = evaluate_dispatch_admission(
        store,
        claim(raw_claim),
        expected_provider_id=raw_claim["provider_id"],
        expected_intent=raw_claim["intent"],
        now=1_700_000_000,
        trust_verified=True,
    )
    queue.put(decision.code)


def test_two_process_consume_has_one_durable_winner(tmp_path: Path) -> None:
    raw = fixture()["cases"][0]["claim"]
    path = str(tmp_path / "multiprocess.sqlite3")
    SqliteDispatchAdmissionStore(path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    workers = [context.Process(target=_consume_process, args=(path, raw, start, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    results = [queue.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert sorted(results) == ["accepted", "reject_replay"]


def test_crash_boundaries_and_bounded_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "crash.sqlite3"
    store = SqliteDispatchAdmissionStore(path)
    raw = fixture()["cases"][0]["claim"]

    # A process dying before commit leaves no consumable state.
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO dispatch_admissions VALUES(?,?,?,?,?,?,?)",
        (raw["jti"], "sha256:provider", "sha256:intent", "accepted", raw["expires_at"], 1, 1),
    )
    db.rollback()
    db.close()
    assert run_case(store, fixture()["cases"][0]) == "accepted"
    record = store.lookup(raw["jti"])
    assert record is not None
    assert record.provider_digest == "sha256:" + hashlib.sha256(raw["provider_id"].encode()).hexdigest()
    assert record.intent_digest == "sha256:" + hashlib.sha256(raw["intent"].encode()).hexdigest()
    assert raw["provider_id"] not in path.read_bytes().decode("latin1")
    assert raw["intent"] not in path.read_bytes().decode("latin1")

    # A process dying after commit cannot make the ticket reusable.
    assert run_case(SqliteDispatchAdmissionStore(path), fixture()["cases"][1]) == "reject_replay"
    for index in range(2):
        item = {**raw, "jti": f"cleanup-ticket-{index:02d}", "not_before": 0, "expires_at": 10}
        assert evaluate_dispatch_admission(
            store,
            claim(item),
            expected_provider_id=item["provider_id"],
            expected_intent=item["intent"],
            now=1,
            trust_verified=True,
        ).accepted
    assert store.cleanup(now=200, retention_s=100, limit=1) == 1
    assert store.cleanup(now=200, retention_s=100, limit=1) == 1
    assert os.stat(path).st_mode & 0o077 == 0


def test_locked_and_corrupt_store_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "locked.sqlite3"
    store = SqliteDispatchAdmissionStore(path, busy_timeout_s=0)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("BEGIN EXCLUSIVE")
    decision = evaluate_dispatch_admission(
        store,
        claim(fixture()["cases"][0]["claim"]),
        expected_provider_id="provider-a",
        expected_intent="urn:iicp:intent:llm:chat:v1",
        now=1_700_000_000,
        trust_verified=True,
    )
    db.rollback()
    db.close()
    assert decision.code == "reject_store_unavailable"

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(DispatchAdmissionStorageError):
        SqliteDispatchAdmissionStore(corrupt)
