# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's usbsink checks.

The checks are hardware-side (systemctl, /proc/asound,
/boot/firmware/config.txt, /lib/modules) so we monkeypatch the helpers
and reads. Pi-side smoke testing happens via jasper-doctor itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from jasper import audio_runtime_plan
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
from jasper.cli import doctor
from jasper.fanin import coupling_auto as _ca
from jasper.fanin import coupling_reconcile as _cr


# ----------------------------------------------------------------------
# shared USB data role
# ----------------------------------------------------------------------


def _role(**overrides) -> UsbPortRoleState:
    values = dict(
        board_model="Raspberry Pi 5 Model B Rev 1.0",
        board_topology="separate_host_ports",
        desired_role="peripheral",
        configured_role="peripheral",
        active_role="peripheral",
        gadget_available=True,
        reboot_required=False,
        reason="available",
        decision_reason="dedicated_host_ports_leave_otg_available",
        management_transport_available=True,
    )
    values.update(overrides)
    return UsbPortRoleState(**values)


@pytest.fixture(autouse=True)
def _available_usb_role(monkeypatch):
    monkeypatch.setattr(doctor.usbsink, "current_usb_data_role", _role)


def test_usb_data_role_available():
    r = doctor.check_usb_data_role()
    assert r.status == "ok"
    assert "gadget available" in r.detail.lower()


def test_usb_data_role_intentional_zero_host_is_ok(monkeypatch):
    monkeypatch.setattr(
        doctor.usbsink,
        "current_usb_data_role",
        lambda: _role(
            board_model="Raspberry Pi Zero 2 W Rev 1.0",
            board_topology="shared_otg_port",
            desired_role="host",
            configured_role="host",
            active_role="host",
            gadget_available=False,
            reason="shared_otg_usb_output_requires_host",
            decision_reason="shared_otg_usb_output_requires_host",
            management_transport_available=False,
        ),
    )
    r = doctor.check_usb_data_role()
    assert r.status == "ok"
    assert "output dac" in r.detail.lower()


def test_usb_data_role_pending_reboot_is_warn(monkeypatch):
    monkeypatch.setattr(
        doctor.usbsink,
        "current_usb_data_role",
        lambda: _role(
            configured_role="host",
            active_role="host",
            gadget_available=False,
            reboot_required=True,
            reason="role_change_pending_reboot",
            management_transport_available=False,
        ),
    )
    r = doctor.check_usb_data_role()
    assert r.status == "warn"
    assert "reboot" in r.detail.lower()


def test_composition_allows_ncm_only_during_pending_host_reboot(
    monkeypatch,
    tmp_path,
):
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "ncm.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(
        doctor.usbsink,
        "current_usb_data_role",
        lambda: _role(
            board_model="Raspberry Pi Zero 2 W Rev 1.0",
            board_topology="shared_otg_port",
            desired_role="host",
            configured_role="host",
            active_role="peripheral",
            gadget_available=False,
            reboot_required=True,
            reason="role_change_pending_reboot",
            decision_reason="shared_otg_defaults_host_without_i2s",
            management_transport_available=True,
        ),
    )
    udc = tmp_path / "udc"
    (udc / "3f980000.usb").mkdir(parents=True)
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc))
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_composition_wanted",
        lambda: (False, "intent_disabled"),
    )

    result = doctor.check_usbgadget_composition()

    assert result.status == "warn"
    assert "retained" in result.detail.lower()


# ----------------------------------------------------------------------
# check_usbsink_state — service active path
# ----------------------------------------------------------------------


def _patch_active(monkeypatch, active: bool):
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: active)


def _patch_libcomp_loaded(monkeypatch, loaded: bool):
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: loaded)


def test_usbsink_state_disabled_no_libcomposite(monkeypatch):
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, False)
    r = doctor.check_usbsink_state()
    assert r.status == "ok"
    assert "disabled" in r.detail.lower()


def test_usbsink_state_disabled_libcomposite_loaded_is_warn(monkeypatch):
    """RAM drift detection: service stopped but libcomposite still
    loaded means the previous stop didn't tear cleanly."""
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, True)
    r = doctor.check_usbsink_state()
    assert r.status == "warn"
    assert "libcomposite" in r.detail.lower()
    assert "ram" in r.detail.lower() or "drift" in r.detail.lower()


def test_usbsink_state_disabled_with_gadget_is_fail(monkeypatch, tmp_path):
    """Computers seeing USB audio while the readiness marker is inactive is drift.

    The uac2.usb0 function is still composed, so doctor must hard-fail the
    split-brain state that made /sources look off while hosts still saw JTS.
    """
    gadget = tmp_path / "jts-usb-audio"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, True)

    r = doctor.check_usbsink_state()

    assert r.status == "fail"
    assert "advertised" in r.detail.lower()
    assert "readiness marker inactive" in r.detail.lower()


def test_usbsink_state_disabled_gadget_present_ncm_only_is_ok(monkeypatch, tmp_path):
    """When the resolved role permits the default-on management network, the
    composite gadget legitimately persists while USB Audio Input is off. A
    ConfigFS dir carrying only ncm.usb0 must not be treated as audio drift."""
    gadget = tmp_path / "jts-usb-audio"
    (gadget / "functions" / "ncm.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, True)

    r = doctor.check_usbsink_state()

    assert r.status == "ok"
    assert "composite gadget" in r.detail.lower() or "always-on" in r.detail.lower()


def test_usbsink_state_parked_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "missing")
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: False)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "ok"
    assert "parked" in r.detail.lower()
    assert "uac2.usb0 function down" in r.detail.lower()


def test_usbsink_state_parked_clean_with_ncm_gadget_notes_network(
    monkeypatch, tmp_path,
):
    """A parked follower's gadget dir may still legitimately carry ncm.usb0
    when the resolved role permits management transport. The ok detail should
    say so rather than reading like the gadget vanished entirely."""
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    gadget = tmp_path / "jts-usb-audio"
    (gadget / "functions" / "ncm.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: True)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "ok"
    assert "management network" in r.detail.lower()


def test_usbsink_state_parked_with_gadget_is_fail(monkeypatch, tmp_path):
    """A parked follower with uac2.usb0 still composed is the split-brain
    state — the local-source park plan should have recomposed the gadget
    without the audio function."""
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    gadget = tmp_path / "jts-usb-audio"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: True)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "fail"
    assert "parked" in r.detail.lower()
    assert "advertised" in r.detail.lower()
    assert "uac2.usb0 function present" in r.detail


def test_usbsink_state_parked_module_only_is_ok(monkeypatch, tmp_path):
    """Parked + uac2.usb0 absent + libcomposite loaded is not audio drift by
    itself: the hardware-permitted gadget may carry ncm.usb0 independently.
    check_usbgadget_composition (not this check) owns genuine RAM-drift
    detection for the composite gadget as a whole."""
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "missing")
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: True)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "ok"
    assert "parked" in r.detail.lower()


def test_usbsink_state_active_without_composed_function_fails(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "missing")

    result = doctor.check_usbsink_state()

    assert result.status == "fail"
    assert "uac2.usb0 is absent" in result.detail


def test_usbsink_state_active_reads_host_connection_from_udc(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    controller = tmp_path / "udc" / "controller"
    controller.mkdir(parents=True)
    (controller / "state").write_text("configured\n")
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(tmp_path / "udc"))

    result = doctor.check_usbsink_state()

    assert result.status == "ok"
    assert "readiness marker active" in result.detail
    assert "host_connected=True" in result.detail
    assert "fan-in status" in result.detail.lower()


# ----------------------------------------------------------------------
# check_usbsink_card
# ----------------------------------------------------------------------


def test_usbsink_card_disabled_skips(monkeypatch):
    _patch_active(monkeypatch, False)
    r = doctor.check_usbsink_card()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower() or "disabled" in r.detail.lower()


def test_usbsink_card_active_present(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    fake_card = tmp_path / "UAC2Gadget"
    fake_card.mkdir()
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/proc/asound/UAC2Gadget":
                return fake_card
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_card()
    assert r.status == "ok"
    assert "UAC2Gadget" in r.detail


def test_usbsink_card_active_missing_is_fail(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    nonexistent = tmp_path / "missing"
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/proc/asound/UAC2Gadget":
                return nonexistent
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_card()
    assert r.status == "fail"
    assert "init" in r.detail.lower() or "missing" in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbsink_low_latency_contract
# ----------------------------------------------------------------------


def _low_latency_plan():
    return audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        route_mode="solo",
    )


def _fanin_direct_status(
    *,
    health="idle",
    period_frames=256,
    buffer_frames=768,
    device="hw:UAC2Gadget",
):
    return {
        "inputs": [
            {
                "label": "usbsink",
                "source": "direct",
                "direct": {
                    "device": device,
                    "health": health,
                    "period_frames": period_frames,
                    "buffer_frames": buffer_frames,
                },
                "resampler": {
                    "locked": health == "capturing",
                    "target_fill_frames": 2048,
                },
            }
        ]
    }


def _enable_usb_audio_contract(monkeypatch):
    monkeypatch.setattr(doctor.usbsink, "_audio_wanted", lambda: (True, "enabled"))


def test_usbsink_low_latency_contract_skips_non_claiming_route(monkeypatch):
    plan = audio_runtime_plan.build_audio_runtime_plan(route_mode="solo")
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "ok"
    assert "no USB low-latency claim" in r.detail


def test_usbsink_low_latency_contract_requires_fanin_status(monkeypatch):
    _enable_usb_audio_contract(monkeypatch)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "read_status_socket",
        lambda _path: (_ for _ in ()).throw(OSError("socket unavailable")),
    )

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "fan-in STATUS" in r.detail


def test_usbsink_low_latency_contract_warns_missing_optional_attrs(
    monkeypatch,
    tmp_path,
):
    _enable_usb_audio_contract(monkeypatch)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "read_status_socket",
        lambda _path: _fanin_direct_status(),
    )
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "warn"
    assert "kernel does not expose" in r.detail


def test_usbsink_low_latency_contract_fails_direct_period_mismatch(monkeypatch):
    _enable_usb_audio_contract(monkeypatch)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "read_status_socket",
        lambda _path: _fanin_direct_status(
            health="capturing",
            period_frames=128,
        ),
    )

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "period_frames" in r.detail


def test_usbsink_low_latency_contract_fails_mismatched_exposed_attr(
    monkeypatch,
    tmp_path,
):
    _enable_usb_audio_contract(monkeypatch)
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "read_status_socket",
        lambda _path: _fanin_direct_status(),
    )
    function_path = tmp_path / "gadget" / "functions" / "uac2.usb0"
    function_path.mkdir(parents=True)
    (function_path / "c_sync").write_text("adaptive\n")
    (function_path / "req_number").write_text("2\n")
    (function_path / "c_hs_bint").write_text("1\n")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "gadget")

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "c_sync" in r.detail


def test_usbsink_low_latency_contract_skips_when_user_turned_usb_off(monkeypatch):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_wanted",
        lambda: (False, "intent_disabled"),
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "read_status_socket",
        lambda _path: (_ for _ in ()).throw(AssertionError("must not probe fan-in")),
    )

    result = doctor.check_usbsink_low_latency_contract()

    assert result.status == "ok"
    assert "intent_disabled" in result.detail


def test_usbsink_low_latency_contract_skips_when_follower_parks_usb(monkeypatch):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_wanted",
        lambda: (False, "parked_follower"),
    )

    result = doctor.check_usbsink_low_latency_contract()

    assert result.status == "ok"
    assert "parked_follower" in result.detail


def test_usbsink_low_latency_contract_fails_invalid_intent(monkeypatch):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_wanted",
        lambda: (False, "intent_invalid:bad token"),
    )

    result = doctor.check_usbsink_low_latency_contract()

    assert result.status == "fail"
    assert "bad token" in result.detail


# ----------------------------------------------------------------------
# check_usbsink_active_libcomposite — the asymmetric mirror of the
# "service inactive + libcomposite loaded" RAM-drift check.
# ----------------------------------------------------------------------


def test_active_libcomposite_disabled_skip(monkeypatch):
    _patch_active(monkeypatch, False)
    r = doctor.check_usbsink_active_libcomposite()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower()


def test_active_libcomposite_consistent(monkeypatch):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    r = doctor.check_usbsink_active_libcomposite()
    assert r.status == "ok"
    assert "consistent" in r.detail.lower()


def test_active_libcomposite_unloaded_is_fail(monkeypatch):
    """Daemon active but libcomposite missing → audio won't flow,
    even though systemd thinks the unit is healthy. This is the
    asymmetric drift the original RAM-drift check missed."""
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, False)
    r = doctor.check_usbsink_active_libcomposite()
    assert r.status == "fail"
    assert "libcomposite" in r.detail.lower()
    assert "restart" in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbsink_name — host-visible device name patch state
# ----------------------------------------------------------------------

_KVER = "6.12.0-test"


def _name_env(monkeypatch, *, active: bool, speaker: str = "Kitchen"):
    """Common monkeypatching: service active state, kernel release,
    and the canonical speaker-name reader."""
    monkeypatch.setattr(
        doctor.usbsink, "_systemd_is_active", lambda unit: active
    )
    monkeypatch.setattr(
        doctor.os, "uname",
        lambda: type("U", (), {"release": _KVER})(),
    )
    monkeypatch.setattr("jasper.speaker_name.runtime_name", lambda: speaker)


def _write_override(root: Path, body: bytes, marker: str | None) -> None:
    updates = root / _KVER / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    (updates / "usb_f_uac2.ko").write_bytes(body)
    if marker is not None:
        (updates / ".jasper-usbsink-name.marker").write_text(marker)


def test_usbsink_name_skipped_when_disabled(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=False)
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "ok"
    assert "skipped" in r.detail.lower()


def test_usbsink_name_warns_when_override_missing(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True)
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "warn"
    assert "no name-patched module override" in r.detail


def test_usbsink_name_warns_when_stock_string_remains(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True)
    # Override present but never actually patched.
    _write_override(
        tmp_path,
        b"\x7fELF" + b"Playback Inactive\x00Capture Inactive\x00rest",
        marker=f"3\t{_KVER}\tKitchen\tKitchen Mic\tdeadbeef",
    )
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "warn"
    assert "stock string" in r.detail


def test_usbsink_name_warns_when_only_active_capture_string_remains(
    monkeypatch, tmp_path,
):
    _name_env(monkeypatch, active=True)
    _write_override(
        tmp_path,
        b"\x7fELF Kitchen\x00Kitchen Mic\x00Capture Active\x00rest",
        marker=f"3\t{_KVER}\tKitchen\tKitchen Mic\tdeadbeef",
    )

    result = doctor.check_usbsink_name(modules_root=str(tmp_path))

    assert result.status == "warn"
    assert "stock string" in result.detail


def test_usbsink_name_ok_when_patched_and_marker_matches(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True, speaker="Kitchen")
    _write_override(
        tmp_path,
        b"\x7fELF Kitchen\x00 patched body, no stock token",
        marker=f"3\t{_KVER}\tKitchen\tKitchen Mic\tdeadbeef",
    )
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "ok"
    assert "Kitchen" in r.detail


def test_usbsink_name_warns_when_marker_name_stale(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True, speaker="Living Room")
    # Override patched for an older name; speaker has since been renamed.
    _write_override(
        tmp_path,
        b"\x7fELF Kitchen\x00 patched body",
        marker=f"3\t{_KVER}\tKitchen\tKitchen Mic\tdeadbeef",
    )
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "warn"
    assert "stale" in r.detail.lower()


def test_usbsink_name_warns_when_marker_missing(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True, speaker="Kitchen")
    _write_override(
        tmp_path,
        b"\x7fELF Kitchen\x00 patched body",
        marker=None,
    )
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "warn"
    assert "stale" in r.detail.lower()


def test_usbsink_name_warns_for_playback_only_patch_schema(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True, speaker="Kitchen")
    _write_override(
        tmp_path,
        b"\x7fELF Kitchen\x00 patched body",
        marker=f"{_KVER}\tKitchen\tdeadbeef",
    )

    result = doctor.check_usbsink_name(modules_root=str(tmp_path))

    assert result.status == "warn"
    assert "stale" in result.detail.lower()


# ----------------------------------------------------------------------
# check_usbgadget_composition — composed gadget functions vs. composed
# *intent* (network kill-switch x audio enablement x follower-park gate).
# This is the composite-era replacement for the old "libcomposite loaded
# <=> usbsink active" invariant; the matrix below enumerates every cell
# of the truth table in docs/HANDOFF-usb-gadget.md / jasper-usbgadget-up.
# ----------------------------------------------------------------------


def _gadget_tree(tmp_path, *, ncm: bool = False, uac2: bool = False) -> Path:
    """Build (or leave absent) a ConfigFS gadget dir under tmp_path with
    the requested function subdirs. `parents=True` on each mkdir also
    creates `root` itself, so a caller requesting neither function gets
    back a path that genuinely doesn't exist on disk."""
    root = tmp_path / "jts-usb-audio"
    functions = root / "functions"
    if ncm:
        (functions / "ncm.usb0").mkdir(parents=True, exist_ok=True)
    if uac2:
        (functions / "uac2.usb0").mkdir(parents=True, exist_ok=True)
    return root


def _patch_composition_env(
    monkeypatch,
    tmp_path,
    *,
    udc_present: bool,
    network_env: str | None,
    usbsink_enabled: bool,
    lifecycle_ready: bool | None = None,
    direct_ready: bool | None = None,
    parked_follower: bool = False,
    ncm: bool = False,
    uac2: bool = False,
):
    udc_dir = tmp_path / "udc"
    if udc_present:
        udc_dir.mkdir(exist_ok=True)
        (udc_dir / "fe980000.usb").mkdir(exist_ok=True)
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))
    if network_env is None:
        monkeypatch.delenv("JASPER_USB_NETWORK", raising=False)
    else:
        monkeypatch.setenv("JASPER_USB_NETWORK", network_env)

    # With neither function requested, `gadget` is a path that does not
    # exist on disk (the "no gadget directory at all" cell); callers that
    # need to test a present-but-empty gadget dir create it explicitly
    # after this call (see test_composition_nothing_wanted_but_gadget_present_is_fail).
    gadget = _gadget_tree(tmp_path, ncm=ncm, uac2=uac2)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)

    monkeypatch.setattr(
        doctor.usbsink,
        "source_intent_enabled",
        lambda _source: usbsink_enabled,
    )
    monkeypatch.setattr(
        doctor.usbsink, "_parked_as_bonded_follower", lambda: parked_follower,
    )
    if lifecycle_ready is None:
        lifecycle_ready = usbsink_enabled
    if direct_ready is None:
        direct_ready = lifecycle_ready
    monkeypatch.setattr(
        doctor.usbsink,
        "_run",
        lambda _cmd: SimpleNamespace(
            returncode=0 if lifecycle_ready else 1,
            stdout="enabled\n" if lifecycle_ready else "disabled\n",
        ),
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "read_fanin_status",
        lambda **_kwargs: _fanin_direct_status() if direct_ready else None,
    )
    return gadget


def test_composition_no_udc_is_ok_skip(monkeypatch, tmp_path):
    """Fresh install pre-reboot (no UDC yet) must never fail — the unit
    itself skips cleanly via jasper-usbgadget-wanted, and
    check_usb_data_role already owns telling the user to reboot."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=False, network_env="enabled", usbsink_enabled=True,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "no udc" in r.detail.lower()


def test_composition_network_enabled_audio_enabled_matches(monkeypatch, tmp_path):
    """network=enabled, audio=enabled/allowed -> ncm.usb0 + uac2.usb0."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="enabled", usbsink_enabled=True,
        ncm=True, uac2=True,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "matches intent" in r.detail


def test_composition_network_enabled_audio_disabled_matches(monkeypatch, tmp_path):
    """network=enabled, audio=intent_disabled -> ncm.usb0 only."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="enabled", usbsink_enabled=False,
        ncm=True, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "intent_disabled" in r.detail
    assert "matches intent" in r.detail


def test_composition_desired_on_lifecycle_not_ready_matches_network_only(
    monkeypatch,
    tmp_path,
):
    """A failed On transition intentionally suppresses UAC2 until ready."""

    _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env="enabled",
        usbsink_enabled=True,
        lifecycle_ready=False,
        ncm=True,
        uac2=False,
    )
    result = doctor.check_usbgadget_composition()
    assert result.status == "ok"
    assert "derived_unit_disabled" in result.detail
    assert "matches intent" in result.detail


def test_composition_desired_on_direct_unarmed_matches_network_only(
    monkeypatch,
    tmp_path,
):
    """UAC2 stays hidden until fan-in is a live consumer.

    The separate combo fallback check diagnoses why DIRECT failed to arm;
    composition itself is correct and a gadget restart cannot repair it.
    """

    _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env="enabled",
        usbsink_enabled=True,
        lifecycle_ready=True,
        direct_ready=False,
        ncm=True,
        uac2=False,
    )
    result = doctor.check_usbgadget_composition()
    assert result.status == "ok"
    assert "direct_lane_unarmed" in result.detail
    assert "matches intent" in result.detail


def test_composition_invalid_source_intent_fails_loud(monkeypatch, tmp_path):
    _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env="enabled",
        usbsink_enabled=False,
        ncm=True,
        uac2=False,
    )

    def invalid(_source):
        raise RuntimeError("bad source intent")

    monkeypatch.setattr(doctor.usbsink, "source_intent_enabled", invalid)
    result = doctor.check_usbgadget_composition()

    assert result.status == "fail"
    assert "bad source intent" in result.detail


def test_composition_network_enabled_audio_parked_follower_matches(
    monkeypatch, tmp_path,
):
    """network=enabled, audio=parked_follower -> ncm.usb0 only (audio
    intent unit enabled, but a bonded follower still parks it)."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="enabled", usbsink_enabled=True,
        parked_follower=True, ncm=True, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "parked_follower" in r.detail
    assert "matches intent" in r.detail


def test_composition_network_disabled_audio_enabled_legacy_shape_matches(
    monkeypatch, tmp_path,
):
    """network=disabled, audio=enabled -> uac2.usb0 only (legacy shape)."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="disabled", usbsink_enabled=True,
        ncm=False, uac2=True,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "network=False" in r.detail
    assert "matches intent" in r.detail


def test_composition_network_disabled_audio_disabled_nothing_wanted_ok(
    monkeypatch, tmp_path,
):
    """network=disabled, audio=intent_disabled -> nothing composed, nothing
    wanted; the unit itself would have skipped via ExecCondition."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="disabled", usbsink_enabled=False,
        ncm=False, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "zero-ram" in r.detail.lower()


def test_composition_disabled_kill_switch_is_case_insensitive_and_exact(
    monkeypatch, tmp_path,
):
    """Only the exact literal 'disabled' (case-insensitive) turns network
    off; any other value (typo, unrelated string) stays enabled — mirrors
    JASPER_SHAIRPORT_SUPERVISOR / JASPER_SYSTEM_SUPERVISOR."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="DISABLED", usbsink_enabled=False,
        ncm=False, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "network=False" in r.detail

    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="off", usbsink_enabled=False,
        ncm=True, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "network=True" in r.detail


def test_composition_killswitch_whitespace_stays_enabled_matching_bash(
    monkeypatch, tmp_path,
):
    """review core-7: a whitespace-decorated ' disabled' must be treated as
    WANTED (network=True) here, matching jasper-usbgadget-up's raw (untrimmed)
    comparison. The Python doctor readers dropped .strip() so bash and Python
    agree byte-for-byte — otherwise check_usbgadget_composition would false-fail
    when bash composed ncm but Python thought the kill switch was set. The bash
    side of this parity is pinned by
    test_usbgadget_script.py::test_up_killswitch_literal_matrix."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env=" disabled ", usbsink_enabled=False,
        ncm=True, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "network=True" in r.detail


# --- mismatch cells: composed functions disagree with intent ---------


def test_composition_network_wanted_but_ncm_missing_is_fail(monkeypatch, tmp_path):
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="enabled", usbsink_enabled=True,
        ncm=False, uac2=True,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "fail"
    assert "network wanted but ncm.usb0 missing" in r.detail


def test_composition_audio_wanted_but_uac2_missing_is_fail(monkeypatch, tmp_path):
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="enabled", usbsink_enabled=True,
        ncm=True, uac2=False,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "fail"
    assert "audio wanted but uac2.usb0 missing" in r.detail


def test_composition_network_not_wanted_but_ncm_present_is_fail(monkeypatch, tmp_path):
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="disabled", usbsink_enabled=True,
        ncm=True, uac2=True,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "fail"
    assert "network not wanted but ncm.usb0 present" in r.detail


def test_composition_audio_not_wanted_but_uac2_present_is_fail(monkeypatch, tmp_path):
    """Covers the parked-follower shape at the composition level too:
    audio intent unit is enabled, but the follower park gate says no —
    yet uac2.usb0 is still (wrongly) composed."""
    _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="enabled", usbsink_enabled=True,
        parked_follower=True, ncm=True, uac2=True,
    )
    r = doctor.check_usbgadget_composition()
    assert r.status == "fail"
    assert "audio not wanted but uac2.usb0 present" in r.detail


def test_composition_nothing_wanted_but_gadget_present_is_fail(monkeypatch, tmp_path):
    """network=disabled + audio=intent_disabled but the ConfigFS gadget
    directory itself still exists (even with no function subdirs) is
    drift — nothing should be composed at all in this cell."""
    gadget = _patch_composition_env(
        monkeypatch, tmp_path,
        udc_present=True, network_env="disabled", usbsink_enabled=False,
        ncm=False, uac2=False,
    )
    gadget.mkdir(parents=True, exist_ok=True)
    r = doctor.check_usbgadget_composition()
    assert r.status == "fail"
    assert "gadget present but neither function should exist" in r.detail


def test_composition_udc_dir_read_error_treated_as_no_udc(monkeypatch, tmp_path):
    """An unreadable /sys/class/udc (OSError on iterdir) must degrade to
    the same 'no UDC' skip as a genuinely absent one, not crash the
    check."""
    udc_dir = tmp_path / "udc-not-a-dir"
    udc_dir.write_text("not a directory")
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))
    r = doctor.check_usbgadget_composition()
    assert r.status == "ok"
    assert "no udc" in r.detail.lower()


# ----------------------------------------------------------------------
# check_usb_mic_export
# ----------------------------------------------------------------------


def _usb_mic_gadget(tmp_path: Path, *, p_chmask: str, bcd_device: str) -> Path:
    gadget = tmp_path / "gadget"
    function = gadget / "functions" / "uac2.usb0"
    function.mkdir(parents=True)
    (function / "p_chmask").write_text(p_chmask + "\n")
    (gadget / "bcdDevice").write_text(bcd_device + "\n")
    return gadget


def test_usb_mic_doctor_accepts_clean_off_descriptor(monkeypatch, tmp_path):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="0", bcd_device="0x0200")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(
        doctor.usbsink,
        "read_usb_mic_intent",
        lambda: SimpleNamespace(valid=True, enabled=False, detail=""),
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "ok"
    assert "disabled" in result.detail


def test_usb_mic_doctor_rejects_stale_on_descriptor_revision(
    monkeypatch,
    tmp_path,
):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0200")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(
        doctor.usbsink,
        "read_usb_mic_intent",
        lambda: SimpleNamespace(valid=True, enabled=True, detail=""),
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_composition_wanted",
        lambda: (True, "ready"),
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "fail"
    assert "0x0210" in result.detail


def test_usb_mic_doctor_warns_when_live_relay_audio_is_stalled(
    monkeypatch,
    tmp_path,
):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0210")
    relay = tmp_path / "relay.json"
    relay.write_text(
        '{"updated_epoch_sec": 100, "audio_stalled": true, '
        '"source_stalled": true, "periods_dropped": 12, '
        '"drop_rate_periods_per_sec": 8}'
    )
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(doctor.usbsink, "RELAY_STATUS_PATH", str(relay))
    monkeypatch.setattr(doctor.usbsink.time, "time", lambda: 100.5)
    monkeypatch.setattr(
        doctor.usbsink,
        "read_usb_mic_intent",
        lambda: SimpleNamespace(valid=True, enabled=True, detail=""),
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_composition_wanted",
        lambda: (True, "ready"),
    )
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda _unit: True)
    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "warn"
    assert "stopped before" in result.detail
    assert "drop_rate=8" in result.detail


@pytest.mark.parametrize(
    (
        "host_streaming",
        "p95_ms",
        "relay_schema",
        "metric_scope",
        "expected_status",
        "expected_detail",
    ),
    [
        (True, 121.0, 4, "bridge_emit_to_alsa_write", "warn", "121.0 ms"),
        (True, 119.0, 4, "bridge_emit_to_alsa_write", "ok", "119.0 ms"),
        (True, None, 4, "bridge_emit_to_alsa_write", "warn", "not yet available"),
        (True, "bad", 4, "bridge_emit_to_alsa_write", "warn", "not yet available"),
        (True, -1.0, 4, "bridge_emit_to_alsa_write", "warn", "not yet available"),
        (
            True,
            float("nan"),
            4,
            "bridge_emit_to_alsa_write",
            "warn",
            "not yet available",
        ),
        (
            True,
            20.0,
            3,
            "bridge_emit_to_relay_dequeue",
            "warn",
            "unsupported",
        ),
        (False, 500.0, 4, "bridge_emit_to_alsa_write", "ok", "host_streaming=False"),
    ],
)
def test_usb_mic_doctor_checks_latency_only_during_active_capture(
    monkeypatch,
    tmp_path,
    host_streaming,
    p95_ms,
    relay_schema,
    metric_scope,
    expected_status,
    expected_detail,
):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0210")
    relay = tmp_path / "relay.json"
    relay.write_text(
        json.dumps(
            {
                "updated_epoch_sec": 100,
                "audio_stalled": False,
                "host_streaming": host_streaming,
                "source_age_ms_p95": p95_ms,
                "schema_version": relay_schema,
                "source_age_basis": "bridge_emit_monotonic_v2",
                "source_age_scope": metric_scope,
                "periods_dropped": 0,
            }
        )
    )
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(doctor.usbsink, "RELAY_STATUS_PATH", str(relay))
    monkeypatch.setattr(doctor.usbsink.time, "time", lambda: 100.5)
    monkeypatch.setattr(
        doctor.usbsink,
        "read_usb_mic_intent",
        lambda: SimpleNamespace(valid=True, enabled=True, detail=""),
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_audio_composition_wanted",
        lambda: (True, "ready"),
    )
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda _unit: True)

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == expected_status
    assert expected_detail in result.detail


# ----------------------------------------------------------------------
# check_usb_combo_consistency
# ----------------------------------------------------------------------


def _setup_combo(
    monkeypatch,
    tmp_path,
    *,
    failed=False,
    gadget=True,
    intent=True,
    parked=False,
    armed=False,
):
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_failed", lambda unit: failed)
    monkeypatch.setattr(_ca, "read_usb_gadget_available", lambda *a, **k: gadget)
    monkeypatch.setattr(
        doctor.usbsink,
        "source_intent_enabled",
        lambda _source: intent,
    )
    monkeypatch.setattr(
        doctor.usbsink,
        "_parked_as_bonded_follower",
        lambda: parked,
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"{_ca.USB_DIRECT_ENV_VAR}={_ca.USB_COMBO_ENABLED_VALUE}\n" if armed else ""
    )
    monkeypatch.setattr(_cr, "FANIN_ENV_PATH", str(fanin_env))


def test_combo_consistency_failed_unit_is_fail(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, failed=True)
    r = doctor.usbsink.check_usb_combo_consistency()
    assert r.status == "fail"
    assert "failed state" in r.detail


def test_combo_consistency_solo_intent_on_but_not_armed_is_warn(
    monkeypatch,
    tmp_path,
):
    # PR #1197 nit: a failed post-toggle kick leaves combo unarmed with no marker.
    _setup_combo(
        monkeypatch,
        tmp_path,
        intent=True,
        parked=False,
        armed=False,
    )
    r = doctor.usbsink.check_usb_combo_consistency()
    assert r.status == "warn"
    assert "NOT armed" in r.detail


def test_combo_consistency_follower_intent_on_disarmed_is_ok(monkeypatch, tmp_path):
    """Desired-On is intentionally disarmed while follower-role parked."""

    _setup_combo(
        monkeypatch,
        tmp_path,
        intent=True,
        parked=True,
        armed=False,
    )
    result = doctor.usbsink.check_usb_combo_consistency()
    assert result.status == "ok"
    assert "parked_follower" in result.detail


def test_combo_consistency_invalid_intent_is_fail(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=True, armed=False)

    def invalid(_source):
        raise RuntimeError("bad USB intent")

    monkeypatch.setattr(doctor.usbsink, "source_intent_enabled", invalid)
    result = doctor.usbsink.check_usb_combo_consistency()
    assert result.status == "fail"
    assert "bad USB intent" in result.detail


def test_combo_consistency_armed_coherent_is_ok(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=True, armed=True)
    r = doctor.usbsink.check_usb_combo_consistency()
    assert r.status == "ok"
    assert "combo armed" in r.detail


def test_combo_consistency_disarmed_off_is_ok(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=False, armed=False)
    r = doctor.usbsink.check_usb_combo_consistency()
    assert r.status == "ok"
    assert "disarmed" in r.detail


def test_combo_consistency_no_gadget_skips(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, gadget=False)
    r = doctor.usbsink.check_usb_combo_consistency()
    assert r.status == "ok"
    assert "not applicable" in r.detail
