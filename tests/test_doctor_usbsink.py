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
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from jasper import audio_runtime_plan
from jasper.cli import doctor
from jasper.fanin import combo_health as _ch
from jasper.fanin import coupling_auto as _ca
from jasper.fanin import coupling_reconcile as _cr


# ----------------------------------------------------------------------
# check_usbsink_dtoverlay
# ----------------------------------------------------------------------


def test_usbsink_dtoverlay_present(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "[pi5]\ndtoverlay=dwc2,dr_mode=peripheral\ncountry=US\n",
    )
    # Patch Path resolution by patching the literal in the function.
    with patch.object(doctor.usbsink, "Path", autospec=True) as mock_path:
        mock_path.side_effect = lambda p: cfg if p == "/boot/firmware/config.txt" else Path(p)
        r = doctor.check_usbsink_dtoverlay()
    assert r.status == "ok"
    assert "enabled" in r.detail.lower()


def test_usbsink_dtoverlay_missing_returns_warn(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("[pi5]\ncountry=US\n")  # no dtoverlay
    with patch.object(doctor.usbsink, "Path") as mock_path:
        mock_path.side_effect = lambda p: cfg if p == "/boot/firmware/config.txt" else Path(p)
        r = doctor.check_usbsink_dtoverlay()
    assert r.status == "warn"
    assert "re-run" in r.detail.lower() or "reboot" in r.detail.lower()


def test_usbsink_dtoverlay_missing_config_file(monkeypatch, tmp_path):
    """Not on a Pi → config.txt missing → warn (not a fail)."""
    nonexistent = tmp_path / "config.txt"
    with patch.object(doctor.usbsink, "Path") as mock_path:
        mock_path.side_effect = lambda p: nonexistent if p == "/boot/firmware/config.txt" else Path(p)
        r = doctor.check_usbsink_dtoverlay()
    assert r.status == "warn"


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
    """Computers seeing USB audio while the bridge is inactive is not RAM drift.

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
    assert "bridge inactive" in r.detail.lower()


def test_usbsink_state_disabled_gadget_present_ncm_only_is_ok(monkeypatch, tmp_path):
    """The composite gadget legitimately persists for the always-on USB
    management network even when USB Audio Input is off — a ConfigFS dir
    carrying only ncm.usb0 (no uac2.usb0) must NOT be treated as drift."""
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
    """A parked follower's gadget dir may still legitimately carry
    ncm.usb0 for the always-on management network — the ok detail should
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
    """Parked + uac2.usb0 absent + libcomposite still loaded is no longer
    RAM drift by itself in the composite model: libcomposite legitimately
    stays resident whenever the gadget carries ncm.usb0 for the always-on
    network. check_usbgadget_composition (not this check) owns genuine
    RAM-drift detection for the composite gadget as a whole."""
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "missing")
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: True)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "ok"
    assert "parked" in r.detail.lower()


def test_usbsink_state_active_no_state_file(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    nonexistent = tmp_path / "state.json"
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return nonexistent
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "fail"
    assert "missing" in r.detail.lower()


def test_usbsink_state_active_fresh_state(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    # Very recent (within 1 s of now).
    now = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps({
        "playing": True, "preempted": False,
        "host_connected": True, "rms_dbfs": -14.2,
        "updated_at": now,
    }))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "ok"
    assert "active" in r.detail.lower()
    assert "playing=True" in r.detail


def test_usbsink_state_active_combo_standby_reports_combo_not_dead_numbers(
    monkeypatch, tmp_path,
):
    """On a combo box the bridge runs in standby and publishes frozen idle
    playing=false / rms_dbfs=-120. The check must report combo mode instead of
    those meaningless numbers, so it doesn't read as 'USB connected but silent'
    while fan-in's direct lane plays — matching the honest /state projection."""
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "standby": True,
        "playing": False, "preempted": False,
        "host_connected": True, "rms_dbfs": -120.0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "ok"
    assert "combo mode" in r.detail.lower()
    assert "host_connected=True" in r.detail
    # The dead standby-bridge numbers must NOT be presented as measured.
    assert "playing=False" not in r.detail
    assert "-120" not in r.detail


def test_usbsink_state_active_null_rms_is_ok(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "playing": False, "preempted": False,
        "host_connected": True, "rms_dbfs": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "ok"
    assert "rms_dbfs=unknown" in r.detail


def test_usbsink_state_active_malformed_rms_is_warn(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "playing": False, "preempted": False,
        "host_connected": True, "rms_dbfs": "quiet",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "warn"
    assert "rms_dbfs not numeric" in r.detail


def test_usbsink_state_active_stale_state_is_warn(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    # 30 seconds old → way past the 10-s tolerance.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    state_path.write_text(json.dumps({
        "playing": False, "preempted": False, "host_connected": False,
        "rms_dbfs": -120.0,
        "updated_at": stale,
    }))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "warn"
    assert "stale" in r.detail.lower()


def test_usbsink_state_active_corrupt_state_is_fail(monkeypatch, tmp_path):
    _patch_active(monkeypatch, True)
    _patch_libcomp_loaded(monkeypatch, True)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid")
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path
        r = doctor.check_usbsink_state()
    assert r.status == "fail"
    assert "parse" in r.detail.lower()


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


def test_usbsink_low_latency_contract_requires_rust_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"implementation": "python"}))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path

        r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "implementation='rust'" in r.detail


def test_usbsink_low_latency_contract_warns_missing_optional_attrs(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "implementation": "rust",
        "period_frames": 256,
        "ring": {"fill_periods": 1, "capacity_periods": 3},
        "counters": {},
    }))
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path

        r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "warn"
    assert "kernel does not expose" in r.detail


def test_usbsink_low_latency_contract_fails_bridge_period_mismatch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "implementation": "rust",
        "period_frames": 128,
        "ring": {"fill_periods": 1, "capacity_periods": 2},
        "counters": {},
    }))
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path

        r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "period_frames" in r.detail
    assert "ring_periods" in r.detail


def test_usbsink_low_latency_contract_fails_mismatched_exposed_attr(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "implementation": "rust",
        "period_frames": 256,
        "ring": {"fill_periods": 1, "capacity_periods": 3},
        "counters": {},
    }))
    function_path = tmp_path / "gadget" / "functions" / "uac2.usb0"
    function_path.mkdir(parents=True)
    (function_path / "c_sync").write_text("adaptive\n")
    (function_path / "req_number").write_text("2\n")
    (function_path / "c_hs_bint").write_text("1\n")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "gadget")
    with patch.object(doctor.usbsink, "Path") as mock_path:
        def _path(p):
            if p == "/run/jasper-usbsink/state.json":
                return state_path
            return Path(p)
        mock_path.side_effect = _path

        r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "c_sync" in r.detail


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
        b"\x7fELF" + b"Playback Inactive\x00rest",
        marker=f"{_KVER}\tKitchen\tdeadbeef",
    )
    r = doctor.check_usbsink_name(modules_root=str(tmp_path))
    assert r.status == "warn"
    assert "stock string" in r.detail


def test_usbsink_name_ok_when_patched_and_marker_matches(monkeypatch, tmp_path):
    _name_env(monkeypatch, active=True, speaker="Kitchen")
    _write_override(
        tmp_path,
        b"\x7fELF Kitchen\x00 patched body, no stock token",
        marker=f"{_KVER}\tKitchen\tdeadbeef",
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
        marker=f"{_KVER}\tKitchen\tdeadbeef",
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

    def _run_stub(cmd, timeout=5.0):
        if cmd[:3] == ["systemctl", "is-enabled", "--quiet"]:
            return type(
                "P", (), {"returncode": 0 if usbsink_enabled else 1, "stdout": "", "stderr": ""},
            )()
        raise AssertionError(f"unexpected _run call in test: {cmd}")

    monkeypatch.setattr(doctor.usbsink, "_run", _run_stub)
    monkeypatch.setattr(
        doctor.usbsink, "_parked_as_bonded_follower", lambda: parked_follower,
    )
    return gadget


def test_composition_no_udc_is_ok_skip(monkeypatch, tmp_path):
    """Fresh install pre-reboot (no UDC yet) must never fail — the unit
    itself skips cleanly via jasper-usbgadget-wanted, and
    check_usbsink_dtoverlay already owns telling the user to reboot."""
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


# --- check_usbsink_env_drift (defect D: stale env after rate-limited restart) ---


def _stage_env_drift(monkeypatch, tmp_path, *, mtime_epoch, start_epoch, wanted=True, active=True):
    """Point the drift check at a tmp usbsink.env with a controlled mtime, and stub
    the daemon start epoch + wanted/active gates. Returns the env Path."""
    import os as _os

    import jasper.fanin.coupling_reconcile as cr

    env = tmp_path / "usbsink.env"
    env.write_text("JASPER_USBSINK_ROUTE=direct\n", encoding="utf-8")
    _os.utime(env, (mtime_epoch, mtime_epoch))
    monkeypatch.setattr(cr, "USBSINK_ENV_PATH", str(env))
    monkeypatch.setattr(doctor.usbsink, "_audio_wanted", lambda: (wanted, "enabled" if wanted else "intent_disabled"))
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: active)
    monkeypatch.setattr(doctor.usbsink, "_unit_main_start_epoch", lambda unit: start_epoch)
    return env


def test_usbsink_env_drift_warns_when_env_newer_than_daemon(monkeypatch, tmp_path):
    # The defect-D state: reconciler rewrote usbsink.env 100 s AFTER the daemon
    # started (its try-restart was rate-limited), so the daemon serves stale route
    # geometry with no auto-retry. The check must surface that as a warn.
    _stage_env_drift(monkeypatch, tmp_path, mtime_epoch=1_000_100.0, start_epoch=1_000_000.0)
    r = doctor.check_usbsink_env_drift()
    assert r.status == "warn"
    assert "stale route env" in r.detail
    assert "systemctl restart" in r.detail


def test_usbsink_env_drift_ok_when_daemon_started_after_env(monkeypatch, tmp_path):
    # Converged box: the daemon started AFTER the last env write (the normal
    # reconcile write-then-restart ordering) → running the live env, no drift.
    _stage_env_drift(monkeypatch, tmp_path, mtime_epoch=1_000_000.0, start_epoch=1_000_050.0)
    r = doctor.check_usbsink_env_drift()
    assert r.status == "ok"
    assert "running live route env" in r.detail


def test_usbsink_env_drift_ok_within_slack(monkeypatch, tmp_path):
    # Env newer than start by less than the slack (write→restart in the same few
    # seconds) is NOT flagged — avoids a false positive on the converged path.
    _stage_env_drift(monkeypatch, tmp_path, mtime_epoch=1_000_002.0, start_epoch=1_000_000.0)
    r = doctor.check_usbsink_env_drift()
    assert r.status == "ok"


def test_usbsink_env_drift_skips_when_not_wanted(monkeypatch, tmp_path):
    # USB Audio disabled / parked → env is irrelevant, skip cleanly.
    _stage_env_drift(
        monkeypatch, tmp_path, mtime_epoch=1_000_100.0, start_epoch=1_000_000.0, wanted=False
    )
    r = doctor.check_usbsink_env_drift()
    assert r.status == "ok"
    assert "not active" in r.detail


def test_usbsink_env_drift_skips_when_inactive(monkeypatch, tmp_path):
    # Enabled but not currently active → nothing running to drift; skip.
    _stage_env_drift(
        monkeypatch, tmp_path, mtime_epoch=1_000_100.0, start_epoch=1_000_000.0, active=False
    )
    r = doctor.check_usbsink_env_drift()
    assert r.status == "ok"
    assert "not active" in r.detail


def test_usbsink_env_drift_skips_when_start_time_unavailable(monkeypatch, tmp_path):
    # Indeterminate daemon start → skip rather than guess.
    _stage_env_drift(
        monkeypatch, tmp_path, mtime_epoch=1_000_100.0, start_epoch=None
    )
    r = doctor.check_usbsink_env_drift()
    assert r.status == "ok"
    assert "start time unavailable" in r.detail


def test_usbsink_env_drift_ok_when_env_absent(monkeypatch, tmp_path):
    # No env file → daemon runs on defaults, nothing to drift from.
    import jasper.fanin.coupling_reconcile as cr

    monkeypatch.setattr(cr, "USBSINK_ENV_PATH", str(tmp_path / "does-not-exist.env"))
    monkeypatch.setattr(doctor.usbsink, "_audio_wanted", lambda: (True, "enabled"))
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: True)
    r = doctor.check_usbsink_env_drift()
    assert r.status == "ok"
    assert "absent" in r.detail


def test_unit_main_start_epoch_parses_monotonic_plus_uptime(monkeypatch):
    # _unit_main_start_epoch converts systemd's ExecMainStartTimestampMonotonic (µs
    # since boot) + /proc/uptime into a wall-clock epoch. boot_epoch = now - uptime;
    # start = boot + start_us/1e6.
    import subprocess
    import time as _time

    from jasper.cli.doctor import usbsink as us

    monkeypatch.setattr(
        us, "_run",
        lambda cmd, timeout=5.0: subprocess.CompletedProcess(cmd, 0, "5000000\n", ""),
    )  # 5_000_000 µs = 5 s since boot
    monkeypatch.setattr(_time, "time", lambda: 2_000.0)

    import builtins

    real_open = builtins.open

    def _fake_open(path, *a, **k):
        if str(path) == "/proc/uptime":
            import io
            return io.StringIO("100.0 50.0\n")  # 100 s of uptime
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _fake_open)
    # boot_epoch = 2000 - 100 = 1900; start = 1900 + 5 = 1905.
    assert us._unit_main_start_epoch("jasper-usbsink.service") == 1905.0


def test_unit_main_start_epoch_none_when_never_started(monkeypatch):
    import subprocess

    from jasper.cli.doctor import usbsink as us

    monkeypatch.setattr(
        us, "_run",
        lambda cmd, timeout=5.0: subprocess.CompletedProcess(cmd, 0, "0\n", ""),
    )
    assert us._unit_main_start_epoch("jasper-usbsink.service") is None


# ----------------------------------------------------------------------
# check_usb_combo_fallback (defect 2026-07-10)
# ----------------------------------------------------------------------


def _setup_combo(monkeypatch, tmp_path, *, failed=False, marker=None,
                 gadget=True, intent=True, armed=False):
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_failed", lambda unit: failed)
    monkeypatch.setattr(_ch, "read_fallback_marker", lambda *a, **k: marker)
    monkeypatch.setattr(_ca, "read_boot_config_gadget_present", lambda *a, **k: gadget)

    def _fake_run(cmd, timeout=5.0):
        rc = 0 if intent else 1
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(doctor.usbsink, "_run", _fake_run)
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"{_ca.USB_DIRECT_ENV_VAR}={_ca.USB_COMBO_ENABLED_VALUE}\n" if armed else ""
    )
    monkeypatch.setattr(_cr, "FANIN_ENV_PATH", str(fanin_env))


def test_combo_fallback_failed_unit_is_fail(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, failed=True)
    r = doctor.usbsink.check_usb_combo_fallback()
    assert r.status == "fail"
    assert "failed state" in r.detail


def test_combo_fallback_marker_present_is_warn(monkeypatch, tmp_path):
    marker = _ch.FallbackMarker(reason="direct capture broke", at_epoch=1.0)
    _setup_combo(monkeypatch, tmp_path, marker=marker)
    r = doctor.usbsink.check_usb_combo_fallback()
    assert r.status == "warn"
    assert "USB audio unavailable" in r.detail
    assert "no aloop solo fallback" in r.detail
    assert "direct capture broke" in r.detail


def test_combo_fallback_intent_on_but_not_armed_is_warn(monkeypatch, tmp_path):
    # PR #1197 nit: a failed post-toggle kick leaves combo unarmed with no marker.
    _setup_combo(monkeypatch, tmp_path, intent=True, armed=False, marker=None)
    r = doctor.usbsink.check_usb_combo_fallback()
    assert r.status == "warn"
    assert "NOT armed" in r.detail


def test_combo_fallback_armed_coherent_is_ok(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=True, armed=True)
    r = doctor.usbsink.check_usb_combo_fallback()
    assert r.status == "ok"
    assert "combo armed" in r.detail


def test_combo_fallback_disarmed_off_is_ok(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=False, armed=False)
    r = doctor.usbsink.check_usb_combo_fallback()
    assert r.status == "ok"
    assert "disarmed" in r.detail


def test_combo_fallback_no_gadget_skips(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, gadget=False)
    r = doctor.usbsink.check_usb_combo_fallback()
    assert r.status == "ok"
    assert "not applicable" in r.detail
