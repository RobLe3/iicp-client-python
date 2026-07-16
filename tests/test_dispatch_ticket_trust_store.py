from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from iicp_client.dispatch_ticket_trust import (
    AdminRecoveryAuthorization,
    FileTrustBundleStore,
    TrustBundle,
    TrustBundleStoreCorrupt,
    TrustBundleStoreError,
    TrustBundleStoreLocked,
    canonical_trust_bundle,
)


def _fixture() -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "parity" / "dispatch-ticket-trust-store-v1.json").read_text()
    )


def _bundle(name: str) -> TrustBundle:
    return TrustBundle.from_dict(_fixture()["bundles"][name])


def test_canonical_bundle_digests_match_shared_fixture() -> None:
    import hashlib

    fixture = _fixture()
    for name, expected in fixture["canonical_digests"].items():
        canonical = canonical_trust_bundle(_bundle(name))
        assert f"sha256:{hashlib.sha256(canonical).hexdigest()}" == expected


def test_shared_store_sequence_and_explicit_recovery(tmp_path: Path) -> None:
    path = tmp_path / "trust" / "bundle.state"
    store = FileTrustBundleStore(path)

    initial = store.install(_bundle("v1"))
    assert (initial.status, initial.state.high_water) == ("installed", 1)
    restarted = FileTrustBundleStore(path).load()
    assert restarted is not None
    assert (restarted.bundle.bundle_version, restarted.high_water) == (1, 1)
    assert store.install(_bundle("v1")).status == "unchanged"
    assert store.install(_bundle("v1_conflict")).status == "conflict"
    assert store.install(_bundle("v2"), expected_current_version=1).status == "installed"
    assert store.install(_bundle("v1")).status == "stale"
    assert store.install(_bundle("v2"), expected_current_version=1).status == "conflict"
    assert store.recover(_bundle("v1"), None).status == "recovery_required"

    recovered = store.recover(
        _bundle("v1"),
        AdminRecoveryAuthorization("operator-approved-test-recovery", minimum_high_water=2),
    )
    assert recovered.status == "recovered"
    assert recovered.state is not None
    assert (recovered.state.bundle.bundle_version, recovered.state.high_water) == (1, 2)
    assert store.install(_bundle("v1")).status == "stale"


def test_corruption_permissions_and_orphan_temp_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "trust" / "bundle.state"
    store = FileTrustBundleStore(path)
    store.install(_bundle("v1"))
    (path.parent / "bundle.state.tmp-interrupted").write_text("partial")
    assert store.load() is not None

    path.write_text("{not-json", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(TrustBundleStoreCorrupt):
        store.load()

    recovered = store.recover(
        _bundle("v1"), AdminRecoveryAuthorization("repair-corrupt-test", 1)
    )
    assert recovered.status == "recovered"
    os.chmod(path, 0o644)
    with pytest.raises(TrustBundleStoreCorrupt):
        store.load()


def test_concurrent_writers_never_finish_below_highest_version(tmp_path: Path) -> None:
    path = tmp_path / "trust" / "bundle.state"
    store = FileTrustBundleStore(path)
    store.install(_bundle("v1"))
    v2 = _bundle("v2")
    v3 = TrustBundle.from_dict({"bundle_version": 3, "issuer": "did:web:directory.example", "keys": []})
    barrier = threading.Barrier(3)
    statuses: list[str] = []

    def install(bundle: TrustBundle) -> None:
        barrier.wait()
        statuses.append(FileTrustBundleStore(path).install(bundle).status)

    threads = [threading.Thread(target=install, args=(bundle,)) for bundle in (v2, v3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    state = store.load()
    assert state is not None
    assert state.bundle.bundle_version == state.high_water == 3
    assert set(statuses) <= {"installed", "stale"}


def test_held_lock_times_out_without_mutating_state(tmp_path: Path) -> None:
    path = tmp_path / "trust" / "bundle.state"
    store = FileTrustBundleStore(path, lock_timeout_s=0)
    path.parent.mkdir(mode=0o700)
    store.lock_path.write_text("held", encoding="utf-8")
    os.chmod(store.lock_path, 0o600)

    with pytest.raises(TrustBundleStoreLocked):
        store.install(_bundle("v1"))
    assert not path.exists()


def test_symlink_and_invalid_versions_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o600)
    path = tmp_path / "bundle.state"
    path.symlink_to(target)
    with pytest.raises(TrustBundleStoreCorrupt):
        FileTrustBundleStore(path).load()

    invalid = TrustBundle.from_dict({"bundle_version": -1, "keys": []})
    with pytest.raises(TrustBundleStoreError, match="non-negative"):
        FileTrustBundleStore(tmp_path / "trust" / "bundle.state").install(invalid)
