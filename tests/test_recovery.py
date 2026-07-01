from iicp_client.recovery import (
    DirectoryPresence,
    RecoveryAction,
    RecoveryState,
    classify,
    node_registry_prefix,
)


def test_uuid_nodes_use_public_eight_char_prefix():
    assert node_registry_prefix("b30aee67-9089-4337-806e-b560428cf97a") == "b30aee67"
    assert node_registry_prefix("relay-eu-e50fc7f9") == "relay-eu-e50fc7f9"


def test_recovery_classification_reregisters_before_restart():
    assert classify(
        local_health_ok=True,
        public_available=True,
        directory_presence=DirectoryPresence.ABSENT,
        consecutive_failures=1,
        grace_checks=3,
    ) == (RecoveryState.DIRECTORY_ABSENT, RecoveryAction.REREGISTER)
    assert classify(
        local_health_ok=True,
        public_available=True,
        directory_presence=DirectoryPresence.ABSENT,
        consecutive_failures=3,
        grace_checks=3,
    ) == (RecoveryState.ROUTE_MISMATCH, RecoveryAction.RESTART_SELF)


def test_unavailable_public_route_waits_then_restarts():
    assert classify(
        local_health_ok=True,
        public_available=False,
        directory_presence=DirectoryPresence.ABSENT,
        consecutive_failures=1,
        grace_checks=3,
    ) == (RecoveryState.LIMITED_REACH, RecoveryAction.WAIT_COOLDOWN)
    assert classify(
        local_health_ok=True,
        public_available=False,
        directory_presence=DirectoryPresence.ABSENT,
        consecutive_failures=3,
        grace_checks=3,
    ) == (RecoveryState.RESTART_RECOMMENDED, RecoveryAction.RESTART_SELF)
