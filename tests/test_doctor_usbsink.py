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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from jasper import audio_runtime_plan
from jasper.cli import doctor


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

    The gadget itself is advertised, so doctor must hard-fail the split-brain
    state that made /sources look off while hosts still saw JTS.
    """
    gadget = tmp_path / "jts-usb-audio"
    gadget.mkdir()
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    _patch_active(monkeypatch, False)
    _patch_libcomp_loaded(monkeypatch, True)

    r = doctor.check_usbsink_state()

    assert r.status == "fail"
    assert "advertised" in r.detail.lower()
    assert "bridge inactive" in r.detail.lower()


def test_usbsink_state_parked_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "missing")
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: False)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "ok"
    assert "parked" in r.detail.lower()
    assert "gadget down" in r.detail.lower()


def test_usbsink_state_parked_with_gadget_is_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    gadget = tmp_path / "jts-usb-audio"
    gadget.mkdir()
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: True)

    def active(unit: str) -> bool:
        return unit == doctor.usbsink.USBSINK_INIT_UNIT

    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", active)

    r = doctor.check_usbsink_state()

    assert r.status == "fail"
    assert "parked" in r.detail.lower()
    assert "advertised" in r.detail.lower()
    assert doctor.usbsink.USBSINK_INIT_UNIT in r.detail


def test_usbsink_state_parked_module_only_is_warn(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "missing")
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: True)
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: False)

    r = doctor.check_usbsink_state()

    assert r.status == "warn"
    assert "libcomposite" in r.detail.lower()


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
# check_usbsink_preempt_port_reachable — catches drift between
# mux.USBSINK_PREEMPT_PORT and preempt_listener.DEFAULT_PORT.
# ----------------------------------------------------------------------


def test_preempt_port_disabled_skip(monkeypatch):
    _patch_active(monkeypatch, False)
    r = doctor.check_usbsink_preempt_port_reachable()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower()


def test_preempt_port_invalid_env_returns_fail(monkeypatch):
    _patch_active(monkeypatch, True)
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT_PORT", "not-a-number")
    r = doctor.check_usbsink_preempt_port_reachable()
    assert r.status == "fail"
    assert "integer" in r.detail.lower()


def test_preempt_port_unreachable_is_fail(monkeypatch):
    """When the daemon claims to be up but nothing's listening on
    the configured port, mux's preempt POSTs would fail silently —
    catch it here."""
    _patch_active(monkeypatch, True)
    # Use a port that's overwhelmingly unlikely to be in use (and
    # we'd rather a 500ms timeout once than a flaky test).
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT_PORT", "1")  # privileged port = ConnectionRefused
    r = doctor.check_usbsink_preempt_port_reachable()
    assert r.status == "fail"
    assert "preempt" in r.detail.lower() or "reachable" in r.detail.lower()


def test_preempt_port_reachable_is_ok(monkeypatch):
    """When something IS listening (we bind our own socket for the
    test), the check returns ok with the host:port in the detail."""
    import socket
    _patch_active(monkeypatch, True)
    # Bind a transient socket on an ephemeral port.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    _, port = s.getsockname()
    monkeypatch.setenv("JASPER_USBSINK_PREEMPT_PORT", str(port))
    try:
        r = doctor.check_usbsink_preempt_port_reachable()
    finally:
        s.close()
    assert r.status == "ok"
    assert str(port) in r.detail


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
