from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.accessories.constants import WIIM_REMOTE_2_MIC_DEVICE
from jasper.accessories import reconcile
from jasper.music_sources import Source

ROOT = Path(__file__).resolve().parents[1]


def _variant(value):
    return SimpleNamespace(value=value)


def _parked_systemctl(calls, *, voice_active: bool = True):
    """Return a fake whose adapter terminal state is disabled + inactive."""

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[:2] == ("is-active", "--quiet"):
            return SimpleNamespace(returncode=0 if voice_active else 3)
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=disabled\nActiveState=inactive\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_systemctl


def _active_systemctl(calls):
    """Return a fake whose adapter terminal state is enabled + active."""

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=enabled\nActiveState=active\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_systemctl


def _bluez_device(
    *,
    name: str = "WiiM Remote 2",
    paired: bool = True,
) -> dict[str, dict[str, object]]:
    return {
        "org.bluez.Device1": {
            "Alias": _variant(name),
            "Name": _variant(name),
            "Paired": _variant(paired),
        }
    }


def test_paired_wiim_remote_activates_manual_mic_source():
    plan = reconcile.plan_from_bluez_objects({
        "/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device(),
    })

    assert dict(plan.sources) == {"wiim_remote_2": WIIM_REMOTE_2_MIC_DEVICE}
    assert plan.adapter_services == ("jasper-wiim-remote-mic.service",)
    assert plan.active_profiles == ("wiim_remote_2",)


def test_unpaired_wiim_remote_scan_result_does_not_activate_pipeline():
    plan = reconcile.plan_from_bluez_objects({
        "/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device(paired=False),
    })

    assert dict(plan.sources) == {}
    assert plan.adapter_services == ()
    assert plan.active_profiles == ()


def test_unknown_paired_hid_does_not_activate_pipeline():
    plan = reconcile.plan_from_bluez_objects({
        "/org/bluez/hci0/dev_00_11_22_33_44_55": _bluez_device(
            name="Some Other Remote",
            paired=True,
        ),
    })

    assert dict(plan.sources) == {}
    assert plan.adapter_services == ()
    assert plan.active_profiles == ()


def test_write_manual_mic_env_publishes_and_removes_file(tmp_path: Path):
    path = tmp_path / "accessory-mics.env"

    changed = reconcile.write_manual_mic_env(
        {"wiim_remote_2": WIIM_REMOTE_2_MIC_DEVICE},
        path=str(path),
    )
    assert changed is True
    assert path.read_text() == (
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n"
    )
    assert oct(path.stat().st_mode & 0o777) == "0o644"

    assert reconcile.write_manual_mic_env(
        {"wiim_remote_2": WIIM_REMOTE_2_MIC_DEVICE},
        path=str(path),
    ) is False

    assert reconcile.write_manual_mic_env({}, path=str(path)) is True
    assert not path.exists()
    assert reconcile.write_manual_mic_env({}, path=str(path)) is False


def test_apply_adapter_services_starts_only_active_profile_service():
    calls = []

    fake_systemctl = _active_systemctl(calls)

    reconcile.apply_adapter_services(
        ("jasper-wiim-remote-mic.service",),
        systemctl=fake_systemctl,
    )

    assert ("enable", "jasper-wiim-remote-mic.service") in calls
    assert ("restart", "jasper-wiim-remote-mic.service") in calls
    assert ("disable", "--now", "jasper-wiim-remote-mic.service") not in calls


def test_apply_adapter_services_can_start_active_profile_without_bounce():
    calls = []

    fake_systemctl = _active_systemctl(calls)

    reconcile.apply_adapter_services(
        ("jasper-wiim-remote-mic.service",),
        systemctl=fake_systemctl,
        restart_active=False,
    )

    assert ("enable", "jasper-wiim-remote-mic.service") in calls
    assert ("start", "jasper-wiim-remote-mic.service") in calls
    assert ("restart", "jasper-wiim-remote-mic.service") not in calls


def test_no_change_boot_reconcile_does_not_restart_active_adapter(
    monkeypatch,
    tmp_path: Path,
):
    env_file = tmp_path / "accessory-mics.env"
    env_file.write_text(
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
        encoding="utf-8",
    )
    calls = []

    async def fake_bluez():
        return {"/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device()}

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=enabled\nActiveState=active\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(reconcile, "bluez_managed_objects", fake_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)

    asyncio.run(
        reconcile.reconcile_once(
            env_file=str(env_file),
            systemctl=fake_systemctl,
            reason="boot",
        ),
    )

    assert ("enable", "jasper-wiim-remote-mic.service") in calls
    assert ("start", "jasper-wiim-remote-mic.service") in calls
    assert ("restart", "jasper-wiim-remote-mic.service") not in calls
    assert ("is-active", "--quiet", "jasper-voice.service") not in calls


def test_bluez_discovery_timeout_is_bounded_and_observable(
    monkeypatch,
    tmp_path: Path,
    caplog,
):
    cancelled = []

    async def hanging_bluez():
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(reconcile, "bluez_managed_objects", hanging_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(reconcile, "BLUEZ_DISCOVERY_TIMEOUT_SEC", 0.01)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(
            reconcile.AccessoryReconcileError,
            match="BlueZ accessory discovery timed out after 0.01s",
        ):
            asyncio.run(
                reconcile.reconcile_once(
                    env_file=str(tmp_path / "accessory-mics.env"),
                    systemctl=lambda _args: pytest.fail(
                        "timed-out discovery must not mutate adapter units"
                    ),
                    reason="test",
                ),
            )

    assert cancelled == [True]
    assert "event=accessory_mic.bluez_discovery_failed" in caplog.text
    assert "timeout_sec=0.01" in caplog.text


def test_active_adapter_failure_raises_with_terminal_state_evidence(
    monkeypatch,
    tmp_path: Path,
    caplog,
):
    calls = []

    async def fake_bluez():
        return {"/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device()}

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "enable":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="enable denied",
            )
        if command[0] == "restart":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="start refused",
            )
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=disabled\nActiveState=inactive\n",
                stderr="",
            )
        if command == ("is-active", "--quiet", "jasper-voice.service"):
            return SimpleNamespace(returncode=3, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(reconcile, "bluez_managed_objects", fake_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(
            reconcile.AdapterServiceActivationError,
            match=(
                "enable denied.*start refused.*"
                "expected is-enabled=enabled.*expected is-active=active"
            ),
        ):
            asyncio.run(
                reconcile.reconcile_once(
                    env_file=str(tmp_path / "accessory-mics.env"),
                    systemctl=fake_systemctl,
                    reason="test",
                ),
            )

    assert ("enable", "jasper-wiim-remote-mic.service") in calls
    assert ("restart", "jasper-wiim-remote-mic.service") in calls
    assert "event=accessory_mic.activation_failed" in caplog.text
    assert "enable denied" in caplog.text
    assert "start refused" in caplog.text


def test_bluetooth_intent_off_parks_adapter_without_querying_bluez(
    monkeypatch,
    tmp_path: Path,
):
    env_file = tmp_path / "accessory-mics.env"
    env_file.write_text(
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
        encoding="utf-8",
    )
    calls = []
    intent_reads = []

    async def fail_bluez():
        pytest.fail("Bluetooth Off must not query BlueZ")

    fake_systemctl = _parked_systemctl(calls)

    monkeypatch.setattr(
        reconcile,
        "source_intent_enabled",
        lambda source: intent_reads.append(source) or False,
    )
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fail_bluez)

    plan = asyncio.run(
        reconcile.reconcile_once(
            env_file=str(env_file),
            systemctl=fake_systemctl,
            reason="source-intent",
        ),
    )

    assert dict(plan.sources) == {}
    assert plan.adapter_services == ()
    assert intent_reads == [Source.BLUETOOTH]
    assert not env_file.exists()
    assert (
        "disable", "--now", "jasper-wiim-remote-mic.service",
    ) in calls
    assert ("reset-failed", "jasper-wiim-remote-mic.service") in calls
    assert not any("bluetooth.service" in command for command in calls)
    assert not any(command[0] == "enable" for command in calls)


def test_malformed_bluetooth_intent_parks_adapter_and_fails_loudly(
    monkeypatch,
    tmp_path: Path,
    caplog,
):
    env_file = tmp_path / "accessory-mics.env"
    env_file.write_text(
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
        encoding="utf-8",
    )
    calls = []

    def invalid_intent(_source):
        raise RuntimeError("invalid intent value for bluetooth: maybe")

    async def fail_bluez():
        pytest.fail("malformed intent must fail closed before querying BlueZ")

    fake_systemctl = _parked_systemctl(calls)

    monkeypatch.setattr(reconcile, "source_intent_enabled", invalid_intent)
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fail_bluez)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(
            reconcile.BluetoothSourceIntentError,
            match="invalid intent value for bluetooth",
        ):
            asyncio.run(
                reconcile.reconcile_once(
                    env_file=str(env_file),
                    systemctl=fake_systemctl,
                    reason="source-intent",
                ),
            )

    assert not env_file.exists()
    assert (
        "disable", "--now", "jasper-wiim-remote-mic.service",
    ) in calls
    assert (
        "show",
        "jasper-wiim-remote-mic.service",
        "--property=UnitFileState",
        "--property=ActiveState",
    ) in calls
    assert not any(command[0] == "enable" for command in calls)
    assert "event=accessory_mic.intent_invalid" in caplog.text
    assert "action=parked" in caplog.text


def test_role_park_preserves_enabled_intent_but_disables_adapter(
    monkeypatch,
    tmp_path: Path,
):
    env_file = tmp_path / "accessory-mics.env"
    env_file.write_text(
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
        encoding="utf-8",
    )
    calls = []
    intent_reads = []

    async def fail_bluez():
        pytest.fail("a role-parked source must not query BlueZ")

    fake_systemctl = _parked_systemctl(calls)

    monkeypatch.setattr(
        reconcile,
        "source_intent_enabled",
        lambda source: intent_reads.append(source) or True,
    )
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: False)
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fail_bluez)

    plan = asyncio.run(
        reconcile.reconcile_once(
            env_file=str(env_file),
            systemctl=fake_systemctl,
            reason="source-intent",
        ),
    )

    assert intent_reads == [Source.BLUETOOTH]
    assert plan.adapter_services == ()
    assert not env_file.exists()
    assert (
        "disable", "--now", "jasper-wiim-remote-mic.service",
    ) in calls
    assert not any(command[0] == "enable" for command in calls)


@pytest.mark.parametrize(
    ("profile_allowed", "grouping_allowed", "expected"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_local_source_role_gate_combines_install_and_grouping_permission(
    monkeypatch,
    profile_allowed,
    grouping_allowed,
    expected,
):
    monkeypatch.setattr(reconcile, "read_install_profile", lambda: "full")
    monkeypatch.setattr(
        reconcile,
        "install_profile_allows_local_sources",
        lambda _profile: profile_allowed,
    )
    monkeypatch.setattr(
        reconcile,
        "local_sources_allowed",
        lambda: (grouping_allowed, None),
    )

    assert reconcile._local_sources_allowed() is expected


def test_local_source_role_probe_failure_parks_and_logs(monkeypatch, caplog):
    def invalid_profile():
        raise ValueError("bad profile")

    monkeypatch.setattr(reconcile, "read_install_profile", invalid_profile)

    with caplog.at_level(logging.WARNING):
        assert reconcile._local_sources_allowed() is False

    assert "event=accessory_mic.role_probe_failed" in caplog.text
    assert "bad profile" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        reconcile.AccessoryReconcileError("BlueZ discovery timed out"),
        reconcile.AdapterServiceActivationError("adapter remained inactive"),
        reconcile.BluetoothSourceIntentError("malformed source intent"),
        reconcile.AdapterServiceTeardownError("adapter remained active"),
    ],
)
def test_main_returns_failure_for_authoritative_reconcile_errors(
    monkeypatch, caplog, error,
):
    def fail_run(_awaitable):
        _awaitable.close()
        raise error

    monkeypatch.setattr(reconcile.asyncio, "run", fail_run)

    with caplog.at_level(logging.ERROR):
        assert reconcile.main(["--reason", "test"]) == 1

    assert "event=accessory_mic.reconcile_failed" in caplog.text
    assert str(error) in caplog.text


def test_apply_adapter_services_disables_inactive_profile_service():
    calls = []
    failures = reconcile.apply_adapter_services(
        (), systemctl=_parked_systemctl(calls),
    )

    assert (
        "disable", "--now", "jasper-wiim-remote-mic.service",
    ) in calls
    assert ("reset-failed", "jasper-wiim-remote-mic.service") in calls
    assert (
        "show",
        "jasper-wiim-remote-mic.service",
        "--property=UnitFileState",
        "--property=ActiveState",
    ) in calls
    assert failures == ()


def test_adapter_teardown_is_synchronous_and_bounded(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    reconcile._systemctl(("disable", "--now", "adapter.service"))

    assert captured["args"] == [
        "systemctl", "disable", "--now", "adapter.service",
    ]
    assert captured["kwargs"]["timeout"] == reconcile.SYSTEMCTL_TIMEOUT_SEC
    assert captured["kwargs"]["check"] is False


def test_adapter_teardown_attempts_every_service_and_aggregates_failures(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        reconcile,
        "adapter_mic_services",
        lambda: ("adapter-a.service", "adapter-b.service"),
    )

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        service = command[1] if command[0] == "show" else command[-1]
        if command[:2] == ("disable", "--now"):
            if service == "adapter-a.service":
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="stop denied",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[0] == "reset-failed":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[0] == "show":
            enabled = "enabled" if service == "adapter-a.service" else "disabled"
            active = "active" if service == "adapter-a.service" else "inactive"
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"UnitFileState={enabled}\n"
                    f"ActiveState={active}\n"
                ),
                stderr="",
            )
        raise AssertionError(command)

    failures = reconcile.apply_adapter_services((), systemctl=fake_systemctl)

    for service in ("adapter-a.service", "adapter-b.service"):
        assert ("disable", "--now", service) in calls
        assert ("reset-failed", service) in calls
        assert (
            "show",
            service,
            "--property=UnitFileState",
            "--property=ActiveState",
        ) in calls
    assert failures == (
        "adapter-a.service: systemctl disable --now adapter-a.service "
        "failed: stop denied",
        "adapter-a.service: expected is-enabled=disabled, observed enabled",
        "adapter-a.service: expected is-active=inactive, observed active",
    )


@pytest.mark.parametrize("active", [False, True])
def test_adapter_services_converge_in_stable_registry_order(monkeypatch, active):
    """The one owner applies adapters deterministically without worker machinery."""

    services = ("adapter-a.service", "adapter-b.service")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(reconcile, "adapter_mic_services", lambda: services)

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "show":
            enabled = "enabled" if active else "disabled"
            activity = "active" if active else "inactive"
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"UnitFileState={enabled}\n"
                    f"ActiveState={activity}\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    active_services = services if active else ()
    assert reconcile.apply_adapter_services(
        active_services,
        systemctl=fake_systemctl,
    ) == ()
    first_service_b_calls = [
        index for index, command in enumerate(calls)
        if "adapter-b.service" in command
    ]
    last_service_a_calls = [
        index for index, command in enumerate(calls)
        if "adapter-a.service" in command
    ]
    assert max(last_service_a_calls) < min(first_service_b_calls)
    for service in services:
        if active:
            assert ("enable", service) in calls
            assert ("restart", service) in calls
        else:
            assert ("disable", "--now", service) in calls
            assert ("reset-failed", service) in calls
        assert (
            "show",
            service,
            "--property=UnitFileState",
            "--property=ActiveState",
        ) in calls


@pytest.mark.parametrize(
    ("malformed_intent", "error_type"),
    [
        (False, reconcile.AdapterServiceTeardownError),
        (True, reconcile.BluetoothSourceIntentError),
    ],
)
def test_teardown_failure_raises_after_env_cleanup_and_voice_refresh(
    monkeypatch,
    tmp_path: Path,
    caplog,
    malformed_intent,
    error_type,
):
    env_file = tmp_path / "accessory-mics.env"
    env_file.write_text(
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n",
        encoding="utf-8",
    )
    calls = []

    async def fail_bluez():
        pytest.fail("Bluetooth Off must not query BlueZ")

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[:2] == ("disable", "--now"):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="stop failed",
            )
        if command[0] == "reset-failed":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=enabled\nActiveState=active\n",
                stderr="",
            )
        if command == ("is-active", "--quiet", "jasper-voice.service"):
            return SimpleNamespace(returncode=0)
        if command == ("--no-block", "restart", "jasper-voice.service"):
            return SimpleNamespace(returncode=0)
        raise AssertionError(command)

    def source_intent(_source):
        if malformed_intent:
            raise RuntimeError("malformed Bluetooth intent")
        return False

    monkeypatch.setattr(reconcile, "source_intent_enabled", source_intent)
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fail_bluez)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(
            error_type,
            match="stop failed.*observed enabled.*observed active",
        ):
            asyncio.run(
                reconcile.reconcile_once(
                    env_file=str(env_file),
                    systemctl=fake_systemctl,
                    reason="source-intent",
                ),
            )

    assert not env_file.exists()
    assert ("--no-block", "restart", "jasper-voice.service") in calls
    assert "event=accessory_mic.teardown_failed" in caplog.text
    assert "stop failed" in caplog.text
    assert "observed enabled" in caplog.text
    assert "observed active" in caplog.text
    if malformed_intent:
        assert "event=accessory_mic.intent_invalid" in caplog.text


def test_restart_voice_if_active_restarts_only_active_voice():
    calls = []

    def fake_systemctl(args):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0)

    assert reconcile.restart_voice_if_active(systemctl=fake_systemctl) is True
    assert calls == [
        ("is-active", "--quiet", "jasper-voice.service"),
        ("--no-block", "restart", "jasper-voice.service"),
    ]

    calls.clear()

    def inactive_systemctl(args):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=3)

    assert reconcile.restart_voice_if_active(systemctl=inactive_systemctl) is False
    assert calls == [("is-active", "--quiet", "jasper-voice.service")]


def test_adapter_service_systemctl_failures_are_observable(caplog):
    def failing_systemctl(args):
        return SimpleNamespace(returncode=1)

    with caplog.at_level(logging.WARNING):
        reconcile.apply_adapter_services(
            ("jasper-wiim-remote-mic.service",),
            systemctl=failing_systemctl,
        )

    assert "event=accessory_mic.systemctl_failed" in caplog.text
    assert 'command="systemctl enable jasper-wiim-remote-mic.service"' in caplog.text


def test_installer_enables_reconciler_not_profile_adapter_by_default():
    units_sh = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8",
    )

    assert "deploy/systemd/jasper-accessory-reconcile.service" in units_sh
    assert "deploy/systemd/jasper-wiim-remote-mic.service" in units_sh
    enable_block = units_sh.rsplit(
        "systemctl enable jasper-camilla.service jasper-fanin.service",
        1,
    )[1].split("park_audio_clients_for_core_graph_restart", 1)[0]
    assert "jasper-accessory-reconcile.service" in enable_block
    assert "jasper-wiim-remote-mic.service" not in enable_block
    assert "jasper-accessory-reconcile --reason install" in units_sh


def test_reconciler_does_not_order_before_adapter_it_restarts():
    unit = (ROOT / "deploy/systemd/jasper-accessory-reconcile.service").read_text(
        encoding="utf-8",
    )

    before_line = next(
        line for line in unit.splitlines() if line.startswith("Before=")
    )
    assert "jasper-voice.service" in before_line
    assert "jasper-wiim-remote-mic.service" not in before_line


def test_accessory_units_never_pull_bluetooth_service_up():
    for name in (
        "jasper-accessory-reconcile.service",
        "jasper-wiim-remote-mic.service",
    ):
        unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        dependency_lines = tuple(
            line for line in unit.splitlines()
            if line.startswith(("Wants=", "Requires="))
        )

        assert all("bluetooth.service" not in line for line in dependency_lines)
        after_line = next(
            line for line in unit.splitlines() if line.startswith("After=")
        )
        assert "bluetooth.service" in after_line


def test_wiim_adapter_skips_cleanly_until_console_script_exists():
    unit = (ROOT / "deploy/systemd/jasper-wiim-remote-mic.service").read_text(
        encoding="utf-8",
    )

    assert "ConditionPathExists=/opt/jasper/.venv/bin/jasper-wiim-remote-mic" in unit
    assert "StartLimitBurst=20" in unit
