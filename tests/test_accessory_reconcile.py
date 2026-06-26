from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from jasper.accessories.constants import WIIM_REMOTE_2_MIC_DEVICE
from jasper.accessories import reconcile

ROOT = Path(__file__).resolve().parents[1]


def _variant(value):
    return SimpleNamespace(value=value)


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

    def fake_systemctl(args):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0)

    reconcile.apply_adapter_services(
        ("jasper-wiim-remote-mic.service",),
        systemctl=fake_systemctl,
    )

    assert ("enable", "jasper-wiim-remote-mic.service") in calls
    assert ("restart", "jasper-wiim-remote-mic.service") in calls
    assert ("disable", "--now", "jasper-wiim-remote-mic.service") not in calls


def test_apply_adapter_services_disables_inactive_profile_service():
    calls = []

    def fake_systemctl(args):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0)

    reconcile.apply_adapter_services((), systemctl=fake_systemctl)

    assert ("disable", "--now", "jasper-wiim-remote-mic.service") in calls
    assert ("reset-failed", "jasper-wiim-remote-mic.service") in calls


def test_restart_voice_if_active_restarts_only_active_voice():
    calls = []

    def fake_systemctl(args):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0)

    assert reconcile.restart_voice_if_active(systemctl=fake_systemctl) is True
    assert calls == [
        ("is-active", "--quiet", "jasper-voice.service"),
        ("restart", "jasper-voice.service"),
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
