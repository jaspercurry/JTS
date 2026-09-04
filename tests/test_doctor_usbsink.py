# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor usbsink domain.

The checks are hardware-side (systemctl, /proc/asound, ConfigFS, /lib/modules)
so the helpers and reads are monkeypatched; Pi-side smoke testing happens via
jasper-doctor itself. Each check is pinned by its verdict plus the token the
operator acts on (the missing function, the intent reason, the stale marker).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper import audio_runtime_plan
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
from jasper.cli import doctor
from jasper.fanin import coupling_auto as _ca

from .fanin_env_fixtures import declare_fanin_env

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


def _zero_host_role() -> UsbPortRoleState:
    """A Zero 2 W: one shared OTG port, spent on the output DAC."""
    return _role(
        board_model="Raspberry Pi Zero 2 W Rev 1.0",
        board_topology="shared_otg_port",
        desired_role="host",
        configured_role="host",
        active_role="host",
        gadget_available=False,
        reason="shared_otg_usb_output_requires_host",
        decision_reason="shared_otg_usb_output_requires_host",
        management_transport_available=False,
    )


def _pending_reboot_role() -> UsbPortRoleState:
    return _role(
        configured_role="host",
        active_role="host",
        gadget_available=False,
        reboot_required=True,
        reason="role_change_pending_reboot",
        management_transport_available=False,
    )


@pytest.fixture(autouse=True)
def _available_usb_role(monkeypatch):
    monkeypatch.setattr(doctor.usbsink, "current_usb_data_role", _role)


@pytest.mark.parametrize(
    "role, status, must_name",
    [
        (_role, "ok", "gadget available"),
        (_zero_host_role, "ok", "output dac"),
        (_pending_reboot_role, "warn", "reboot"),
    ],
    ids=["available", "zero-host", "pending-reboot"],
)
def test_check_usb_data_role_verdicts(monkeypatch, role, status, must_name):
    monkeypatch.setattr(doctor.usbsink, "current_usb_data_role", role)

    r = doctor.check_usb_data_role()

    assert r.status == status
    assert must_name in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbsink_state
# ----------------------------------------------------------------------


def _state_env(
    monkeypatch,
    tmp_path,
    *,
    active: bool,
    libcomposite: bool,
    functions: tuple[str, ...] = (),
    parked: bool = False,
):
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: active)
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: libcomposite)
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: parked)
    gadget = tmp_path / "jts-usb-audio"
    for fn in functions:
        (gadget / "functions" / fn).mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    return gadget


@pytest.mark.parametrize(
    "kwargs, status, must_name",
    [
        ({"active": False, "libcomposite": False}, "ok", "disabled"),
        # Service stopped but libcomposite still loaded: the previous stop did
        # not tear cleanly.
        ({"active": False, "libcomposite": True}, "warn", "libcomposite"),
        # uac2 composed while the readiness marker is inactive is the
        # split-brain that made /sources read off while hosts still saw JTS.
        (
            {"active": False, "libcomposite": True, "functions": ("uac2.usb0",)},
            "fail",
            "readiness marker inactive",
        ),
        # ncm alone is the hardware-conditional management network, not audio
        # drift.
        (
            {"active": False, "libcomposite": True, "functions": ("ncm.usb0",)},
            "ok",
            "composite gadget",
        ),
        ({"active": False, "libcomposite": False, "parked": True}, "ok", "parked"),
        (
            {
                "active": False,
                "libcomposite": True,
                "functions": ("ncm.usb0",),
                "parked": True,
            },
            "ok",
            "management network",
        ),
        # A parked follower should have been recomposed without uac2.
        (
            {
                "active": False,
                "libcomposite": True,
                "functions": ("uac2.usb0",),
                "parked": True,
            },
            "fail",
            "uac2.usb0 function present",
        ),
        # Parked + libcomposite but no uac2 is not audio drift by itself;
        # check_usbgadget_composition owns composite RAM-drift detection.
        ({"active": False, "libcomposite": True, "parked": True}, "ok", "parked"),
        ({"active": True, "libcomposite": True}, "fail", "uac2.usb0 is absent"),
    ],
    ids=[
        "off-clean",
        "off-libcomposite-drift",
        "off-uac2-composed",
        "off-ncm-only",
        "parked-clean",
        "parked-ncm-only",
        "parked-uac2-composed",
        "parked-module-only",
        "active-without-function",
    ],
)
def test_check_usbsink_state_verdicts(
    monkeypatch, tmp_path, kwargs, status, must_name
):
    _state_env(monkeypatch, tmp_path, **kwargs)

    r = doctor.check_usbsink_state()

    assert r.status == status
    assert must_name in r.detail.lower()


def test_check_usbsink_state_active_reads_host_connection_from_the_udc(
    monkeypatch, tmp_path
):
    _state_env(
        monkeypatch, tmp_path, active=True, libcomposite=True,
        functions=("uac2.usb0",),
    )
    controller = tmp_path / "udc" / "controller"
    controller.mkdir(parents=True)
    (controller / "state").write_text("configured\n")
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(tmp_path / "udc"))

    result = doctor.check_usbsink_state()

    assert result.status == "ok"
    assert "host_connected=True" in result.detail


# ----------------------------------------------------------------------
# check_usbsink_card
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "active, card_present, status",
    [(False, False, "ok"), (True, True, "ok"), (True, False, "fail")],
    ids=["disabled", "present", "missing"],
)
def test_check_usbsink_card_verdicts(
    monkeypatch, tmp_path, active, card_present, status
):
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: active)
    card = tmp_path / "UAC2Gadget"
    if card_present:
        card.mkdir()

    with patch.object(doctor.usbsink, "Path") as mock_path:
        mock_path.side_effect = lambda p: (
            card if p == "/proc/asound/UAC2Gadget" else Path(p)
        )
        r = doctor.check_usbsink_card()

    assert r.status == status


# ----------------------------------------------------------------------
# check_usbsink_host_stream — #3194 disclosure. The Pi cannot tell an idle
# host from a wedged ISO data path, so this check only ever reports.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "composed, card_present, rate, must_name",
    [
        (False, False, None, "uac2.usb0"),
        (True, False, None, "check_usbsink_card"),
        (True, True, None, "Capture Rate"),
        (True, True, 0, "capture_rate=0"),
        (True, True, 48000, "capture_rate=48000"),
    ],
    ids=["not-composed", "no-card", "no-control", "host-idle-or-wedged", "streaming"],
)
def test_check_usbsink_host_stream_discloses_without_judging(
    monkeypatch, tmp_path, composed, card_present, rate, must_name
):
    function_path = tmp_path / "uac2.usb0"
    if composed:
        function_path.mkdir()
    card = tmp_path / "UAC2Gadget"
    if card_present:
        card.mkdir()
    monkeypatch.setattr(doctor.usbsink, "_uac2_function_path", lambda: function_path)
    monkeypatch.setattr(doctor.usbsink, "UAC2_CARD_PATH", str(card))
    monkeypatch.setattr(doctor.usbsink, "_uac2_capture_rate", lambda: rate)

    result = doctor.check_usbsink_host_stream()

    assert result.status == "ok"
    assert must_name in result.detail


@pytest.mark.parametrize(
    "raised",
    [
        FileNotFoundError(2, "No such file or directory", "amixer"),
        subprocess.TimeoutExpired("amixer", 5.0),
    ],
    ids=["amixer-absent", "read-hung"],
)
def test_check_usbsink_host_stream_never_crashes_the_doctor(
    monkeypatch, tmp_path, raised
):
    """alsa-utils is not in install.sh's apt lists, and a wedged card can hang
    the read — the exact state this check exists to name. Driven through the
    doctor's own runner, which turns an escaping exception into a red fail."""
    function_path = tmp_path / "uac2.usb0"
    function_path.mkdir()
    card = tmp_path / "UAC2Gadget"
    card.mkdir()
    monkeypatch.setattr(doctor.usbsink, "_uac2_function_path", lambda: function_path)
    monkeypatch.setattr(doctor.usbsink, "UAC2_CARD_PATH", str(card))

    def raise_it(*args, **kwargs):
        raise raised

    monkeypatch.setattr(doctor._shared.subprocess, "run", raise_it)

    result = doctor._run_doctor_check(doctor.check_usbsink_host_stream)

    assert result.status == "ok"
    assert "not observable here" in result.detail


@pytest.mark.parametrize(
    "controls_rc, controls_out, value_rc, value_out, expected",
    [
        (1, "", 0, "", None),
        (0, "numid=3,iface=MIXER,name='PCM Capture Volume'\n", 0, "", None),
        (0, "numid=8,iface=PCM,name='Capture Rate'\n", 1, "", None),
        (
            0,
            "numid=8,iface=PCM,name='Capture Rate'\n",
            0,
            "numid=8,iface=PCM,name='Capture Rate'\n"
            "  ; type=INTEGER,access=r--v----,values=1\n  : values=48000\n",
            48000,
        ),
        (
            0,
            "numid=1,iface=MIXER,name='PCM Capture Switch'\n"
            "numid=9,iface=PCM,name='Capture Rate'\n",
            0,
            "  ; type=INTEGER,access=r--v----,values=1\n  : values=0\n",
            0,
        ),
    ],
    ids=["controls-fail", "control-absent", "cget-fail", "streaming", "resolved-numid"],
)
def test_uac2_capture_rate_resolves_the_pcm_control_by_name(
    monkeypatch, controls_rc, controls_out, value_rc, value_out, expected
):
    """The numid shifts with the composed direction set, so never pin it."""

    def fake_run(cmd, timeout=5.0):
        if "controls" in cmd:
            return SimpleNamespace(returncode=controls_rc, stdout=controls_out)
        return SimpleNamespace(returncode=value_rc, stdout=value_out)

    monkeypatch.setattr(doctor.usbsink, "_run", fake_run)

    assert doctor.usbsink._uac2_capture_rate() == expected


# ----------------------------------------------------------------------
# check_usbsink_active_libcomposite — the asymmetric mirror of the
# "service inactive + libcomposite loaded" RAM-drift check.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "active, libcomposite, status",
    [(False, False, "ok"), (True, True, "ok"), (True, False, "fail")],
    ids=["disabled", "consistent", "module-unloaded"],
)
def test_check_usbsink_active_libcomposite_verdicts(
    monkeypatch, active, libcomposite, status
):
    """A daemon active with libcomposite missing means audio cannot flow even
    though systemd thinks the unit is healthy."""
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: active)
    monkeypatch.setattr(doctor.usbsink, "_module_loaded", lambda name: libcomposite)

    assert doctor.check_usbsink_active_libcomposite().status == status


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


def test_low_latency_contract_skips_a_route_that_makes_no_claim(monkeypatch):
    plan = audio_runtime_plan.build_audio_runtime_plan(route_mode="solo")
    monkeypatch.setattr(
        audio_runtime_plan, "build_audio_runtime_plan_from_system", lambda: plan
    )

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "ok"
    assert "no USB low-latency claim" in r.detail


@pytest.mark.parametrize(
    "reason, status, must_name",
    [
        ("intent_disabled", "ok", "intent_disabled"),
        ("parked_follower", "ok", "parked_follower"),
        ("intent_invalid:bad token", "fail", "bad token"),
    ],
    ids=["user-off", "parked", "invalid-intent"],
)
def test_low_latency_contract_reports_why_usb_audio_is_not_wanted(
    monkeypatch, reason, status, must_name
):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(doctor.usbsink, "_audio_wanted", lambda: (False, reason))
    monkeypatch.setattr(
        doctor.usbsink,
        "read_status_socket",
        lambda _path: (_ for _ in ()).throw(AssertionError("must not probe fan-in")),
    )

    result = doctor.check_usbsink_low_latency_contract()

    assert result.status == status
    assert must_name in result.detail


def _claiming_route(monkeypatch, status_reader):
    monkeypatch.setattr(doctor.usbsink, "_audio_wanted", lambda: (True, "enabled"))
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(doctor.usbsink, "read_status_socket", status_reader)


def test_low_latency_contract_requires_a_readable_fanin_status(monkeypatch):
    _claiming_route(
        monkeypatch,
        lambda _path: (_ for _ in ()).throw(OSError("socket unavailable")),
    )

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "fan-in STATUS" in r.detail


def test_low_latency_contract_warns_when_the_kernel_hides_the_attrs(
    monkeypatch, tmp_path
):
    _claiming_route(monkeypatch, lambda _path: _fanin_direct_status())
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "warn"
    assert "kernel does not expose" in r.detail


def test_low_latency_contract_fails_on_a_direct_period_mismatch(monkeypatch):
    _claiming_route(
        monkeypatch,
        lambda _path: _fanin_direct_status(health="capturing", period_frames=128),
    )

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "period_frames" in r.detail


def test_low_latency_contract_fails_on_a_mismatched_exposed_attr(
    monkeypatch, tmp_path
):
    _claiming_route(monkeypatch, lambda _path: _fanin_direct_status())
    function_path = tmp_path / "gadget" / "functions" / "uac2.usb0"
    function_path.mkdir(parents=True)
    (function_path / "c_sync").write_text("adaptive\n")
    (function_path / "req_number").write_text("2\n")
    (function_path / "c_hs_bint").write_text("1\n")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", tmp_path / "gadget")

    r = doctor.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert "c_sync" in r.detail


# ----------------------------------------------------------------------
# check_usbsink_name — host-visible device name patch state
# ----------------------------------------------------------------------

_KVER = "6.12.0-test"
_MARKER = f"3\t{_KVER}\tKitchen\tKitchen Mic\tdeadbeef"
_PATCHED = b"\x7fELF Kitchen\x00 patched body, no stock token"


def _name_env(monkeypatch, *, active: bool, speaker: str = "Kitchen"):
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda unit: active)
    monkeypatch.setattr(
        doctor.os, "uname", lambda: type("U", (), {"release": _KVER})()
    )
    monkeypatch.setattr("jasper.speaker_name.runtime_name", lambda: speaker)


def _write_override(root: Path, body: bytes, marker: str | None) -> None:
    updates = root / _KVER / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    (updates / "usb_f_uac2.ko").write_bytes(body)
    if marker is not None:
        (updates / ".jasper-usbsink-name.marker").write_text(marker)


@pytest.mark.parametrize(
    "active, speaker, body, marker, status, must_name",
    [
        (False, "Kitchen", None, None, "ok", "skipped"),
        (True, "Kitchen", None, None, "warn", "no name-patched module override"),
        # Override present but never actually patched.
        (
            True,
            "Kitchen",
            b"\x7fELF" + b"Playback Inactive\x00Capture Inactive\x00rest",
            _MARKER,
            "warn",
            "stock string",
        ),
        (
            True,
            "Kitchen",
            b"\x7fELF Kitchen\x00Kitchen Mic\x00Capture Active\x00rest",
            _MARKER,
            "warn",
            "stock string",
        ),
        (True, "Kitchen", _PATCHED, _MARKER, "ok", "Kitchen"),
        # Patched for an older name; the speaker has since been renamed.
        (True, "Living Room", _PATCHED, _MARKER, "warn", "stale"),
        (True, "Kitchen", _PATCHED, None, "warn", "stale"),
        # A playback-only marker is the pre-mic schema.
        (True, "Kitchen", _PATCHED, f"{_KVER}\tKitchen\tdeadbeef", "warn", "stale"),
    ],
    ids=[
        "disabled",
        "no-override",
        "stock-inactive-strings",
        "stock-active-capture-string",
        "patched",
        "renamed-speaker",
        "no-marker",
        "playback-only-schema",
    ],
)
def test_check_usbsink_name_verdicts(
    monkeypatch, tmp_path, active, speaker, body, marker, status, must_name
):
    _name_env(monkeypatch, active=active, speaker=speaker)
    if body is not None:
        _write_override(tmp_path, body, marker)

    r = doctor.check_usbsink_name(modules_root=str(tmp_path))

    assert r.status == status
    assert must_name.lower() in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbgadget_composition — composed gadget functions vs composed
# *intent* (network kill switch x audio enablement x follower-park gate).
# The composite-era replacement for the old "libcomposite loaded <=>
# usbsink active" invariant; the table below walks every cell of the truth
# table in jasper-usbgadget-up.
# ----------------------------------------------------------------------


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

    # With neither function requested this path does not exist on disk (the
    # "no gadget directory at all" cell); the present-but-empty cell creates
    # it explicitly afterwards.
    gadget = tmp_path / "jts-usb-audio"
    for want, name in ((ncm, "ncm.usb0"), (uac2, "uac2.usb0")):
        if want:
            (gadget / "functions" / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)

    monkeypatch.setattr(
        doctor.usbsink, "source_intent_enabled", lambda _source: usbsink_enabled
    )
    monkeypatch.setattr(
        doctor.usbsink, "_parked_as_bonded_follower", lambda: parked_follower
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


@pytest.mark.parametrize(
    "env, status, must_name",
    [
        # Fresh install pre-reboot: the unit skips via jasper-usbgadget-wanted
        # and check_usb_data_role owns the reboot prompt.
        (
            {"udc_present": False, "network_env": "enabled", "usbsink_enabled": True},
            "ok",
            "no udc",
        ),
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "ncm": True,
                "uac2": True,
            },
            "ok",
            "matches intent",
        ),
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": False,
                "ncm": True,
            },
            "ok",
            "intent_disabled",
        ),
        # ADR-0191: a disabled lifecycle mirror no longer suppresses UAC2.
        # Derived state is a consequence of intent, never a precondition.
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "lifecycle_ready": False,
                "ncm": True,
                "uac2": True,
            },
            "ok",
            "enabled",
        ),
        # ADR-0191: nor does an unarmed DIRECT lane. The endpoint stays
        # advertised and the consumer state is disclosed instead.
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "lifecycle_ready": True,
                "direct_ready": False,
                "ncm": True,
                "uac2": True,
            },
            "ok",
            "consumed=False",
        ),
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "parked_follower": True,
                "ncm": True,
            },
            "ok",
            "parked_follower",
        ),
        # Legacy shape: network off, audio on.
        (
            {
                "udc_present": True,
                "network_env": "disabled",
                "usbsink_enabled": True,
                "uac2": True,
            },
            "ok",
            "network=False",
        ),
        (
            {
                "udc_present": True,
                "network_env": "disabled",
                "usbsink_enabled": False,
            },
            "ok",
            "zero-ram",
        ),
        # Mismatch cells: composed functions disagree with intent.
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "uac2": True,
            },
            "fail",
            "network wanted but ncm.usb0 missing",
        ),
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "ncm": True,
            },
            "fail",
            "audio wanted but uac2.usb0 missing",
        ),
        (
            {
                "udc_present": True,
                "network_env": "disabled",
                "usbsink_enabled": True,
                "ncm": True,
                "uac2": True,
            },
            "fail",
            "network not wanted but ncm.usb0 present",
        ),
        # The parked-follower shape at the composition level.
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "parked_follower": True,
                "ncm": True,
                "uac2": True,
            },
            "fail",
            "audio not wanted but uac2.usb0 present",
        ),
    ],
    ids=[
        "no-udc",
        "both-wanted",
        "network-only",
        "derived-unit-disabled-still-composes",
        "unarmed-lane-still-composes",
        "parked-follower",
        "legacy-audio-only",
        "nothing-wanted",
        "ncm-missing",
        "uac2-missing",
        "ncm-unwanted",
        "uac2-unwanted",
    ],
)
def test_check_usbgadget_composition_verdicts(
    monkeypatch, tmp_path, env, status, must_name
):
    _patch_composition_env(monkeypatch, tmp_path, **env)

    r = doctor.check_usbgadget_composition()

    assert r.status == status
    assert must_name.lower() in r.detail.lower()


@pytest.mark.parametrize(
    "network_env, expected",
    [
        ("DISABLED", "network=False"),
        ("off", "network=True"),
        # review core-7: whitespace-decorated ' disabled ' stays WANTED,
        # matching jasper-usbgadget-up's raw (untrimmed) comparison. The bash
        # side is pinned by test_usbgadget_script.py's literal matrix.
        (" disabled ", "network=True"),
    ],
    ids=["uppercase", "other-word", "whitespace"],
)
def test_composition_kill_switch_matches_only_the_exact_literal(
    monkeypatch, tmp_path, network_env, expected
):
    _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env=network_env,
        usbsink_enabled=False,
        ncm=expected == "network=True",
    )

    r = doctor.check_usbgadget_composition()

    assert r.status == "ok"
    assert expected in r.detail


def test_composition_gadget_dir_with_no_functions_is_still_drift(
    monkeypatch, tmp_path
):
    """Nothing should be composed at all in this cell — even an empty
    ConfigFS gadget directory is leftover state."""
    gadget = _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env="disabled",
        usbsink_enabled=False,
    )
    gadget.mkdir(parents=True, exist_ok=True)

    r = doctor.check_usbgadget_composition()

    assert r.status == "fail"
    assert "gadget present but neither function should exist" in r.detail


def test_composition_unreadable_udc_dir_degrades_to_the_no_udc_skip(
    monkeypatch, tmp_path
):
    udc_dir = tmp_path / "udc-not-a-dir"
    udc_dir.write_text("not a directory")
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))

    r = doctor.check_usbgadget_composition()

    assert r.status == "ok"
    assert "no udc" in r.detail.lower()


def test_composition_invalid_source_intent_fails_loud(monkeypatch, tmp_path):
    _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env="enabled",
        usbsink_enabled=False,
        ncm=True,
    )

    def invalid(_source):
        raise RuntimeError("bad source intent")

    monkeypatch.setattr(doctor.usbsink, "source_intent_enabled", invalid)

    result = doctor.check_usbgadget_composition()

    assert result.status == "fail"
    assert "bad source intent" in result.detail


def test_composition_retains_ncm_during_a_pending_host_reboot(
    monkeypatch, tmp_path
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
        doctor.usbsink, "_audio_wanted", lambda: (False, "intent_disabled")
    )

    result = doctor.check_usbgadget_composition()

    assert result.status == "warn"
    assert "retained" in result.detail.lower()


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


def _mic_intent(monkeypatch, *, valid=True, enabled=True, detail=""):
    monkeypatch.setattr(
        doctor.usbsink,
        "read_usb_mic_intent",
        lambda: SimpleNamespace(valid=valid, enabled=enabled, detail=detail),
    )


def test_usb_mic_export_accepts_a_clean_off_descriptor(monkeypatch, tmp_path):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="0", bcd_device="0x0200")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    _mic_intent(monkeypatch, enabled=False)

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "ok"
    assert "disabled" in result.detail


def test_usb_mic_export_skips_intent_when_the_gadget_is_unavailable(monkeypatch):
    """Unavailable hardware must gate the durable intent read entirely."""
    monkeypatch.setattr(doctor.usbsink, "current_usb_data_role", _zero_host_role)
    monkeypatch.setattr(
        doctor.usbsink,
        "read_usb_mic_intent",
        lambda: (_ for _ in ()).throw(AssertionError("intent must not be read")),
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "ok"
    assert "single USB data port" in result.detail


def test_usb_mic_export_rejects_a_missing_intent_when_the_gadget_is_available(
    monkeypatch,
):
    _mic_intent(
        monkeypatch,
        valid=False,
        enabled=False,
        detail="USB microphone preference is missing.",
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "fail"
    assert result.detail == "USB microphone preference is missing."


def test_usb_mic_export_rejects_a_stale_descriptor_revision(monkeypatch, tmp_path):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0200")
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    _mic_intent(monkeypatch)
    monkeypatch.setattr(
        doctor.usbsink, "_audio_wanted", lambda: (True, "ready")
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "fail"
    assert "0x0210" in result.detail


def _relay_mic_env(monkeypatch, tmp_path, payload: dict):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0210")
    relay = tmp_path / "relay.json"
    relay.write_text(json.dumps(payload))
    monkeypatch.setattr(doctor.usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(doctor.usbsink, "RELAY_STATUS_PATH", str(relay))
    monkeypatch.setattr(doctor.usbsink.time, "time", lambda: 100.5)
    _mic_intent(monkeypatch)
    monkeypatch.setattr(
        doctor.usbsink, "_audio_wanted", lambda: (True, "ready")
    )
    monkeypatch.setattr(doctor.usbsink, "_systemd_is_active", lambda _unit: True)


def test_usb_mic_export_warns_when_the_live_relay_audio_is_stalled(
    monkeypatch, tmp_path
):
    _relay_mic_env(
        monkeypatch,
        tmp_path,
        {
            "updated_epoch_sec": 100,
            "audio_stalled": True,
            "source_stalled": True,
            "periods_dropped": 12,
            "drop_rate_periods_per_sec": 8,
        },
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == "warn"
    assert "drop_rate=8" in result.detail


@pytest.mark.parametrize(
    "host_streaming, p95_ms, relay_schema, metric_scope, status, must_name",
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
        (True, 20.0, 3, "bridge_emit_to_relay_dequeue", "warn", "unsupported"),
        (False, 500.0, 4, "bridge_emit_to_alsa_write", "ok", "host_streaming=False"),
    ],
    ids=[
        "over-budget",
        "in-budget",
        "absent",
        "non-numeric",
        "negative",
        "nan",
        "old-schema",
        "not-streaming",
    ],
)
def test_usb_mic_export_checks_latency_only_during_active_capture(
    monkeypatch,
    tmp_path,
    host_streaming,
    p95_ms,
    relay_schema,
    metric_scope,
    status,
    must_name,
):
    _relay_mic_env(
        monkeypatch,
        tmp_path,
        {
            "updated_epoch_sec": 100,
            "audio_stalled": False,
            "host_streaming": host_streaming,
            "source_age_ms_p95": p95_ms,
            "schema_version": relay_schema,
            "source_age_basis": "bridge_emit_monotonic_v2",
            "source_age_scope": metric_scope,
            "periods_dropped": 0,
        },
    )

    result = doctor.usbsink.check_usb_mic_export()

    assert result.status == status
    assert must_name in result.detail


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
        doctor.usbsink, "source_intent_enabled", lambda _source: intent
    )
    monkeypatch.setattr(doctor.usbsink, "_parked_as_bonded_follower", lambda: parked)
    declare_fanin_env(
        monkeypatch,
        tmp_path,
        f"{_ca.USB_DIRECT_ENV_VAR}={_ca.USB_COMBO_ENABLED_VALUE}\n" if armed else "",
    )


@pytest.mark.parametrize(
    "kwargs, status, must_name",
    [
        ({"failed": True}, "fail", "failed state"),
        # A failed post-toggle kick leaves combo unarmed with no marker.
        ({"intent": True, "armed": False}, "warn", "NOT armed"),
        # Desired-On is intentionally disarmed while follower-role parked.
        ({"intent": True, "parked": True, "armed": False}, "ok", "parked_follower"),
        ({"intent": True, "armed": True}, "ok", "combo armed"),
        ({"intent": False, "armed": False}, "ok", "disarmed"),
        ({"gadget": False}, "ok", "not applicable"),
    ],
    ids=["failed-unit", "intent-on-unarmed", "parked", "armed", "off", "no-gadget"],
)
def test_check_usb_combo_consistency_verdicts(
    monkeypatch, tmp_path, kwargs, status, must_name
):
    _setup_combo(monkeypatch, tmp_path, **kwargs)

    r = doctor.usbsink.check_usb_combo_consistency()

    assert r.status == status
    assert must_name in r.detail


def test_check_usb_combo_consistency_invalid_intent_is_fail(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=True, armed=False)

    def invalid(_source):
        raise RuntimeError("bad USB intent")

    monkeypatch.setattr(doctor.usbsink, "source_intent_enabled", invalid)

    result = doctor.usbsink.check_usb_combo_consistency()

    assert result.status == "fail"
    assert "bad USB intent" in result.detail
