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
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper import audio_runtime_plan
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
from jasper.cli.doctor import _evidence, _shared, usbsink
from jasper.cli.doctor._evidence import evidence
from jasper.fanin import coupling_auto as _ca
from .doctor_test_support import _fake_unit_states
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
    monkeypatch.setattr(usbsink, "current_usb_data_role", _role)


@pytest.mark.parametrize(
    "role, status, reason",
    [
        (_role, "ok", ""),
        (_zero_host_role, "ok", usbsink.REASON_DATA_ROLE_HOST_ONLY),
        (_pending_reboot_role, "warn", usbsink.REASON_DATA_ROLE_REBOOT_REQUIRED),
    ],
    ids=["available", "zero-host", "pending-reboot"],
)
def test_check_usb_data_role_verdicts(monkeypatch, role, status, reason):
    monkeypatch.setattr(usbsink, "current_usb_data_role", role)

    r = usbsink.check_usb_data_role()

    assert r.status == status
    assert r.reason == reason


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
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states({usbsink.USBSINK_UNIT: "active" if active else "inactive"}),
    )
    monkeypatch.setattr(usbsink, "_module_loaded", lambda name: libcomposite)
    evidence.seed("parked_bonded_follower", parked)
    gadget = tmp_path / "jts-usb-audio"
    for fn in functions:
        (gadget / "functions" / fn).mkdir(parents=True)
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)
    return gadget


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        ({"active": False, "libcomposite": False}, "ok", usbsink.REASON_STATE_DISABLED),
        # Service stopped but libcomposite still loaded: the previous stop did
        # not tear cleanly.
        (
            {"active": False, "libcomposite": True}, "warn",
            usbsink.REASON_STATE_RAM_DRIFT,
        ),
        # uac2 composed while the readiness marker is inactive is the
        # split-brain that made /sources read off while hosts still saw JTS.
        (
            {"active": False, "libcomposite": True, "functions": ("uac2.usb0",)},
            "fail",
            usbsink.REASON_STATE_SPLIT_BRAIN,
        ),
        # ncm alone is the hardware-conditional management network, not audio
        # drift.
        (
            {"active": False, "libcomposite": True, "functions": ("ncm.usb0",)},
            "ok",
            usbsink.REASON_STATE_DISABLED,
        ),
        (
            {"active": False, "libcomposite": False, "parked": True}, "ok",
            usbsink.REASON_STATE_PARKED,
        ),
        (
            {
                "active": False,
                "libcomposite": True,
                "functions": ("ncm.usb0",),
                "parked": True,
            },
            "ok",
            usbsink.REASON_STATE_PARKED,
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
            usbsink.REASON_STATE_PARKED_STILL_ACTIVE,
        ),
        # Parked + libcomposite but no uac2 is not audio drift by itself;
        # check_usbgadget_composition owns composite RAM-drift detection.
        (
            {"active": False, "libcomposite": True, "parked": True}, "ok",
            usbsink.REASON_STATE_PARKED,
        ),
        (
            {"active": True, "libcomposite": True}, "fail",
            usbsink.REASON_STATE_MARKER_ACTIVE_NO_FUNCTION,
        ),
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
    monkeypatch, tmp_path, kwargs, status, reason
):
    _state_env(monkeypatch, tmp_path, **kwargs)

    r = usbsink.check_usbsink_state()

    assert r.status == status
    assert r.reason == reason


def test_check_usbsink_state_active_reads_host_connection_from_the_udc(
    monkeypatch, tmp_path
):
    """This is the plain healthy-active branch (no distinguishing reason); the
    UDC-derived boolean it discloses is dynamic runtime data the reason
    vocabulary doesn't carry, so this keeps the pure-formatting-helper
    `.detail` exception."""
    _state_env(
        monkeypatch, tmp_path, active=True, libcomposite=True,
        functions=("uac2.usb0",),
    )
    controller = tmp_path / "udc" / "controller"
    controller.mkdir(parents=True)
    (controller / "state").write_text("configured\n")
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(tmp_path / "udc"))

    result = usbsink.check_usbsink_state()

    assert result.status == "ok"
    assert result.reason == ""
    assert "host_connected=True" in result.detail


# ----------------------------------------------------------------------
# check_usbsink_card
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "active, card_present, status, reason",
    [
        (False, False, "skipped", usbsink.REASON_USBSINK_SERVICE_INACTIVE),
        (True, True, "ok", ""),
        (True, False, "fail", usbsink.REASON_CARD_MISSING),
    ],
    ids=["disabled", "present", "missing"],
)
def test_check_usbsink_card_verdicts(
    monkeypatch, tmp_path, active, card_present, status, reason
):
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states({usbsink.USBSINK_UNIT: "active" if active else "inactive"}),
    )
    card = tmp_path / "UAC2Gadget"
    if card_present:
        card.mkdir()

    with patch.object(usbsink, "Path") as mock_path:
        mock_path.side_effect = lambda p: (
            card if p == "/proc/asound/UAC2Gadget" else Path(p)
        )
        r = usbsink.check_usbsink_card()

    assert r.status == status
    assert r.reason == reason


# ----------------------------------------------------------------------
# check_usbsink_host_stream — #3194 disclosure. The Pi cannot tell an idle
# host from a wedged ISO data path, so this check only ever reports.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "composed, card_present, rate, status, reason",
    [
        (False, False, None, "skipped", usbsink.REASON_HOST_STREAM_NOT_COMPOSED),
        (True, False, None, "skipped", usbsink.REASON_HOST_STREAM_NO_CARD),
        (True, True, None, "skipped", usbsink.REASON_HOST_STREAM_NO_CONTROL),
        (True, True, 0, "ok", usbsink.REASON_HOST_STREAM_IDLE),
        (True, True, 48000, "ok", usbsink.REASON_HOST_STREAM_ACTIVE),
    ],
    ids=["not-composed", "no-card", "no-control", "host-idle-or-wedged", "streaming"],
)
def test_check_usbsink_host_stream_discloses_without_judging(
    monkeypatch, tmp_path, composed, card_present, rate, status, reason
):
    function_path = tmp_path / "uac2.usb0"
    if composed:
        function_path.mkdir()
    card = tmp_path / "UAC2Gadget"
    if card_present:
        card.mkdir()
    monkeypatch.setattr(usbsink, "_uac2_function_path", lambda: function_path)
    monkeypatch.setattr(usbsink, "UAC2_CARD_PATH", str(card))
    monkeypatch.setattr(usbsink, "_uac2_capture_rate", lambda: rate)

    result = usbsink.check_usbsink_host_stream()

    assert result.status == status
    assert result.reason == reason


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
    monkeypatch.setattr(usbsink, "_uac2_function_path", lambda: function_path)
    monkeypatch.setattr(usbsink, "UAC2_CARD_PATH", str(card))

    def raise_it(*args, **kwargs):
        raise raised

    monkeypatch.setattr(_shared.subprocess, "run", raise_it)

    result = _shared._run_doctor_check(usbsink.check_usbsink_host_stream)

    assert result.status == "skipped"
    assert result.reason == usbsink.REASON_HOST_STREAM_NO_CONTROL


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

    monkeypatch.setattr(usbsink, "_run", fake_run)

    assert usbsink._uac2_capture_rate() == expected


# ----------------------------------------------------------------------
# check_usbsink_active_libcomposite — the asymmetric mirror of the
# "service inactive + libcomposite loaded" RAM-drift check.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "active, libcomposite, status, reason",
    [
        (False, False, "skipped", usbsink.REASON_USBSINK_SERVICE_INACTIVE),
        (True, True, "ok", ""),
        (True, False, "fail", usbsink.REASON_ACTIVE_MODULES_UNLOADED),
    ],
    ids=["disabled", "consistent", "module-unloaded"],
)
def test_check_usbsink_active_libcomposite_verdicts(
    monkeypatch, active, libcomposite, status, reason
):
    """A daemon active with libcomposite missing means audio cannot flow even
    though systemd thinks the unit is healthy."""
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states({usbsink.USBSINK_UNIT: "active" if active else "inactive"}),
    )
    monkeypatch.setattr(usbsink, "_module_loaded", lambda name: libcomposite)

    r = usbsink.check_usbsink_active_libcomposite()
    assert r.status == status
    assert r.reason == reason


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

    r = usbsink.check_usbsink_low_latency_contract()

    assert r.status == "skipped"
    assert r.reason == usbsink.REASON_LOW_LATENCY_NO_CLAIM


@pytest.mark.parametrize(
    "audio_reason, status, reason",
    [
        ("intent_disabled", "skipped", usbsink.REASON_LOW_LATENCY_NOT_WANTED),
        ("parked_follower", "skipped", usbsink.REASON_LOW_LATENCY_NOT_WANTED),
        (
            _shared.REASON_SOURCE_INTENT_INVALID, "fail",
            _shared.REASON_SOURCE_INTENT_INVALID,
        ),
    ],
    ids=["user-off", "parked", "invalid-intent"],
)
def test_low_latency_contract_reports_why_usb_audio_is_not_wanted(
    monkeypatch, audio_reason, status, reason
):
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(
        usbsink,
        "_audio_wanted",
        lambda: usbsink._AudioIntent(False, audio_reason, "bad token"),
    )
    monkeypatch.setattr(
        _evidence,
        "read_status_socket",
        lambda _path, *, timeout=2.0: (
            _ for _ in ()
        ).throw(AssertionError("must not probe fan-in")),
    )

    result = usbsink.check_usbsink_low_latency_contract()

    assert result.status == status
    assert result.reason == reason


def _claiming_route(monkeypatch, status_reader):
    monkeypatch.setattr(
        usbsink,
        "_audio_wanted",
        lambda: usbsink._AudioIntent(True, "enabled"),
    )
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        _low_latency_plan,
    )
    monkeypatch.setattr(_evidence, "read_status_socket", status_reader)


def test_low_latency_contract_requires_a_readable_fanin_status(monkeypatch):
    _claiming_route(
        monkeypatch,
        lambda _path, *, timeout=2.0: (_ for _ in ()).throw(
            OSError("socket unavailable")
        ),
    )

    r = usbsink.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert r.reason == usbsink.REASON_LOW_LATENCY_STATUS_UNREADABLE


def test_low_latency_contract_warns_when_the_kernel_hides_the_attrs(
    monkeypatch, tmp_path
):
    _claiming_route(monkeypatch, lambda _path, *, timeout=2.0: _fanin_direct_status())
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True)
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)

    r = usbsink.check_usbsink_low_latency_contract()

    assert r.status == "warn"
    assert r.reason == usbsink.REASON_LOW_LATENCY_ATTR_UNEXPOSED


def test_low_latency_contract_fails_on_a_direct_period_mismatch(monkeypatch):
    _claiming_route(
        monkeypatch,
        lambda _path, *, timeout=2.0: _fanin_direct_status(health="capturing", period_frames=128),
    )

    r = usbsink.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert r.reason == usbsink.REASON_LOW_LATENCY_LIVE_MISMATCH


def test_low_latency_contract_fails_on_a_mismatched_exposed_attr(
    monkeypatch, tmp_path
):
    _claiming_route(monkeypatch, lambda _path, *, timeout=2.0: _fanin_direct_status())
    function_path = tmp_path / "gadget" / "functions" / "uac2.usb0"
    function_path.mkdir(parents=True)
    (function_path / "c_sync").write_text("adaptive\n")
    (function_path / "req_number").write_text("2\n")
    (function_path / "c_hs_bint").write_text("1\n")
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", tmp_path / "gadget")

    r = usbsink.check_usbsink_low_latency_contract()

    assert r.status == "fail"
    assert r.reason == usbsink.REASON_LOW_LATENCY_ATTR_MISMATCH


# ----------------------------------------------------------------------
# check_usbsink_name — host-visible device name patch state
# ----------------------------------------------------------------------

_KVER = "6.12.0-test"
_MARKER = f"3\t{_KVER}\tKitchen\tKitchen Mic\tdeadbeef"
_PATCHED = b"\x7fELF Kitchen\x00 patched body, no stock token"


def _name_env(monkeypatch, *, active: bool, speaker: str = "Kitchen"):
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states({usbsink.USBSINK_UNIT: "active" if active else "inactive"}),
    )
    monkeypatch.setattr(
        os, "uname", lambda: type("U", (), {"release": _KVER})()
    )
    monkeypatch.setattr("jasper.speaker_name.runtime_name", lambda: speaker)


def _write_override(root: Path, body: bytes, marker: str | None) -> None:
    updates = root / _KVER / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    (updates / "usb_f_uac2.ko").write_bytes(body)
    if marker is not None:
        (updates / ".jasper-usbsink-name.marker").write_text(marker)


@pytest.mark.parametrize(
    "active, speaker, body, marker, status, reason",
    [
        (False, "Kitchen", None, None, "skipped", usbsink.REASON_USBSINK_SERVICE_INACTIVE),
        (
            True, "Kitchen", None, None, "warn",
            usbsink.REASON_NAME_OVERRIDE_MISSING,
        ),
        # Override present but never actually patched.
        (
            True,
            "Kitchen",
            b"\x7fELF" + b"Playback Inactive\x00Capture Inactive\x00rest",
            _MARKER,
            "warn",
            usbsink.REASON_NAME_STOCK_STRING,
        ),
        (
            True,
            "Kitchen",
            b"\x7fELF Kitchen\x00Kitchen Mic\x00Capture Active\x00rest",
            _MARKER,
            "warn",
            usbsink.REASON_NAME_STOCK_STRING,
        ),
        (True, "Kitchen", _PATCHED, _MARKER, "ok", usbsink.REASON_NAME_PATCHED),
        # Patched for an older name; the speaker has since been renamed.
        (
            True, "Living Room", _PATCHED, _MARKER, "warn",
            usbsink.REASON_NAME_STALE,
        ),
        (True, "Kitchen", _PATCHED, None, "warn", usbsink.REASON_NAME_STALE),
        # A playback-only marker is the pre-mic schema.
        (
            True, "Kitchen", _PATCHED, f"{_KVER}\tKitchen\tdeadbeef", "warn",
            usbsink.REASON_NAME_STALE,
        ),
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
    monkeypatch, tmp_path, active, speaker, body, marker, status, reason
):
    _name_env(monkeypatch, active=active, speaker=speaker)
    if body is not None:
        _write_override(tmp_path, body, marker)

    r = usbsink.check_usbsink_name(modules_root=str(tmp_path))

    assert r.status == status
    assert r.reason == reason


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
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)

    monkeypatch.setattr(
        usbsink, "source_intent_enabled", lambda _source: usbsink_enabled
    )
    evidence.seed("parked_bonded_follower", parked_follower)
    if lifecycle_ready is None:
        lifecycle_ready = usbsink_enabled
    if direct_ready is None:
        direct_ready = lifecycle_ready
    monkeypatch.setattr(
        usbsink,
        "_run",
        lambda _cmd: SimpleNamespace(
            returncode=0 if lifecycle_ready else 1,
            stdout="enabled\n" if lifecycle_ready else "disabled\n",
        ),
    )

    def fake_read_status_socket(_path, *, timeout=2.0):
        if direct_ready:
            return _fanin_direct_status()
        raise OSError("fan-in unreachable")

    monkeypatch.setattr(_evidence, "read_status_socket", fake_read_status_socket)
    return gadget


@pytest.mark.parametrize(
    "env, status, reason",
    [
        # Fresh install pre-reboot: the unit skips via jasper-usbgadget-wanted
        # and check_usb_data_role owns the reboot prompt.
        (
            {"udc_present": False, "network_env": "enabled", "usbsink_enabled": True},
            "skipped",
            usbsink.REASON_COMPOSITION_NO_UDC,
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
            "",
        ),
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": False,
                "ncm": True,
            },
            "ok",
            "",
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
            "",
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
            "",
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
            "",
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
            "",
        ),
        (
            {
                "udc_present": True,
                "network_env": "disabled",
                "usbsink_enabled": False,
            },
            "ok",
            usbsink.REASON_COMPOSITION_ZERO_RAM,
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
            usbsink.REASON_COMPOSITION_MISMATCH,
        ),
        (
            {
                "udc_present": True,
                "network_env": "enabled",
                "usbsink_enabled": True,
                "ncm": True,
            },
            "fail",
            usbsink.REASON_COMPOSITION_MISMATCH,
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
            usbsink.REASON_COMPOSITION_MISMATCH,
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
            usbsink.REASON_COMPOSITION_MISMATCH,
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
    monkeypatch, tmp_path, env, status, reason
):
    _patch_composition_env(monkeypatch, tmp_path, **env)

    r = usbsink.check_usbgadget_composition()

    assert r.status == status
    assert r.reason == reason


@pytest.mark.parametrize(
    "network_env, expected, reason",
    [
        ("DISABLED", "network=False", usbsink.REASON_COMPOSITION_ZERO_RAM),
        ("off", "network=True", ""),
        # A whitespace-decorated ' disabled ' stays WANTED, matching
        # jasper-usbgadget-up's raw (untrimmed) comparison. The bash side is
        # pinned by test_usbgadget_script.py's literal matrix.
        (" disabled ", "network=True", ""),
    ],
    ids=["uppercase", "other-word", "whitespace"],
)
def test_composition_kill_switch_matches_only_the_exact_literal(
    monkeypatch, tmp_path, network_env, expected, reason
):
    """The literal-vs-whitespace kill-switch parity with the bash side is the
    behavior under test here, not a category the reason vocabulary carries on
    its own (both "network=True" cells land in the same "matches intent"
    no-reason branch, discriminated only by which function got composed) —
    kept as the pure-formatting-helper `.detail` exception."""
    _patch_composition_env(
        monkeypatch,
        tmp_path,
        udc_present=True,
        network_env=network_env,
        usbsink_enabled=False,
        ncm=expected == "network=True",
    )

    r = usbsink.check_usbgadget_composition()

    assert r.status == "ok"
    assert r.reason == reason
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

    r = usbsink.check_usbgadget_composition()

    assert r.status == "fail"
    assert r.reason == usbsink.REASON_COMPOSITION_STALE


def test_composition_unreadable_udc_dir_degrades_to_the_no_udc_skip(
    monkeypatch, tmp_path
):
    udc_dir = tmp_path / "udc-not-a-dir"
    udc_dir.write_text("not a directory")
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))

    r = usbsink.check_usbgadget_composition()

    assert r.status == "skipped"
    assert r.reason == usbsink.REASON_COMPOSITION_NO_UDC


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

    monkeypatch.setattr(usbsink, "source_intent_enabled", invalid)

    result = usbsink.check_usbgadget_composition()

    assert result.status == "fail"
    assert result.reason == _shared.REASON_SOURCE_INTENT_INVALID


def test_composition_retains_ncm_during_a_pending_host_reboot(
    monkeypatch, tmp_path
):
    gadget = tmp_path / "gadget"
    (gadget / "functions" / "ncm.usb0").mkdir(parents=True)
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(
        usbsink,
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
        usbsink,
        "_audio_wanted",
        lambda: usbsink._AudioIntent(False, "intent_disabled"),
    )

    result = usbsink.check_usbgadget_composition()

    assert result.status == "warn"
    assert result.reason == usbsink.REASON_COMPOSITION_RETAINED_PENDING_REBOOT


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


def _mic_intent(monkeypatch, *, valid=True, enabled=True, detail="", absent=False):
    monkeypatch.setattr(
        usbsink,
        "read_usb_mic_intent",
        lambda: SimpleNamespace(
            valid=valid, enabled=enabled, detail=detail, absent=absent,
        ),
    )


def test_usb_mic_export_accepts_a_clean_off_descriptor(monkeypatch, tmp_path):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="0", bcd_device="0x0200")
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)
    _mic_intent(monkeypatch, enabled=False)

    result = usbsink.check_usb_mic_export()

    assert result.status == "ok"
    assert result.reason == usbsink.REASON_MIC_EXPORT_DISABLED


def test_usb_mic_export_skips_intent_when_the_gadget_is_unavailable(monkeypatch):
    """Unavailable hardware must gate the durable intent read entirely."""
    monkeypatch.setattr(usbsink, "current_usb_data_role", _zero_host_role)
    monkeypatch.setattr(
        usbsink,
        "read_usb_mic_intent",
        lambda: (_ for _ in ()).throw(AssertionError("intent must not be read")),
    )

    result = usbsink.check_usb_mic_export()

    assert result.status == "skipped"
    assert result.reason == usbsink.REASON_MIC_EXPORT_NOT_APPLICABLE


def test_usb_mic_export_defaults_to_disabled_when_the_intent_was_never_set(
    monkeypatch, tmp_path,
):
    """A never-written intent file is the factory default (disabled), not a
    fault — only a present-but-corrupt file should fail."""
    gadget = _usb_mic_gadget(tmp_path, p_chmask="0", bcd_device="0x0200")
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)
    _mic_intent(
        monkeypatch,
        valid=False,
        enabled=False,
        detail="USB microphone preference is missing.",
        absent=True,
    )

    result = usbsink.check_usb_mic_export()

    assert result.status == "skipped"
    assert result.reason == usbsink.REASON_MIC_EXPORT_NOT_CONFIGURED


def test_usb_mic_export_rejects_a_corrupt_intent_when_the_gadget_is_available(
    monkeypatch,
):
    _mic_intent(
        monkeypatch,
        valid=False,
        enabled=False,
        detail="Unrecognised JASPER_USB_MIC value 'maybe'.",
    )

    result = usbsink.check_usb_mic_export()

    assert result.status == "fail"
    assert result.reason == _shared.REASON_SOURCE_INTENT_INVALID


def test_usb_mic_export_rejects_a_stale_descriptor_revision(monkeypatch, tmp_path):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0200")
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)
    _mic_intent(monkeypatch)
    monkeypatch.setattr(
        usbsink,
        "_audio_wanted",
        lambda: usbsink._AudioIntent(True, "ready"),
    )

    result = usbsink.check_usb_mic_export()

    assert result.status == "fail"
    assert result.reason == usbsink.REASON_MIC_EXPORT_DESCRIPTOR_STALE


def _relay_mic_env(monkeypatch, tmp_path, payload: dict):
    gadget = _usb_mic_gadget(tmp_path, p_chmask="1", bcd_device="0x0210")
    relay = tmp_path / "relay.json"
    relay.write_text(json.dumps(payload))
    monkeypatch.setattr(usbsink, "USBSINK_GADGET_PATH", gadget)
    monkeypatch.setattr(usbsink, "RELAY_STATUS_PATH", str(relay))
    monkeypatch.setattr(usbsink.time, "time", lambda: 100.5)
    _mic_intent(monkeypatch)
    monkeypatch.setattr(
        usbsink,
        "_audio_wanted",
        lambda: usbsink._AudioIntent(True, "ready"),
    )
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states({usbsink.USBMIC_UNIT: "active"}),
    )


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

    result = usbsink.check_usb_mic_export()

    assert result.status == "warn"
    assert result.reason == usbsink.REASON_MIC_EXPORT_AUDIO_UNHEALTHY


@pytest.mark.parametrize(
    "host_streaming, p95_ms, relay_schema, metric_scope, status, reason",
    [
        (
            True, 121.0, 4, "bridge_emit_to_alsa_write", "warn",
            usbsink.REASON_MIC_EXPORT_LATENCY_HIGH,
        ),
        (True, 119.0, 4, "bridge_emit_to_alsa_write", "ok", ""),
        (
            True, None, 4, "bridge_emit_to_alsa_write", "warn",
            usbsink.REASON_MIC_EXPORT_LATENCY_UNAVAILABLE,
        ),
        (
            True, "bad", 4, "bridge_emit_to_alsa_write", "warn",
            usbsink.REASON_MIC_EXPORT_LATENCY_UNAVAILABLE,
        ),
        (
            True, -1.0, 4, "bridge_emit_to_alsa_write", "warn",
            usbsink.REASON_MIC_EXPORT_LATENCY_UNAVAILABLE,
        ),
        (
            True,
            float("nan"),
            4,
            "bridge_emit_to_alsa_write",
            "warn",
            usbsink.REASON_MIC_EXPORT_LATENCY_UNAVAILABLE,
        ),
        (
            True, 20.0, 3, "bridge_emit_to_relay_dequeue", "warn",
            usbsink.REASON_MIC_EXPORT_METRIC_CONTRACT_UNSUPPORTED,
        ),
        (False, 500.0, 4, "bridge_emit_to_alsa_write", "ok", ""),
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
    reason,
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

    result = usbsink.check_usb_mic_export()

    assert result.status == status
    assert result.reason == reason


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
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states({usbsink.USBSINK_UNIT: "failed" if failed else "inactive"}),
    )
    monkeypatch.setattr(_ca, "read_usb_gadget_available", lambda *a, **k: gadget)
    monkeypatch.setattr(
        usbsink, "source_intent_enabled", lambda _source: intent
    )
    evidence.seed("parked_bonded_follower", parked)
    declare_fanin_env(
        monkeypatch,
        tmp_path,
        f"{_ca.USB_DIRECT_ENV_VAR}={_ca.USB_COMBO_ENABLED_VALUE}\n" if armed else "",
    )


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        ({"failed": True}, "fail", usbsink.REASON_COMBO_UNIT_FAILED),
        # A failed post-toggle kick leaves combo unarmed with no marker.
        ({"intent": True, "armed": False}, "warn", usbsink.REASON_COMBO_UNARMED),
        # Desired-On is intentionally disarmed while follower-role parked.
        (
            {"intent": True, "parked": True, "armed": False}, "ok",
            usbsink.REASON_COMBO_DISARMED,
        ),
        ({"intent": True, "armed": True}, "ok", usbsink.REASON_COMBO_ARMED),
        ({"intent": False, "armed": False}, "ok", usbsink.REASON_COMBO_DISARMED),
        ({"gadget": False}, "skipped", usbsink.REASON_COMBO_NOT_APPLICABLE),
    ],
    ids=["failed-unit", "intent-on-unarmed", "parked", "armed", "off", "no-gadget"],
)
def test_check_usb_combo_consistency_verdicts(
    monkeypatch, tmp_path, kwargs, status, reason
):
    _setup_combo(monkeypatch, tmp_path, **kwargs)

    r = usbsink.check_usb_combo_consistency()

    assert r.status == status
    assert r.reason == reason


def test_check_usb_combo_consistency_invalid_intent_is_fail(monkeypatch, tmp_path):
    _setup_combo(monkeypatch, tmp_path, intent=True, armed=False)

    def invalid(_source):
        raise RuntimeError("bad USB intent")

    monkeypatch.setattr(usbsink, "source_intent_enabled", invalid)

    result = usbsink.check_usb_combo_consistency()

    assert result.status == "fail"
    assert result.reason == _shared.REASON_SOURCE_INTENT_INVALID
