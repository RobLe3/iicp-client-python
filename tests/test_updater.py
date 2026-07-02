# ADR-016: IICP client SDK conformance — #521 self-updater P1 (read-only check)
"""Behavior tests for the version-check logic + the `update` CLI command.
Network is monkeypatched — no real PyPI call."""

from __future__ import annotations

import pytest

from iicp_client import updater
from iicp_client.cli import _build_parser, main
from iicp_client.updater import (
    auto_update_enabled,
    auto_update_initial_delay_s,
    auto_update_interval_s,
    auto_update_tick,
)


class TestVersionCompare:
    @pytest.mark.parametrize(
        ("cur", "latest", "expected"),
        [
            ("0.7.56", "0.7.57", True),
            ("0.7.57", "0.7.57", False),
            ("0.7.57", "0.7.56", False),
            ("0.7.9", "0.7.10", True),  # numeric, not lexicographic
            ("1.0.0", "0.9.9", False),
            ("v0.7.56", "0.7.57", True),  # leading v tolerated
        ],
    )
    def test_is_outdated(self, cur, latest, expected):
        assert updater.is_outdated(cur, latest) is expected

    def test_parse_version_truncates_prerelease(self):
        assert updater.parse_version("1.2.3rc1") == (1, 2, 3)
        assert updater.parse_version("0.7.57") == (0, 7, 57)


class TestCheckUpdate:
    def test_outdated_verdict(self):
        v = updater.check_update("0.7.56", "0.7.57")
        assert v["outdated"] is True
        assert v["command"] == "pip install -U iicp-client"

    def test_unknown_latest_is_not_outdated(self):
        v = updater.check_update("0.7.57", None)
        assert v["outdated"] is False


class TestUpdateCli:
    def test_subcommand_parses(self):
        ns = _build_parser().parse_args(["update", "--check"])
        assert ns.cmd == "update"
        assert ns.check is True

    # _cmd_update does `from iicp_client.updater import latest_pypi_version`
    # at call time, so patch the source module (the binding it imports from).
    def test_exit_10_when_outdated(self, monkeypatch, capsys):
        monkeypatch.setattr(updater, "latest_pypi_version", lambda *a, **k: "99.0.0")
        code = main(["update", "--check"])
        assert code == 10
        assert "newer release is available" in capsys.readouterr().out

    def test_exit_0_when_current(self, monkeypatch, capsys):
        from iicp_client import __version__
        monkeypatch.setattr(updater, "latest_pypi_version", lambda *a, **k: __version__)
        code = main(["update", "--check"])
        assert code == 0
        assert "up to date" in capsys.readouterr().out

    def test_exit_0_when_registry_unreachable(self, monkeypatch, capsys):
        monkeypatch.setattr(updater, "latest_pypi_version", lambda *a, **k: None)
        code = main(["update", "--check"])
        assert code == 0
        assert "could not reach PyPI" in capsys.readouterr().out


# ── P2 auto-updater (#521) ──────────────────────────────────────────────────────


class TestPerformSelfUpdate:
    """pipx app-venvs ship without pip; the updater must bootstrap it via
    ensurepip instead of silently failing every tick."""

    def test_ensure_pip_is_noop_when_pip_present(self, monkeypatch):
        monkeypatch.setattr(updater, "_pip_available", lambda: True)
        monkeypatch.setattr(
            updater.subprocess, "run",
            lambda *a, **k: pytest.fail("must not shell out when pip is present"),
        )
        assert updater._ensure_pip() is True

    def test_ensure_pip_bootstraps_with_ensurepip_when_missing(self, monkeypatch):
        states = iter([False, True])  # absent, then present after ensurepip
        monkeypatch.setattr(updater, "_pip_available", lambda: next(states))
        ran = []
        monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **k: ran.append(cmd))
        assert updater._ensure_pip() is True
        assert ran and ran[0][:3] == [updater.sys.executable, "-m", "ensurepip"]

    def test_ensure_pip_false_when_bootstrap_fails(self, monkeypatch):
        monkeypatch.setattr(updater, "_pip_available", lambda: False)

        def boom(cmd, **k):
            raise updater.subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(updater.subprocess, "run", boom)
        assert updater._ensure_pip() is False

    def test_perform_self_update_aborts_when_pip_unavailable(self, monkeypatch):
        monkeypatch.setattr(updater, "_ensure_pip", lambda *a, **k: False)
        monkeypatch.setattr(
            updater.subprocess, "run",
            lambda *a, **k: pytest.fail("must not pip-install without pip"),
        )
        assert updater.perform_self_update() is False

    def test_perform_self_update_installs_after_bootstrap(self, monkeypatch):
        monkeypatch.setattr(updater, "_ensure_pip", lambda *a, **k: True)
        ran = []
        monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **k: ran.append(cmd))
        assert updater.perform_self_update() is True
        assert ran and ran[0][:5] == [
            updater.sys.executable, "-m", "pip", "install", "--upgrade",
        ]


def _spy():
    calls = []
    return calls, (lambda *a: calls.append(a))


def test_auto_update_tick_upgrades_and_reexecs_when_newer():
    logs, log_fn = _spy()
    reexec_calls = []
    result = auto_update_tick(
        "0.7.59", "0.7.60", True,
        upgrade_fn=lambda: True,
        reexec_fn=lambda: reexec_calls.append(1),
        log_fn=log_fn,
    )
    assert result == "upgraded"
    assert reexec_calls == [1]  # re-exec attempted exactly once


def test_auto_update_tick_noop_when_current():
    result = auto_update_tick(
        "0.7.60", "0.7.60", True,
        upgrade_fn=lambda: (_ for _ in ()).throw(AssertionError("must not upgrade")),
        reexec_fn=lambda: (_ for _ in ()).throw(AssertionError("must not reexec")),
        log_fn=lambda *a: None,
    )
    assert result == "current"


def test_auto_update_tick_disabled_is_noop():
    assert auto_update_tick("0.7.59", "0.7.60", False, lambda: True, lambda: None, lambda *a: None) == "disabled"


def test_auto_update_tick_unknown_latest_is_noop():
    assert auto_update_tick("0.7.59", None, True, lambda: True, lambda: None, lambda *a: None) == "unknown"


def test_auto_update_tick_failed_upgrade_does_not_reexec():
    reexec_calls = []
    result = auto_update_tick(
        "0.7.59", "0.7.60", True,
        upgrade_fn=lambda: False,
        reexec_fn=lambda: reexec_calls.append(1),
        log_fn=lambda *a: None,
    )
    assert result == "upgrade-failed"
    assert reexec_calls == []  # no restart on a failed upgrade


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(300, 300), (900, 300), (21600, 300)],
)
def test_auto_update_initial_delay_is_at_most_five_minutes(interval, expected):
    assert auto_update_initial_delay_s(interval) == expected


def test_auto_update_enabled_env_opt_out(monkeypatch):
    monkeypatch.delenv("IICP_AUTO_UPDATE", raising=False)
    assert auto_update_enabled() is True
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("IICP_AUTO_UPDATE", value)
        assert auto_update_enabled() is False
    monkeypatch.setenv("IICP_AUTO_UPDATE", "1")
    assert auto_update_enabled() is True


def test_auto_update_interval_env_floor_and_bad_value(monkeypatch):
    monkeypatch.delenv("IICP_AUTO_UPDATE_INTERVAL_S", raising=False)
    assert auto_update_interval_s() == 3600
    monkeypatch.setenv("IICP_AUTO_UPDATE_INTERVAL_S", "42")
    assert auto_update_interval_s() == 300
    monkeypatch.setenv("IICP_AUTO_UPDATE_INTERVAL_S", "900")
    assert auto_update_interval_s() == 900
    monkeypatch.setenv("IICP_AUTO_UPDATE_INTERVAL_S", "not-a-number")
    assert auto_update_interval_s() == 3600


def test_auto_update_status_payload_defaults_hourly(monkeypatch):
    monkeypatch.delenv("IICP_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("IICP_AUTO_UPDATE_INTERVAL_S", raising=False)
    updater.record_update_check("0.7.69")

    payload = updater.auto_update_status_payload()

    assert payload["auto_update_enabled"] is True
    assert payload["auto_update_interval_s"] == 3600
    assert payload["sdk_latest_seen"] == "0.7.69"
    assert payload["sdk_update_last_checked_at"]
    assert payload["sdk_update_error_class"] is None


def test_start_auto_update_loop_runs_without_blocking(monkeypatch):
    monkeypatch.delenv("IICP_AUTO_UPDATE", raising=False)
    stop = updater.start_auto_update_loop(
        "0.7.66",
        latest_fn=lambda: "0.7.66",
        upgrade_fn=lambda: (_ for _ in ()).throw(AssertionError("must not upgrade")),
        reexec_fn=lambda: (_ for _ in ()).throw(AssertionError("must not reexec")),
        log_fn=lambda *_: None,
    )
    assert stop is not None
    stop.set()


def test_provider_serve_uses_shared_auto_update_starter(monkeypatch):
    import iicp_client.cli as cli
    from iicp_client import __version__

    calls = []
    sentinel = object()
    monkeypatch.setattr(
        updater,
        "start_auto_update_loop",
        lambda current, **kw: calls.append((current, kw)) or sentinel,
    )

    assert cli._start_provider_auto_update() is sentinel
    assert calls and calls[0][0] == __version__
