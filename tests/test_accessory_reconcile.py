from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.accessories.constants import WIIM_REMOTE_2_MIC_DEVICE
from jasper.accessories import reconcile
from tests.systemd_unit_helpers import value_for as _value_for
from tests._log_events import event_field_maps, event_fields
from jasper.music_sources import Source

ROOT = Path(__file__).resolve().parents[1]


def _variant(value):
    return SimpleNamespace(value=value)


def _parked_systemctl(calls):
    """Return a fake whose adapter terminal state is disabled + inactive."""

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=disabled\nActiveState=inactive\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_systemctl


def _voice_owner_systemctl(
    calls,
    *,
    adapter_active: bool = False,
    voice_active: bool = False,
):
    """Fake that answers `show` for jasper-voice, the adapter, and no gate owner."""

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] != "show":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1] == reconcile.VOICE_UNIT:
            state = "active" if voice_active else "inactive"
            return SimpleNamespace(
                returncode=0, stdout=f"ActiveState={state}\n", stderr="",
            )
        if "--property=LoadState" in command:
            return SimpleNamespace(
                returncode=0, stdout="LoadState=not-found\n", stderr="",
            )
        enabled = "enabled" if adapter_active else "disabled"
        active = "active" if adapter_active else "inactive"
        return SimpleNamespace(
            returncode=0,
            stdout=f"UnitFileState={enabled}\nActiveState={active}\n",
            stderr="",
        )

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
    assert not any(reconcile.VOICE_UNIT in command for command in calls)


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
        with pytest.raises(reconcile.AccessoryReconcileError):
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
    assert event_fields(caplog, "accessory_mic.bluez_discovery_failed") == {
        "reason": "test",
        "error": "timeout",
        "timeout_sec": "0.01",
    }


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
        if command == ("--no-block", "try-restart", "jasper-voice.service"):
            # This fake's `show` reports no LoadState, so refresh_voice_input
            # sees no loadable gate owner and falls back to try-restart, which
            # never starts a stopped voice daemon (issue #2205).
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(reconcile, "bluez_managed_objects", fake_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(reconcile.AdapterServiceActivationError):
            asyncio.run(
                reconcile.reconcile_once(
                    env_file=str(tmp_path / "accessory-mics.env"),
                    systemctl=fake_systemctl,
                    reason="test",
                ),
            )

    assert ("enable", "jasper-wiim-remote-mic.service") in calls
    assert ("restart", "jasper-wiim-remote-mic.service") in calls
    fields = event_fields(caplog, "accessory_mic.activation_failed")
    assert (fields["reason"], fields["env_changed"], fields["voice"]) == (
        "test",
        "1",
        "voice_try_restart",
    )


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
    intent_detail = "invalid intent value for bluetooth: maybe"

    def invalid_intent(_source):
        raise RuntimeError(intent_detail)

    async def fail_bluez():
        pytest.fail("malformed intent must fail closed before querying BlueZ")

    fake_systemctl = _parked_systemctl(calls)

    monkeypatch.setattr(reconcile, "source_intent_enabled", invalid_intent)
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fail_bluez)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(reconcile.BluetoothSourceIntentError):
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
    fields = event_fields(caplog, "accessory_mic.intent_invalid")
    assert (fields["reason"], fields["action"], fields["env_changed"]) == (
        "source-intent",
        "parked",
        "1",
    )
    assert fields["err"].endswith(intent_detail)


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
    probe_detail = "bad profile"

    def invalid_profile():
        raise ValueError(probe_detail)

    monkeypatch.setattr(reconcile, "read_install_profile", invalid_profile)

    with caplog.at_level(logging.WARNING):
        assert reconcile._local_sources_allowed() is False

    assert event_fields(caplog, "accessory_mic.role_probe_failed") == {
        "error": probe_detail,
    }


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

    assert event_fields(caplog, "accessory_mic.reconcile_failed") == {
        "reason": "test",
        "err": str(error),
    }


# --------------------------------------------------------------------------
# Request protocol (jasper-accessory-reconcile.path)
# --------------------------------------------------------------------------


def test_a_request_is_published_readably_and_claimed_exactly_once(tmp_path):
    """0664 because the reconciler runs as root with an empty
    CapabilityBoundingSet: no DAC override, so it reads this file on the
    ordinary group/other bits like anyone else."""
    request = tmp_path / "accessory-reconcile.request"

    reconcile.request_reconcile("bluetooth-pair", path=str(request))

    assert request.read_text(encoding="utf-8") == "bluetooth-pair"
    assert request.stat().st_mode & 0o777 == 0o664

    assert reconcile.claim_reconcile_request(str(request)) == "bluetooth-pair"
    assert list(tmp_path.iterdir()) == []


def test_claiming_nothing_is_not_an_error(tmp_path):
    assert reconcile.claim_reconcile_request(str(tmp_path / "absent")) is None


def test_a_request_written_during_a_pass_survives_its_claim(tmp_path):
    """The whole point of claiming by rename. A requester whose change landed
    after the pass read BlueZ must get its own pass, not this one's verdict."""
    request = tmp_path / "accessory-reconcile.request"
    reconcile.request_reconcile("source-intent", path=str(request))

    real_replace = reconcile.os.replace

    def replace_then_race(src, dst):
        real_replace(src, dst)
        # A concurrent requester, landing the instant the claim frees the name.
        # Keyed on the claim's own source: os.replace is global here, and
        # request_reconcile publishes through it too (tmp -> request).
        if str(src) == str(request):
            reconcile.request_reconcile("bluetooth-forget", path=str(request))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reconcile.os, "replace", replace_then_race)
        assert reconcile.claim_reconcile_request(str(request)) == "source-intent"

    assert request.exists(), "the racing request did not survive the claim"
    assert request.read_text(encoding="utf-8") == "bluetooth-forget"
    assert reconcile.claim_reconcile_request(str(request)) == "bluetooth-forget"


@pytest.mark.parametrize(
    ("failure", "code"),
    (
        (reconcile.AccessoryReconcileError("stop here"), 1),
        (RuntimeError("transient"), 0),
    ),
)
def test_main_prefers_a_claimed_reason_over_the_fallback_argument(
    monkeypatch,
    caplog,
    tmp_path,
    failure: Exception,
    code: int,
):
    """ExecStart carries `--reason boot` for the direct start; a request that is
    waiting names its own requester and must win — on both failure branches,
    since the soft one is the common outcome and the one that still exits 0."""
    request = tmp_path / "accessory-reconcile.request"
    reconcile.request_reconcile("bluetooth-forget", path=str(request))

    def fail_run(awaitable):
        awaitable.close()
        raise failure

    monkeypatch.setattr(reconcile.asyncio, "run", fail_run)

    with caplog.at_level(logging.WARNING):
        assert reconcile.main(
            ["--reason", "boot", "--reason-file", str(request)],
        ) == code

    fields = event_fields(caplog, "accessory_mic.reconcile_failed")
    assert fields["reason"] == "bluetooth-forget"
    # Drained even on the boot path: PathExists is level-triggered, so a
    # request left on disk re-starts the oneshot forever.
    assert not request.exists()


def test_main_falls_back_to_its_argument_when_no_request_is_waiting(
    monkeypatch,
    caplog,
    tmp_path,
):
    def fail_run(awaitable):
        awaitable.close()
        raise reconcile.AccessoryReconcileError("stop here")

    monkeypatch.setattr(reconcile.asyncio, "run", fail_run)

    with caplog.at_level(logging.ERROR):
        assert reconcile.main(
            ["--reason", "boot", "--reason-file", str(tmp_path / "absent")],
        ) == 1

    assert event_fields(caplog, "accessory_mic.reconcile_failed")["reason"] == "boot"


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


def test_adapter_teardown_ignores_reset_of_already_clean_unit(caplog):
    calls = []

    def clean_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "reset-failed":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "Failed to reset failed state: Unit adapter.service "
                    "not loaded."
                ),
            )
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=disabled\nActiveState=inactive\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with caplog.at_level(logging.WARNING):
        failures = reconcile.apply_adapter_services((), systemctl=clean_systemctl)

    assert ("reset-failed", "jasper-wiim-remote-mic.service") in calls
    assert failures == ()
    assert event_field_maps(caplog, "accessory_mic.systemctl_failed") == []


def test_adapter_teardown_still_rejects_failed_terminal_state():
    def failed_systemctl(args):
        command = tuple(args)
        if command[0] == "reset-failed":
            return SimpleNamespace(returncode=1, stdout="", stderr="reset failed")
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout="UnitFileState=disabled\nActiveState=failed\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert reconcile.apply_adapter_services((), systemctl=failed_systemctl) == (
        "jasper-wiim-remote-mic.service: expected is-active=inactive, "
        "observed failed",
    )


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
    unit = "jasper-wiim-remote-mic.service"
    intent_detail = "malformed Bluetooth intent"
    stop_detail = "stop failed"

    async def fail_bluez():
        pytest.fail("Bluetooth Off must not query BlueZ")

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[:2] == ("disable", "--now"):
            return SimpleNamespace(
                returncode=1, stdout="", stderr=stop_detail,
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
        if command == ("--no-block", "try-restart", "jasper-voice.service"):
            return SimpleNamespace(returncode=0)
        raise AssertionError(command)

    def source_intent(_source):
        if malformed_intent:
            raise RuntimeError(intent_detail)
        return False

    monkeypatch.setattr(reconcile, "source_intent_enabled", source_intent)
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fail_bluez)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(error_type):
            asyncio.run(
                reconcile.reconcile_once(
                    env_file=str(env_file),
                    systemctl=fake_systemctl,
                    reason="source-intent",
                ),
            )

    assert not env_file.exists()
    # try-restart, not restart: these fakes present no loadable gate owner,
    # and refresh_voice_input must never START a stopped voice daemon.
    assert ("--no-block", "try-restart", "jasper-voice.service") in calls
    fields = event_fields(caplog, "accessory_mic.teardown_failed")
    assert (fields["reason"], fields["env_changed"], fields["voice"]) == (
        "source-intent",
        "1",
        "voice_try_restart",
    )
    # Every terminal-state observation is carried, not just the first failure.
    assert fields["failures"].split(" | ") == [
        f"{unit}: systemctl disable --now {unit} failed: {stop_detail}",
        f"{unit}: expected is-enabled=disabled, observed enabled",
        f"{unit}: expected is-active=inactive, observed active",
    ]
    invalid = event_field_maps(caplog, "accessory_mic.intent_invalid")
    if malformed_intent:
        assert len(invalid) == 1 and invalid[0]["err"].endswith(intent_detail)
    else:
        assert invalid == []


def _gate_owner_systemctl(calls, *, load_state: str, unit_file_state: str):
    """Fake whose `systemctl show` answers for the voice-input gate owner."""

    def fake_systemctl(args):
        command = tuple(args)
        calls.append(command)
        if command[0] == "show":
            return SimpleNamespace(
                returncode=0,
                stdout=f"LoadState={load_state}\nUnitFileState={unit_file_state}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_systemctl


def test_refresh_voice_input_starts_an_enabled_gate_owner():
    """Issue #2205: a stopped jasper-voice may be GATED off, and no restart can
    open ``ConditionPathExists=!/var/lib/jasper/voice-input-absent``. Only the
    marker's single writer can, so start it and let it re-derive."""
    calls = []
    systemctl = _gate_owner_systemctl(
        calls, load_state="loaded", unit_file_state="enabled",
    )

    assert reconcile.refresh_voice_input(systemctl=systemctl) == "gate_reconcile"
    assert calls == [
        ("show", "jasper-aec-reconcile.service",
         "--property=LoadState", "--property=UnitFileState"),
        ("--no-block", "start", "jasper-aec-reconcile.service"),
    ]


def test_refresh_voice_input_never_starts_a_parked_gate_owner(caplog):
    """The un-park hazard. ``park_streambox_brain_units`` runs
    ``disable --now`` on jasper-aec-reconcile but does NOT touch this
    reconciler, so a converted full->streambox box keeps the unit FILE while we
    keep running. ``systemctl start`` on a disabled-but-installed unit
    SUCCEEDS, and the gate owner's ``restart_voice`` runs
    ``systemctl enable jasper-voice.service`` — persistently re-arming the voice
    brain on a Zero-class box whose profile exists to keep it off."""
    calls = []
    systemctl = _gate_owner_systemctl(
        calls, load_state="loaded", unit_file_state="disabled",
    )

    with caplog.at_level(logging.INFO):
        assert (
            reconcile.refresh_voice_input(systemctl=systemctl)
            == "voice_try_restart"
        )
    assert ("--no-block", "start", "jasper-aec-reconcile.service") not in calls
    assert calls[-1] == ("--no-block", "try-restart", "jasper-voice.service")
    assert event_fields(caplog, "accessory_mic.gate_owner_unavailable") == {
        "unit": reconcile.VOICE_INPUT_GATE_UNIT,
        "state": "parked",
    }


def test_refresh_voice_input_reports_a_masked_gate_owner_as_masked(caplog):
    """Masking is a deliberate operator act. The ACTION is the same as absent —
    never start it — but the remediation is `systemctl unmask`, not "re-run the
    installer", so the log must not call it "not installed"."""
    calls = []
    systemctl = _gate_owner_systemctl(
        calls, load_state="masked", unit_file_state="masked",
    )

    with caplog.at_level(logging.INFO):
        assert (
            reconcile.refresh_voice_input(systemctl=systemctl)
            == "voice_try_restart"
        )
    assert ("--no-block", "start", "jasper-aec-reconcile.service") not in calls
    assert event_fields(caplog, "accessory_mic.gate_owner_unavailable") == {
        "unit": reconcile.VOICE_INPUT_GATE_UNIT,
        "state": "masked",
    }


def test_refresh_voice_input_falls_back_when_gate_owner_is_absent(caplog):
    """A streambox never installs jasper-aec-reconcile (LoadState=not-found,
    verified on jts4). Nothing writes the marker there, so voice is never gated
    off for a missing local mic and try-restart is the whole story."""
    calls = []
    systemctl = _gate_owner_systemctl(
        calls, load_state="not-found", unit_file_state="",
    )

    with caplog.at_level(logging.INFO):
        assert (
            reconcile.refresh_voice_input(systemctl=systemctl)
            == "voice_try_restart"
        )
    assert ("--no-block", "start", "jasper-aec-reconcile.service") not in calls
    assert event_fields(caplog, "accessory_mic.gate_owner_unavailable") == {
        "unit": reconcile.VOICE_INPUT_GATE_UNIT,
        "state": "absent",
    }


def test_refresh_voice_input_uses_try_restart_so_it_never_starts_stopped_voice():
    """Plain `restart` would START a voice daemon the household or the
    streambox profile deliberately stopped; `try-restart` cannot.

    The no-op property is systemd's, measured on jts4 2026-08-07: rc=0 with
    `ExecMainStartTimestamp` unchanged against a loaded-`inactive` unit AND a
    deliberately-`failed` one, while a plain `start` on the same unit re-ran
    it. What this test pins is the half that lives here — that we spend the
    `try-restart` verb and never `restart`."""
    for load_state, unit_file_state in (("not-found", ""), ("loaded", "disabled")):
        calls = []
        systemctl = _gate_owner_systemctl(
            calls, load_state=load_state, unit_file_state=unit_file_state,
        )
        reconcile.refresh_voice_input(systemctl=systemctl)
        voice_calls = [c for c in calls if "jasper-voice.service" in c]
        assert voice_calls == [
            ("--no-block", "try-restart", "jasper-voice.service"),
        ], (load_state, unit_file_state, calls)


def test_refresh_voice_input_stays_within_its_declared_systemctl_budget():
    """The bound tests/test_systemd_hardening.py holds against the unit's
    TimeoutStartSec is a claim about this function; measure it, don't assume."""
    for load_state, unit_file_state in (
        ("loaded", "enabled"), ("loaded", "disabled"), ("not-found", ""),
        ("masked", "masked"),
    ):
        calls = []
        systemctl = _gate_owner_systemctl(
            calls, load_state=load_state, unit_file_state=unit_file_state,
        )
        reconcile.refresh_voice_input(systemctl=systemctl)
        assert len(calls) <= reconcile._VOICE_REFRESH_SYSTEMCTL_CALLS, calls


@pytest.mark.parametrize(
    ("wanted", "env_changed", "voice_active", "action", "command"),
    [
        (True, False, True, "start", ("--no-block", "start")),
        (True, True, True, "restart", ("--no-block", "restart")),
        (True, True, False, "start", ("--no-block", "start")),
        (False, True, True, "stop", ("stop",)),
    ],
)
def test_converge_voice_unit_runs_voice_for_as_long_as_a_mic_is_published(
    wanted, env_changed, voice_active, action, command,
):
    calls = []
    systemctl = _voice_owner_systemctl(calls, voice_active=voice_active)

    assert reconcile.converge_voice_unit(
        wanted=wanted,
        env_changed=env_changed,
        systemctl=systemctl,
    ) == (action, ())

    assert (*command, reconcile.VOICE_UNIT) in calls
    assert not any(
        verb in entry and reconcile.VOICE_UNIT in entry
        for entry in calls
        for verb in ("enable", "disable")
    )
    assert len(calls) <= reconcile._VOICE_REFRESH_SYSTEMCTL_CALLS, calls


def test_converge_voice_unit_reports_a_refused_start_as_a_failure():
    def refusing_systemctl(args):
        if tuple(args)[0] == "show":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Access denied")

    action, _failures = reconcile.converge_voice_unit(
        wanted=True, env_changed=False, systemctl=refusing_systemctl,
    )

    assert action == "none"


@pytest.mark.parametrize(
    ("device_name", "voice_command"),
    [
        ("WiiM Remote 2", ("--no-block", "start")),
        (None, ("stop",)),
        ("Anticater VK-01", ("stop",)),
    ],
)
def test_reconciler_owns_voice_where_it_follows_the_accessory_mic(
    monkeypatch,
    tmp_path: Path,
    device_name,
    voice_command,
):
    env_file = tmp_path / "accessory-mics.env"
    calls = []
    managed = (
        {"/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device(name=device_name)}
        if device_name
        else {}
    )

    async def fake_bluez():
        return managed

    monkeypatch.setattr(reconcile, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fake_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)

    asyncio.run(
        reconcile.reconcile_once(
            env_file=str(env_file),
            systemctl=_voice_owner_systemctl(
                calls, adapter_active=bool(device_name == "WiiM Remote 2"),
            ),
            reason="pair",
        ),
    )

    assert (*voice_command, reconcile.VOICE_UNIT) in calls
    assert not any(
        verb in entry and reconcile.VOICE_UNIT in entry
        for entry in calls
        for verb in ("enable", "disable", "try-restart")
    )
    assert ("--no-block", "start", reconcile.VOICE_INPUT_GATE_UNIT) not in calls


def test_owned_voice_restarts_when_published_sources_change_under_it(
    monkeypatch,
    tmp_path: Path,
):
    env_file = tmp_path / "accessory-mics.env"
    env_file.write_text(
        "JASPER_MANUAL_MIC_SOURCES=wiim_remote_2=hw:Stale\n",
        encoding="utf-8",
    )
    calls = []

    async def fake_bluez():
        return {"/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device()}

    monkeypatch.setattr(reconcile, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fake_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)

    asyncio.run(
        reconcile.reconcile_once(
            env_file=str(env_file),
            systemctl=_voice_owner_systemctl(
                calls, adapter_active=True, voice_active=True,
            ),
            reason="pair",
        ),
    )

    assert env_file.read_text(encoding="utf-8") == (
        f"JASPER_MANUAL_MIC_SOURCES=wiim_remote_2={WIIM_REMOTE_2_MIC_DEVICE}\n"
    )
    assert ("--no-block", "restart", reconcile.VOICE_UNIT) in calls
    assert ("--no-block", "start", reconcile.VOICE_UNIT) not in calls


def test_wake_detection_profile_keeps_handing_voice_to_its_gate_owner(
    monkeypatch,
    tmp_path: Path,
):
    env_file = tmp_path / "accessory-mics.env"
    calls = []

    async def fake_bluez():
        return {"/org/bluez/hci0/dev_CA_AC_04_04_09_D7": _bluez_device()}

    monkeypatch.setattr(reconcile, "read_install_profile", lambda: "full")
    monkeypatch.setattr(reconcile, "bluez_managed_objects", fake_bluez)
    monkeypatch.setattr(reconcile, "source_intent_enabled", lambda _source: True)
    monkeypatch.setattr(reconcile, "_local_sources_allowed", lambda: True)

    asyncio.run(
        reconcile.reconcile_once(
            env_file=str(env_file),
            systemctl=_voice_owner_systemctl(calls, adapter_active=True),
            reason="pair",
        ),
    )

    assert ("--no-block", "try-restart", reconcile.VOICE_UNIT) in calls
    assert not any(
        command[-1] == reconcile.VOICE_UNIT
        and command[-2] in ("start", "restart", "stop")
        for command in calls
    )


def test_adapter_service_systemctl_failures_are_observable(caplog):
    def failing_systemctl(args):
        return SimpleNamespace(returncode=1)

    with caplog.at_level(logging.WARNING):
        reconcile.apply_adapter_services(
            ("jasper-wiim-remote-mic.service",),
            systemctl=failing_systemctl,
        )

    unit = "jasper-wiim-remote-mic.service"
    assert event_field_maps(caplog, "accessory_mic.systemctl_failed") == [
        {"command": f"systemctl enable {unit}", "returncode": "1"},
        {"command": f"systemctl restart {unit}", "returncode": "1"},
    ]


def test_the_path_unit_watches_the_request_file_this_module_publishes():
    """The watcher and the writers must name the same file; a drift here drops
    every request silently, with the reconciler still reporting healthy."""
    body = (ROOT / "deploy/systemd/jasper-accessory-reconcile.path").read_text(
        encoding="utf-8",
    )

    assert _value_for(body, "PathExists") == (
        reconcile.DEFAULT_RECONCILE_REQUEST_FILE
    )
    assert _value_for(body, "Unit") == "jasper-accessory-reconcile.service"
    assert _value_for(body, "WantedBy") == "multi-user.target"


def test_installer_enables_reconciler_not_profile_adapter_by_default():
    units_sh = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8",
    )

    assert "deploy/systemd/jasper-accessory-reconcile.service" in units_sh
    assert "deploy/systemd/jasper-accessory-reconcile.path" in units_sh
    assert "deploy/systemd/jasper-wiim-remote-mic.service" in units_sh
    enable_block = units_sh.rsplit(
        "systemctl enable jasper-camilla.service jasper-fanin.service",
        1,
    )[1].split("park_audio_clients_for_core_graph_restart", 1)[0]
    assert "jasper-accessory-reconcile.service" in enable_block
    assert "jasper-wiim-remote-mic.service" not in enable_block
    assert "jasper-accessory-reconcile --reason install" in units_sh


@pytest.mark.parametrize(
    "function",
    ("install_systemd_units", "start_streambox_runtime_units"),
)
def test_both_profiles_start_the_request_watcher_this_boot(function: str):
    """`enable` alone arms the watcher for the NEXT boot. Every refresh
    requested before then would be dropped, and nothing else would say so."""
    units_sh = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8",
    )
    body = units_sh.split(f"{function}() {{", 1)[1].split("\n}", 1)[0]

    assert "systemctl enable --now jasper-accessory-reconcile.path" in body


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
