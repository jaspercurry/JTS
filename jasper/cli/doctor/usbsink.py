# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — usbsink domain.

Originally re-homed verbatim from the monolithic ``jasper/cli/doctor.py``;
reworked for the composite-gadget model. The gadget is now
``jasper-usbgadget.service`` — a single ConfigFS owner that
composes up to two functions onto one UDC: ``ncm.usb0`` (the USB
management network) and ``uac2.usb0`` (the wizard-toggled USB Audio Input,
whose readiness marker is ``jasper-usbsink.service``). The old invariant
"libcomposite loaded <=> usbsink active" no longer holds — libcomposite can be
loaded for the network function alone with USB audio fully off. The checks below
compare observed gadget/function state against the *composed intent*
(network kill-switch + canonical audio authorization), mirroring the truth table
``jasper-usbgadget-up``/``jasper-usbgadget-wanted`` compute. Derived lifecycle
readiness is reported, never composed on (ADR-0191).
``check_usbsink_low_latency_contract`` reads the actual fan-in direct-capture
lane; the oneshot marker is lifecycle/readiness state, not data-plane liveness
or latency evidence."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import NamedTuple

from jasper.audio_hardware.usb_port_role import gadget_unavailable_detail
from jasper.audio_runtime_plan import UAC2_LOW_LATENCY_EXPECTED_ATTRS
from jasper.audio_validation_route import route_live_state_issues
from jasper.fanin.status import fanin_usbsink_lane_is_direct, read_fanin_status
from jasper.music_sources import Source
from jasper.output_hardware import current_usb_data_role
from jasper.route_latency.status_socket import FANIN_STATUS_SOCKET, read_status_socket
from jasper.source_intent import source_intent_enabled
from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR, udc_host_connected
from jasper.usb_mic import (
    RELAY_STATUS_FRESH_SECONDS,
    RELAY_STATUS_PATH,
    USB_MIC_BCD_DEVICE,
    USB_MIC_LATENCY_WARN_MS,
    USB_MIC_RELAY_SCHEMA_VERSION,
    USB_MIC_SOURCE_AGE_BASIS,
    USB_MIC_SOURCE_AGE_SCOPE,
    USB_NO_MIC_BCD_DEVICE,
    USBMIC_UNIT,
    read_intent as read_usb_mic_intent,
    relay_audio_issue,
)

from ._registry import doctor_check
from ._shared import (
    REASON_SOURCE_INTENT_INVALID,
    CheckResult,
    _parked_as_bonded_follower,
    _run,
)

# Closed vocabulary for this module's `CheckResult.reason` (AGENTS.md: tests
# pin status + reason, never `detail` prose). Named by the fact a consumer
# would branch on; two branches meaning the same thing to a consumer share
# one code (e.g. every "disabled but still advertised" mismatch below).
REASON_DATA_ROLE_UNAVAILABLE = "data_role_unavailable"
REASON_DATA_ROLE_REBOOT_REQUIRED = "data_role_reboot_required"
REASON_DATA_ROLE_HOST_ONLY = "data_role_host_only"
REASON_DATA_ROLE_GADGET_UNAVAILABLE = "data_role_gadget_unavailable"

REASON_STATE_HARDWARE_MISMATCH = "state_hardware_mismatch"
REASON_STATE_HARDWARE_UNAVAILABLE = "state_hardware_unavailable"
REASON_STATE_PARKED_STILL_ACTIVE = "state_parked_still_active"
REASON_STATE_PARKED = "state_parked"
REASON_STATE_SPLIT_BRAIN = "state_split_brain"
REASON_STATE_RAM_DRIFT = "state_ram_drift"
REASON_STATE_DISABLED = "state_disabled"
REASON_STATE_MARKER_ACTIVE_NO_FUNCTION = "state_marker_active_no_function"

# The readiness marker is not active, so the card, its device name and the
# composed modules have nothing to observe — one code behind
# `_skip_when_usbsink_inactive` for all three.
REASON_USBSINK_SERVICE_INACTIVE = "usbsink_service_inactive"

REASON_CARD_MISSING = "card_missing"

REASON_HOST_STREAM_NOT_COMPOSED = "host_stream_not_composed"
REASON_HOST_STREAM_NO_CARD = "host_stream_no_card"
REASON_HOST_STREAM_NO_CONTROL = "host_stream_no_control"
REASON_HOST_STREAM_ACTIVE = "host_stream_active"
REASON_HOST_STREAM_IDLE = "host_stream_idle"

REASON_LOW_LATENCY_NOT_APPLICABLE = "low_latency_not_applicable"
REASON_LOW_LATENCY_NO_CLAIM = "low_latency_no_claim"
REASON_LOW_LATENCY_NOT_WANTED = "low_latency_not_wanted"
REASON_LOW_LATENCY_STATUS_UNREADABLE = "low_latency_status_unreadable"
REASON_LOW_LATENCY_LIVE_MISMATCH = "low_latency_live_mismatch"
REASON_LOW_LATENCY_ATTR_MISMATCH = "low_latency_attr_mismatch"
REASON_LOW_LATENCY_ATTR_UNEXPOSED = "low_latency_attr_unexposed"

REASON_MIC_EXPORT_NOT_APPLICABLE = "mic_export_not_applicable"
REASON_MIC_EXPORT_UNEXPECTED_ADVERTISE = "mic_export_unexpected_advertise"
REASON_MIC_EXPORT_DISABLED = "mic_export_disabled"
REASON_MIC_EXPORT_AUDIO_NOT_WANTED = "mic_export_audio_not_wanted"
REASON_MIC_EXPORT_NOT_ADVERTISED = "mic_export_not_advertised"
REASON_MIC_EXPORT_DESCRIPTOR_STALE = "mic_export_descriptor_stale"
REASON_MIC_EXPORT_UNIT_INACTIVE = "mic_export_unit_inactive"
REASON_MIC_EXPORT_RELAY_STALE = "mic_export_relay_stale"
REASON_MIC_EXPORT_AUDIO_UNHEALTHY = "mic_export_audio_unhealthy"
REASON_MIC_EXPORT_METRIC_CONTRACT_UNSUPPORTED = "mic_export_metric_contract_unsupported"
REASON_MIC_EXPORT_LATENCY_UNAVAILABLE = "mic_export_latency_unavailable"
REASON_MIC_EXPORT_LATENCY_HIGH = "mic_export_latency_high"

REASON_COMBO_UNIT_FAILED = "combo_unit_failed"
REASON_COMBO_NOT_APPLICABLE = "combo_not_applicable"
REASON_COMBO_UNARMED = "combo_unarmed"
REASON_COMBO_STALE_ARM = "combo_stale_arm"
REASON_COMBO_ARMED = "combo_armed"
REASON_COMBO_DISARMED = "combo_disarmed"

REASON_NAME_OVERRIDE_MISSING = "name_override_missing"
REASON_NAME_STOCK_STRING = "name_stock_string"
REASON_NAME_OVERRIDE_UNREADABLE = "name_override_unreadable"
REASON_NAME_PATCHED = "name_patched"
REASON_NAME_STALE = "name_stale"

REASON_ACTIVE_MODULES_UNLOADED = "active_modules_unloaded"

REASON_COMPOSITION_STALE_HARDWARE_MISMATCH = "composition_stale_hardware_mismatch"
REASON_COMPOSITION_NOT_APPLICABLE = "composition_not_applicable"
REASON_COMPOSITION_AUDIO_DURING_TRANSITION = "composition_audio_during_transition"
REASON_COMPOSITION_NO_UDC = "composition_no_udc"
REASON_COMPOSITION_STALE = "composition_stale"
REASON_COMPOSITION_ZERO_RAM = "composition_zero_ram"
REASON_COMPOSITION_MISMATCH = "composition_mismatch"
REASON_COMPOSITION_RETAINED_PENDING_REBOOT = "composition_retained_pending_reboot"

USBSINK_UNIT = "jasper-usbsink.service"
USBGADGET_UNIT = "jasper-usbgadget.service"
USBSINK_GADGET_PATH = Path("/sys/kernel/config/usb_gadget/jts-usb-audio")
UAC2_EXPECTED_LOW_LATENCY_ATTRS = UAC2_LOW_LATENCY_EXPECTED_ATTRS
USB_NAME_PATCH_SCHEMA = "3"
UAC2_CARD_NAME = "UAC2Gadget"
UAC2_CARD_PATH = "/proc/asound/UAC2Gadget"
# u_audio registers the volatile host-stream rate indicator on the PCM
# interface, not MIXER, and its numid shifts with the composed direction set —
# resolve it by name rather than pinning a numid.
_UAC2_RATE_NUMID_RE = re.compile(r"numid=(\d+),iface=PCM,name='Capture Rate'")
_UAC2_CTL_VALUE_RE = re.compile(r"^\s*: values=(\d+)", re.MULTILINE)


def _systemd_is_active(unit: str) -> bool:
    """Wrapper around `systemctl is-active`. Cheap; ~5 ms per call."""
    return _run(["systemctl", "is-active", unit]).stdout.strip() == "active"

def _skip_when_usbsink_inactive(label: str) -> CheckResult | None:
    """The `skipped` row for a check whose evidence only exists while the
    readiness marker is active, or ``None`` when it is active."""
    if _systemd_is_active(USBSINK_UNIT):
        return None
    return CheckResult(
        label, "skipped",
        f"{USBSINK_UNIT} inactive — nothing to observe",
        reason=REASON_USBSINK_SERVICE_INACTIVE,
    )

def _systemd_is_failed(unit: str) -> bool:
    """Wrapper around `systemctl is-failed`. True when the unit is parked in the
    `failed` state. Cheap."""
    return _run(["systemctl", "is-failed", unit]).stdout.strip() == "failed"

def _module_loaded(name: str) -> bool:
    """True if `lsmod` shows the named kernel module."""
    proc = _run(["lsmod"])
    if proc.returncode != 0:
        return False
    # lsmod output: first column is the module name. Match-at-line-
    # start to avoid substring matches against unrelated modules.
    return any(
        line.split() and line.split()[0] == name
        for line in proc.stdout.splitlines()
    )


def _uac2_capture_rate() -> int | None:
    """Read u_audio's volatile ``Capture Rate`` control, or None if unreadable.

    Subprocesses ``amixer`` because the control is an iface=PCM one the simple
    mixer does not expose, and its output is stable and parseable. ``_run`` is
    a bare ``subprocess.run``, so both failure
    modes have to be caught here: alsa-utils is not in install.sh's apt lists,
    and a wedged card — the very state this feeds — can hang the read past the
    timeout. Either one must read as "not observable", never as a doctor crash.
    ``TimeoutExpired`` subclasses ``SubprocessError``, not ``OSError``."""
    try:
        controls = _run(["amixer", "-c", UAC2_CARD_NAME, "controls"])
        if controls.returncode != 0:
            return None
        numid = _UAC2_RATE_NUMID_RE.search(controls.stdout)
        if numid is None:
            return None
        value = _run(
            ["amixer", "-c", UAC2_CARD_NAME, "cget", f"numid={numid.group(1)}"]
        )
        if value.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    match = _UAC2_CTL_VALUE_RE.search(value.stdout)
    return int(match.group(1)) if match else None


def _uac2_function_path() -> Path:
    return USBSINK_GADGET_PATH / "functions" / "uac2.usb0"


def _ncm_function_path() -> Path:
    return USBSINK_GADGET_PATH / "functions" / "ncm.usb0"


def _network_wanted() -> bool:
    """Mirror the network half of the shared gadget truth table.

    The table itself lives in ``deploy/usbsink/jasper-usbgadget-compose.sh``,
    which ``jasper-usbgadget-{wanted,up,converge}`` all source; this is the
    deliberate SECOND implementation, in Python, so the doctor can report on a
    composition it did not compute.

    Network is wanted unless the kill switch is the exact literal
    ``disabled`` (case-insensitive); any other value is treated as
    enabled, same as ``JASPER_SHAIRPORT_SUPERVISOR`` /
    ``JASPER_SYSTEM_SUPERVISOR``. Read from ``os.environ`` (not a fresh
    file parse) because ``jasper.env_load`` already unions
    ``/etc/jasper/jasper.env`` into ``os.environ`` at CLI startup —
    the same convention every other doctor env read in this package uses.
    The shell side reaches the same value the same way: the fragment reads
    that one key out of the same file when nothing supplied it in the
    environment, parsing it exactly as ``parse_env_text`` does.

    NOT stripped: the shell table matches the RAW environment value (no trim), so
    a whitespace-decorated ``" disabled"`` is a warned near-miss that STAYS
    enabled in bash. The Python readers must agree byte-for-byte, or
    check_usbgadget_composition would false-fail when bash composed ncm but
    Python thought the kill switch was set. The fail-safe
    direction is deliberate: a stray space must never silently drop the
    default-on fallback network when hardware permits it. Pinned by
    tests/test_usbgadget_script.py's
    literal matrix (bash) and test_doctor_usbsink.py (Python)."""
    raw = os.environ.get("JASPER_USB_NETWORK", "enabled")
    return raw.lower() != "disabled"


class _AudioIntent(NamedTuple):
    """What `_audio_wanted` observed: whether UAC2 should be composed, the
    machine-stable code for why, and the evidence behind an invalid intent."""

    wanted: bool
    reason: str
    detail: str = ""


def _audio_wanted() -> _AudioIntent:
    """Return whether the gadget should compose UAC2, and why.

    Wanted when the canonical /sources intent is enabled AND local sources are
    allowed (a bonded follower parks it). Derived state — the lifecycle mirror
    unit, fan-in's DIRECT consumer — is a CONSEQUENCE of that intent, reported
    by its own checks, never a precondition that can withdraw the endpoint
    (ADR-0191).

    Keep this in lockstep with ``deploy/usbsink/jasper-usbgadget-compose.sh``,
    the single shell definition that ``jasper-usbgadget-{wanted,up,converge}``
    all source.

    ``reason`` is a code, never prose, so callers branch on it rather than
    sniffing a prefix: intent Off, follower parking, an invalid intent, or
    effective authorization."""
    try:
        enabled = source_intent_enabled(Source.USBSINK)
    except RuntimeError as exc:
        return _AudioIntent(False, REASON_SOURCE_INTENT_INVALID, str(exc))
    if not enabled:
        return _AudioIntent(False, "intent_disabled")
    if _parked_as_bonded_follower():
        return _AudioIntent(False, "parked_follower")
    return _AudioIntent(True, "enabled")

@doctor_check(order=57, group="usbsink")
def check_usb_data_role() -> CheckResult:
    """Explain the resolved host/peripheral role and pending reboot state."""

    try:
        state = current_usb_data_role()
    except (OSError, RuntimeError, ValueError) as exc:
        return CheckResult(
            "USB data role", "warn", f"capability state unavailable: {exc}",
            reason=REASON_DATA_ROLE_UNAVAILABLE,
        )
    detail = (
        f"topology={state.board_topology}, desired={state.desired_role}, "
        f"configured={state.configured_role}, active={state.active_role}, "
        f"management_transport={state.management_transport_available}, "
        f"reason={state.reason}"
    )
    if state.reboot_required:
        return CheckResult(
            "USB data role",
            "warn",
            f"{detail}; {gadget_unavailable_detail(state)}",
            reason=REASON_DATA_ROLE_REBOOT_REQUIRED,
        )
    if state.gadget_available:
        return CheckResult(
            "USB data role", "ok", f"{detail}; USB gadget available"
        )
    unavailable = f"{detail}; {gadget_unavailable_detail(state)}"
    if state.desired_role == "host":
        return CheckResult(
            "USB data role", "ok", unavailable,
            reason=REASON_DATA_ROLE_HOST_ONLY,
        )
    return CheckResult(
        "USB data role", "warn", unavailable,
        reason=REASON_DATA_ROLE_GADGET_UNAVAILABLE,
    )

@doctor_check(order=58, group="usbsink")
def check_usbsink_state() -> CheckResult:
    """Check the USB readiness marker against observed gadget state.

    When the service is inactive, verify the host-visible *audio* function
    (uac2.usb0) is also absent. A composed uac2.usb0 with the marker down is
    a split-brain source state: computers still see JTS as USB audio while
    /sources can otherwise appear off. The composite gadget itself
    (jasper-usbgadget.service / ConfigFS dir) legitimately persists for the
    hardware-permitted management network even when audio is off — that alone is
    never a drift signal here; check_usbgadget_composition owns the
    gadget-vs-network-intent story. A leftover libcomposite module with
    NEITHER function composed is RAM drift (network kill-switched + audio
    off, but the module never unloaded)."""
    active = _systemd_is_active(USBSINK_UNIT)
    uac2_present = _uac2_function_path().exists()
    libcomp = _module_loaded("libcomposite")
    usb_role = current_usb_data_role()
    if not usb_role.gadget_available:
        if active or uac2_present:
            return CheckResult(
                "usbsink state",
                "fail",
                "USB gadget hardware is unavailable but USB Audio Input is "
                f"still active/advertised (active={active}, uac2={uac2_present}).",
                reason=REASON_STATE_HARDWARE_MISMATCH,
            )
        return CheckResult(
            "usbsink state",
            "skipped",
            f"USB Audio Input unavailable as resolved ({usb_role.reason})",
            reason=REASON_STATE_HARDWARE_UNAVAILABLE,
        )

    if _parked_as_bonded_follower():
        if active or uac2_present:
            details: list[str] = []
            if active:
                details.append(f"{USBSINK_UNIT}=active")
            if uac2_present:
                details.append("uac2.usb0 function present")
            return CheckResult(
                "usbsink state",
                "fail",
                "parked (bonded follower) but USB Audio Input is still "
                f"running/advertised ({', '.join(details)}). Run "
                "jasper-grouping-reconcile or unpair/re-pair so the "
                "local-source park plan recomposes the gadget without "
                "uac2.usb0.",
                reason=REASON_STATE_PARKED_STILL_ACTIVE,
            )
        return CheckResult(
            "usbsink state",
            "ok",
            "parked (bonded follower) — readiness marker and uac2.usb0 function down"
            + (" (gadget may still carry ncm.usb0 for the management network)"
               if USBSINK_GADGET_PATH.exists() else ""),
            reason=REASON_STATE_PARKED,
        )

    if not active:
        if uac2_present:
            return CheckResult(
                "usbsink state",
                "fail",
                "readiness marker inactive but USB Audio Input is still advertised "
                "(uac2.usb0 function present in the composite gadget). "
                "Toggle USB Audio Input off in /sources/ or run "
                "`sudo systemctl restart jasper-usbgadget.service` so "
                "hosts stop seeing the audio device.",
                reason=REASON_STATE_SPLIT_BRAIN,
            )
        if libcomp and not USBSINK_GADGET_PATH.exists():
            return CheckResult(
                "usbsink state", "warn",
                "service inactive, uac2.usb0 absent, but libcomposite still "
                "loaded with no gadget directory — RAM drift from a failed "
                "stop. Reboot or `sudo rmmod u_audio libcomposite` to "
                "recover.",
                reason=REASON_STATE_RAM_DRIFT,
            )
        return CheckResult(
            "usbsink state", "ok",
            "USB Audio Input disabled (uac2.usb0 not composed; the "
            "composite gadget/libcomposite may still be resident for the "
            "hardware-conditional USB management network — see "
            "check_usbgadget_composition)",
            reason=REASON_STATE_DISABLED,
        )

    if not uac2_present:
        return CheckResult(
            "usbsink state",
            "fail",
            "readiness marker active but uac2.usb0 is absent — restart "
            f"{USBGADGET_UNIT} so the marker re-runs its bounded card gate.",
            reason=REASON_STATE_MARKER_ACTIVE_NO_FUNCTION,
        )
    connected = udc_host_connected(
        os.environ.get("JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR),
    )
    return CheckResult(
        "usbsink state", "ok",
        "readiness marker active; uac2.usb0 composed; "
        f"host_connected={connected} (activity/level owned by fan-in STATUS)",
    )

@doctor_check(order=59, group="usbsink")
def check_usbsink_card() -> CheckResult:
    """When jasper-usbsink is enabled, the UAC2Gadget ALSA card MUST
    be present — otherwise jasper-usbgadget.service either didn't run
    or failed to compose/bind the uac2.usb0 function to the UDC."""
    inactive = _skip_when_usbsink_inactive("usbsink card")
    if inactive is not None:
        return inactive
    if Path(UAC2_CARD_PATH).is_dir():
        return CheckResult(
            "usbsink card", "ok",
            "UAC2Gadget card present (host will see the speaker as USB audio)",
        )
    return CheckResult(
        "usbsink card", "fail",
        "service active but /proc/asound/UAC2Gadget missing — "
        f"{USBGADGET_UNIT} didn't compose/bind uac2.usb0. Check "
        f"`systemctl status {USBGADGET_UNIT}` for the failure mode.",
        reason=REASON_CARD_MISSING,
    )


@doctor_check(order=59.3, group="usbsink")
def check_usbsink_host_stream() -> CheckResult:
    """Disclose whether the HOST has actually started the USB audio stream.

    Everything else in this group proves the gadget side: descriptor composed,
    card present, marker active, lane armed. None of it can see the one state
    #3194 produced — the host enumerates and its control plane works (volume
    keys move ``PCM Capture Volume``) while the ISO data path never starts, so
    playback is silent with no failing check anywhere.

    u_audio publishes exactly that fact as a volatile, read-only ``Capture
    Rate`` kcontrol on the gadget card: it holds the negotiated rate while the
    host streams and reads 0 otherwise. This check reports it and nothing more.
    An idle host also reads 0, so the Pi cannot tell "host idle" from "host
    playing into a wedged data path" — the honest disclosure is the number plus
    the recovery, never a verdict, which is why this check never fails."""
    label = "usbsink host stream"
    if not _uac2_function_path().exists():
        return CheckResult(
            label, "skipped", "uac2.usb0 not composed — no host stream",
            reason=REASON_HOST_STREAM_NOT_COMPOSED,
        )
    if not Path(UAC2_CARD_PATH).is_dir():
        return CheckResult(
            label, "skipped",
            f"{UAC2_CARD_PATH} missing — see check_usbsink_card",
            reason=REASON_HOST_STREAM_NO_CARD,
        )
    rate = _uac2_capture_rate()
    if rate is None:
        return CheckResult(
            label, "skipped",
            "kernel does not expose the u_audio 'Capture Rate' control on "
            f"{UAC2_CARD_NAME} — host stream state is not observable here",
            reason=REASON_HOST_STREAM_NO_CONTROL,
        )
    if rate > 0:
        return CheckResult(
            label, "ok", f"host stream running (capture_rate={rate})",
            reason=REASON_HOST_STREAM_ACTIVE,
        )
    return CheckResult(
        label, "ok",
        "host is not streaming (capture_rate=0). Normal while the host is "
        "idle. If the host IS playing, the ISO data path never started: "
        f"`systemctl restart {USBGADGET_UNIT}`, then restart jasper-fanin and "
        f"{USBMIC_UNIT} to drop their stale card handles (#3194).",
        reason=REASON_HOST_STREAM_IDLE,
    )


@doctor_check(order=59.5, group="usbsink")
def check_usbsink_low_latency_contract() -> CheckResult:
    """When the route claims low latency, verify the live USB data plane."""

    usb_role = current_usb_data_role()
    if not usb_role.gadget_available:
        return CheckResult(
            "usbsink low-latency contract",
            "skipped",
            f"not applicable: USB gadget unavailable ({usb_role.reason})",
            reason=REASON_LOW_LATENCY_NOT_APPLICABLE,
        )

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    if not plan.route_profile.low_latency_claim:
        return CheckResult(
            "usbsink low-latency contract",
            "skipped",
            f"route_profile={plan.route_profile.route_id} has no USB low-latency claim",
            reason=REASON_LOW_LATENCY_NO_CLAIM,
        )

    audio = _audio_wanted()
    if not audio.wanted:
        if audio.reason == REASON_SOURCE_INTENT_INVALID:
            return CheckResult(
                "usbsink low-latency contract",
                "fail",
                f"USB source intent is invalid: {audio.detail}",
                reason=REASON_SOURCE_INTENT_INVALID,
            )
        return CheckResult(
            "usbsink low-latency contract",
            "skipped",
            "live USB low-latency check not applicable: "
            f"route_profile={plan.route_profile.route_id}, {audio.reason}",
            reason=REASON_LOW_LATENCY_NOT_WANTED,
        )

    try:
        fanin_status = read_status_socket(FANIN_STATUS_SOCKET)
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            f"can't read fan-in STATUS at {FANIN_STATUS_SOCKET}: {e}",
            reason=REASON_LOW_LATENCY_STATUS_UNREADABLE,
        )
    live_issues = tuple(
        route_live_state_issues(
            plan.route_latency_identity(),
            fanin_status=fanin_status,
        )
    )
    if live_issues:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            "usb_low_latency_48k live fan-in direct-capture state does not match route "
            f"identity: {list(live_issues)}",
            reason=REASON_LOW_LATENCY_LIVE_MISMATCH,
        )

    lane = next(
        (
            item
            for item in fanin_status.get("inputs", [])
            if isinstance(item, dict) and item.get("label") == "usbsink"
        ),
        {},
    )
    direct = lane.get("direct") if isinstance(lane, dict) else {}

    missing: list[str] = []
    mismatched: list[str] = []
    function_path = _uac2_function_path()
    for name, expected in UAC2_EXPECTED_LOW_LATENCY_ATTRS.items():
        path = function_path / name
        if not path.exists():
            missing.append(name)
            continue
        try:
            observed = path.read_text().strip()
        except OSError as e:
            mismatched.append(f"{name}=unreadable({e}) expected={expected}")
            continue
        if observed != expected:
            mismatched.append(f"{name}={observed!r} expected={expected!r}")
    detail = (
        f"route_profile={plan.route_profile.route_id}, fanin_source=direct, "
        f"direct={direct}"
    )
    if mismatched:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            detail
            + "; UAC2 attrs mismatched: "
            + ", ".join(mismatched)
            + f"; Restart {USBGADGET_UNIT} so the gadget descriptor is recreated.",
            reason=REASON_LOW_LATENCY_ATTR_MISMATCH,
        )
    if missing:
        return CheckResult(
            "usbsink low-latency contract",
            "warn",
            detail + "; kernel does not expose UAC2 attrs: " + ", ".join(missing),
            reason=REASON_LOW_LATENCY_ATTR_UNEXPOSED,
        )
    return CheckResult("usbsink low-latency contract", "ok", detail)


@doctor_check(order=59.7, group="usbsink")
def check_usb_mic_export() -> CheckResult:
    """Cross-check USB-microphone intent, descriptor, relay, and privacy."""

    usb_role = current_usb_data_role()
    if not usb_role.gadget_available:
        return CheckResult(
            "USB microphone export",
            "skipped",
            f"not applicable: {gadget_unavailable_detail(usb_role)}",
            reason=REASON_MIC_EXPORT_NOT_APPLICABLE,
        )

    intent = read_usb_mic_intent()
    function = _uac2_function_path()
    try:
        p_chmask = (function / "p_chmask").read_text(encoding="utf-8").strip()
    except OSError:
        p_chmask = ""
    advertised = p_chmask == "1"
    try:
        bcd_device = (USBSINK_GADGET_PATH / "bcdDevice").read_text(
            encoding="utf-8",
        ).strip()
    except OSError:
        bcd_device = ""
    if not intent.valid:
        return CheckResult(
            "USB microphone export", "fail", intent.detail,
            reason=REASON_SOURCE_INTENT_INVALID,
        )
    if not intent.enabled:
        if advertised:
            return CheckResult(
                "USB microphone export",
                "fail",
                "preference is Off but UAC2 p_chmask=1 still advertises a host "
                f"microphone; restart {USBGADGET_UNIT}",
                reason=REASON_MIC_EXPORT_UNEXPECTED_ADVERTISE,
            )
        if function.is_dir() and bcd_device != USB_NO_MIC_BCD_DEVICE:
            return CheckResult(
                "USB microphone export",
                "fail",
                "preference is Off but the composed UAC2 descriptor revision is "
                f"{bcd_device or 'missing'}, expected {USB_NO_MIC_BCD_DEVICE}; "
                f"restart {USBGADGET_UNIT}",
                reason=REASON_MIC_EXPORT_UNEXPECTED_ADVERTISE,
            )
        return CheckResult(
            "USB microphone export", "ok", "disabled; host microphone absent",
            reason=REASON_MIC_EXPORT_DISABLED,
        )

    audio = _audio_wanted()
    if not audio.wanted:
        return CheckResult(
            "USB microphone export",
            "warn",
            "preference is On but USB Audio Input is unavailable "
            f"({audio.reason}); saved intent will apply when that source recovers",
            reason=REASON_MIC_EXPORT_AUDIO_NOT_WANTED,
        )
    if not advertised:
        return CheckResult(
            "USB microphone export",
            "fail",
            "preference is On but UAC2 does not advertise the mono host input "
            f"(p_chmask={p_chmask or 'missing'}); restart {USBGADGET_UNIT}",
            reason=REASON_MIC_EXPORT_NOT_ADVERTISED,
        )
    if bcd_device != USB_MIC_BCD_DEVICE:
        return CheckResult(
            "USB microphone export",
            "fail",
            "host microphone is advertised but the descriptor revision is "
            f"{bcd_device or 'missing'}, expected {USB_MIC_BCD_DEVICE}; "
            f"restart {USBGADGET_UNIT}",
            reason=REASON_MIC_EXPORT_DESCRIPTOR_STALE,
        )
    if not _systemd_is_active(USBMIC_UNIT):
        return CheckResult(
            "USB microphone export",
            "fail",
            f"host microphone is advertised but {USBMIC_UNIT} is inactive",
            reason=REASON_MIC_EXPORT_UNIT_INACTIVE,
        )
    try:
        relay_payload = json.loads(
            Path(RELAY_STATUS_PATH).read_text(encoding="utf-8")
        )
        relay = relay_payload if isinstance(relay_payload, dict) else {}
        age = time.time() - float(relay.get("updated_epoch_sec", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        relay = {}
        age = float("inf")
    if age > RELAY_STATUS_FRESH_SECONDS:
        return CheckResult(
            "USB microphone export",
            "warn",
            f"{USBMIC_UNIT} is active but relay status is missing or stale",
            reason=REASON_MIC_EXPORT_RELAY_STALE,
        )
    audio_issue = relay_audio_issue(relay)
    if audio_issue:
        return CheckResult(
            "USB microphone export",
            "warn",
            f"advertised but audio progress is unhealthy: {audio_issue} "
            f"host_streaming={bool(relay.get('host_streaming'))}, "
            f"queue_drops={relay.get('periods_dropped', 0)}, "
            f"drop_rate={relay.get('drop_rate_periods_per_sec', 0)} periods/s",
            reason=REASON_MIC_EXPORT_AUDIO_UNHEALTHY,
        )
    host_streaming = bool(relay.get("host_streaming"))
    source_age_p95 = relay.get("source_age_ms_p95")
    try:
        source_age_p95_ms = (
            float(source_age_p95) if source_age_p95 is not None else None
        )
    except (TypeError, ValueError):
        source_age_p95_ms = None
    if source_age_p95_ms is not None and (
        not math.isfinite(source_age_p95_ms) or source_age_p95_ms < 0
    ):
        source_age_p95_ms = None
    metric_contract_ok = bool(
        relay.get("schema_version") == USB_MIC_RELAY_SCHEMA_VERSION
        and relay.get("source_age_basis") == USB_MIC_SOURCE_AGE_BASIS
        and relay.get("source_age_scope") == USB_MIC_SOURCE_AGE_SCOPE
    )
    if host_streaming and not metric_contract_ok:
        return CheckResult(
            "USB microphone export",
            "warn",
            "advertised and relay healthy, but active capture latency "
            "telemetry uses an unsupported schema or measurement scope",
            reason=REASON_MIC_EXPORT_METRIC_CONTRACT_UNSUPPORTED,
        )
    if host_streaming and source_age_p95_ms is None:
        return CheckResult(
            "USB microphone export",
            "warn",
            "advertised and relay healthy, but active capture latency "
            "telemetry is not yet available",
            reason=REASON_MIC_EXPORT_LATENCY_UNAVAILABLE,
        )
    if (
        host_streaming
        and source_age_p95_ms is not None
        and source_age_p95_ms > USB_MIC_LATENCY_WARN_MS
    ):
        return CheckResult(
            "USB microphone export",
            "warn",
            "advertised and relay healthy, but active capture latency is high: "
            f"source_age_p95={source_age_p95_ms:.1f} ms "
            f"(budget {USB_MIC_LATENCY_WARN_MS:.0f} ms)",
            reason=REASON_MIC_EXPORT_LATENCY_HIGH,
        )
    latency_detail = (
        f", source_age_p95={source_age_p95_ms:.1f} ms"
        if host_streaming and source_age_p95_ms is not None
        else ""
    )
    return CheckResult(
        "USB microphone export",
        "ok",
        "advertised and relay healthy; "
        f"host_streaming={host_streaming}, "
        f"queue_drops={int(relay.get('periods_dropped', 0))}"
        f"{latency_detail}",
    )

@doctor_check(order=59.8, group="usbsink")
def check_usb_combo_consistency() -> CheckResult:
    """Cross-check canonical USB permission against the resolved combo state.

    Two facts that must agree on a healthy combo box:

    1. EFFECTIVE PERMISSION — canonical ``source_intent.env`` says USB Audio
       Input is On *and* the current grouping role allows local sources.
       Desired-On on a bonded follower is intentionally parked, not drift.
       ``jasper-usbsink.service`` enablement is a derived composition mirror,
       not a second preference store. Invalid intent remains a loud failure.
    2. RESOLVED — ``fanin.env`` carries ``JASPER_FANIN_USB_DIRECT=enabled`` (the
       reconciler armed the combo so fan-in DIRECT-captures the gadget).

    Reported outcomes:

    - ``fail`` — ``jasper-usbsink.service`` is in the ``failed`` state (its
      composed-function or bounded ALSA-card readiness gate failed; USB audio is
      unavailable until the gadget is reconciled).
    - ``warn`` — USB audio is effectively wanted + gadget present but the combo
      was never armed (the coupling kick did not land — a failed wizard kick
      otherwise leaves no durable surface), or the combo is armed while
      canonical permission is Off.
    - ``ok`` — armed coherently, or cleanly disarmed (USB audio off / non-gadget
      box), with no failed unit.

    Skip-if-not-applicable: a box whose resolved USB role cannot carry a gadget
    reports ok with a skip note (check_usb_data_role owns the reason)."""
    from jasper.fanin.coupling_auto import (
        USB_COMBO_ENABLED_VALUE,
        USB_DIRECT_ENV_VAR,
        read_usb_gadget_available,
    )

    # 1. A failed readiness-marker unit is the most actionable state — report first.
    if _systemd_is_failed(USBSINK_UNIT):
        return CheckResult(
            "USB combo consistency", "fail",
            f"{USBSINK_UNIT} is in the failed state — the USB readiness marker "
            "did not pass its composed-function/card gate. USB audio is unavailable "
            "until recovery: "
            "`sudo systemctl reset-failed jasper-usbsink && sudo systemctl restart "
            "jasper-usbgadget`.",
            reason=REASON_COMBO_UNIT_FAILED,
        )

    gadget = read_usb_gadget_available()
    audio = _audio_wanted()
    if audio.reason == REASON_SOURCE_INTENT_INVALID:
        return CheckResult(
            "USB combo consistency",
            "fail",
            "USB Audio Input source intent is invalid or unreadable: "
            + audio.detail,
            reason=REASON_SOURCE_INTENT_INVALID,
        )
    from jasper.env_file import read_value
    from jasper.fanin.ring_health import FANIN_ENV_PATH

    try:
        fanin_text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        fanin_text = ""

    armed = read_value(fanin_text, USB_DIRECT_ENV_VAR) == USB_COMBO_ENABLED_VALUE

    if not gadget:
        return CheckResult(
            "USB combo consistency", "skipped",
            "resolved USB gadget unavailable — combo not applicable "
            "(see 'USB data role')",
            reason=REASON_COMBO_NOT_APPLICABLE,
        )
    if audio.wanted and not armed:
        return CheckResult(
            "USB combo consistency", "warn",
            "USB Audio Input is effectively wanted (intent enabled and role "
            "allowed) and the gadget is present, but the combo is NOT armed in "
            "fanin.env (JASPER_FANIN_USB_DIRECT != enabled) — the coupling "
            "reconcile likely did not run (a failed "
            "post-toggle kick). Re-run the /sources/ toggle or `sudo systemctl "
            "start jasper-fanin-coupling-auto.service`.",
            reason=REASON_COMBO_UNARMED,
        )
    if armed and not audio.wanted:
        return CheckResult(
            "USB combo consistency", "warn",
            f"combo is armed in fanin.env but USB Audio Input is not effectively "
            f"wanted ({audio.reason}) — a stale arm. `sudo systemctl start "
            "jasper-fanin-coupling-auto.service` to reconcile.",
            reason=REASON_COMBO_STALE_ARM,
        )
    if armed:
        return CheckResult(
            "USB combo consistency", "ok",
            "combo armed from canonical source intent (fan-in direct-captures "
            "the gadget as the sole live ingress owner)",
            reason=REASON_COMBO_ARMED,
        )
    return CheckResult(
        "USB combo consistency", "ok",
        f"combo disarmed (USB Audio Input {audio.reason}) — the fan-in DIRECT lane "
        "is off (USB audio inactive, as intended).",
        reason=REASON_COMBO_DISARMED,
    )

@doctor_check(order=62, group="usbsink")
def check_usbsink_name(modules_root: str = "/lib/modules") -> CheckResult:
    """When jasper-usbsink is enabled, verify the host-visible device
    name has been patched to track the Speaker Name.

    The kernel hardcodes the UAC2 playback/capture AudioStreaming strings that
    macOS shows as device names; configfs can't set them on 6.12, so
    jasper-usbsink-name-patch builds a name-patched
    `updates/` module override at bring-up. This check confirms the
    override exists, is genuinely patched, and matches the current
    Speaker Name + running kernel. A `warn` here is cosmetic only —
    USB audio still works, the host just shows the default label.

    ``modules_root`` is injectable for tests; production uses the real
    /lib/modules tree."""
    inactive = _skip_when_usbsink_inactive("usbsink name")
    if inactive is not None:
        return inactive

    # Reuse the canonical speaker-name reader (single source of truth for
    # how the name is parsed/validated) rather than re-implementing it.
    from jasper.speaker_name import runtime_name

    try:
        name = runtime_name()
    except Exception:  # noqa: BLE001 - malformed file/env; defer to default
        name = "JTS"
    kver = os.uname().release
    override = Path(f"{modules_root}/{kver}/updates/usb_f_uac2.ko")
    marker = Path(f"{modules_root}/{kver}/updates/.jasper-usbsink-name.marker")

    if not override.exists():
        return CheckResult(
            "usbsink name", "warn",
            "no name-patched module override — host shows the default "
            f"'Playback Inactive' label. Restart {USBGADGET_UNIT} "
            "and check `journalctl -u jasper-usbgadget "
            "-u jasper-usbsink-name-index | grep event=usbsink_name` "
            "(a kernel rename of the string degrades "
            "here gracefully; audio is unaffected).",
            reason=REASON_NAME_OVERRIDE_MISSING,
        )

    # The override must be a complete schema-3 patch — all four stock
    # alt-setting strings gone. Capture is checked even while p_chmask=0 so a
    # later switch cannot reveal an upgrade-stale label. The patcher publishes
    # no partial override; this scan keeps doctor truthful for manually copied
    # or upgrade-stale modules as well.
    try:
        override_bytes = override.read_bytes()
        if any(
            token in override_bytes
            for token in (
                b"Playback Inactive\x00",
                b"Playback Active\x00",
                b"Capture Inactive\x00",
                b"Capture Active\x00",
            )
        ):
            return CheckResult(
                "usbsink name", "warn",
                f"override {override} still contains the stock string — "
                f"patch did not take. Restart {USBGADGET_UNIT}.",
                reason=REASON_NAME_STOCK_STRING,
            )
    except OSError as exc:
        return CheckResult(
            "usbsink name", "warn",
            f"can't read {override}: {exc}",
            reason=REASON_NAME_OVERRIDE_UNREADABLE,
        )

    # Marker records (patch-schema, kernel, speaker name, derived mic name,
    # stock-hash). A mismatch means a transform, rename, or kernel bump has not
    # been re-applied yet.
    try:
        fields = marker.read_text().split("\t")
    except OSError:
        fields = []
    if (
        len(fields) >= 4
        and fields[0] == USB_NAME_PATCH_SCHEMA
        and fields[1] == kver
        and fields[2] == name
        and fields[3] == f"{name} Mic"
    ):
        return CheckResult(
            "usbsink name", "ok",
            f"speaker label tracks Speaker Name '{name}'; microphone label "
            f"tracks '{name} Mic' (kernel {kver}; each is truncated to its "
            "14-character USB slot while preserving the Mic suffix).",
            reason=REASON_NAME_PATCHED,
        )
    return CheckResult(
        "usbsink name", "warn",
        f"override present but stale for Speaker Name '{name}' / kernel "
        f"{kver} (marker={fields or 'missing'}). Restart "
        f"{USBGADGET_UNIT} to re-apply.",
        reason=REASON_NAME_STALE,
    )

@doctor_check(order=60, group="usbsink")
def check_usbsink_active_libcomposite() -> CheckResult:
    """The mirror of check_usbsink_state's RAM-drift check: when the
    readiness marker IS active but libcomposite is NOT loaded, the marker will
    appear active to systemd but audio won't flow (no gadget = no
    capture endpoint) regardless of whether the composite gadget also
    carries the network function. This asymmetry can happen if a user
    manually `rmmod libcomposite` while the daemon is up, or if
    jasper-usbgadget.service succeeded its modprobe but a subsequent
    reload unloaded the module. The jasper-usbgadget ↔ marker
    Requires=/After= chain normally prevents this, but a manual
    override breaks the invariant."""
    inactive = _skip_when_usbsink_inactive("usbsink active+modules")
    if inactive is not None:
        return inactive
    if _module_loaded("libcomposite"):
        return CheckResult(
            "usbsink active+modules", "ok",
            "service active, libcomposite loaded — consistent",
        )
    return CheckResult(
        "usbsink active+modules", "fail",
        "service active but libcomposite NOT loaded — audio won't "
        "flow even though the readiness marker appears healthy to systemd. "
        f"Run `systemctl restart {USBGADGET_UNIT}` to "
        "re-load the kernel module and re-compose the gadget.",
        reason=REASON_ACTIVE_MODULES_UNLOADED,
    )

@doctor_check(order=60.5, group="usbsink")
def check_usbgadget_composition() -> CheckResult:
    """The composed gadget functions must match the composed *intent*.

    deploy/usbsink/jasper-usbgadget-compose.sh holds the one function truth
    table; jasper-usbgadget-up computes it from there at start, and
    jasper-usbgadget-converge computes it again to decide whether a rebind is
    needed at all:

      network intent   audio authorized+ready    composed functions
      --------------   ------------------------  --------------------
      enabled          yes                       ncm.usb0 + uac2.usb0
      enabled          no / not ready            ncm.usb0
      disabled         yes                       uac2.usb0 (legacy shape)
      disabled         no / not ready            none (ExecCondition skip)

    This check recomputes the same desired composition in Python: network
    kill-switch plus canonical USB source intent/role authorization, the
    coordinator-derived unit-enablement mirror, and live fan-in DIRECT
    readiness. It compares that against the observed ConfigFS function
    directories. It is the composite-era
    replacement for the old "libcomposite loaded <=> usbsink active"
    invariant, which stopped holding the moment the network function could
    be composed alone. check_usbsink_state/check_usbsink_active_libcomposite
    own the *audio*-function split-brain/RAM-drift stories in more per-daemon
    detail; this check owns the *composition-as-a-whole* story, including the
    "gadget present but neither function should exist" and "network intent
    on but ncm.usb0 missing" cases those per-daemon checks can't see.

    A missing UDC (`/sys/class/udc` empty — pre-reboot fresh install, no
    peripheral role applied yet) is reported as ok/skip: check_usb_data_role
    already owns that gap, and jasper-usbgadget-wanted cleanly skips the
    unit in this state (not a unit failure), so there is nothing to compose
    yet regardless of intent."""
    label = "usbgadget composition"
    usb_role = current_usb_data_role()
    if not usb_role.management_transport_available:
        stale = (
            USBSINK_GADGET_PATH.exists()
            or _ncm_function_path().exists()
            or _uac2_function_path().exists()
        )
        if stale:
            return CheckResult(
                label,
                "fail",
                "gadget is composed while the resolved USB hardware role is "
                f"unavailable ({usb_role.reason}); stop {USBGADGET_UNIT} and "
                "reboot if a role change is pending.",
                reason=REASON_COMPOSITION_STALE_HARDWARE_MISMATCH,
            )
        return CheckResult(
            label,
            "skipped",
            f"nothing composed; USB gadget unavailable ({usb_role.reason})",
            reason=REASON_COMPOSITION_NOT_APPLICABLE,
        )
    if not usb_role.gadget_available and _uac2_function_path().exists():
        return CheckResult(
            label,
            "fail",
            "USB audio remains composed during a management-only role "
            f"transition ({usb_role.reason}); restart {USBGADGET_UNIT}.",
            reason=REASON_COMPOSITION_AUDIO_DURING_TRANSITION,
        )
    udc_dir = Path(os.environ.get("JASPER_UDC_CLASS_DIR", "/sys/class/udc"))
    try:
        has_udc = udc_dir.is_dir() and any(udc_dir.iterdir())
    except OSError:
        has_udc = False
    if not has_udc:
        return CheckResult(
            label, "skipped",
            "no UDC present (fresh install pre-reboot, or non-gadget-"
            "capable hardware) — see check_usb_data_role",
            reason=REASON_COMPOSITION_NO_UDC,
        )

    want_network = _network_wanted()
    audio = _audio_wanted()
    if audio.reason == REASON_SOURCE_INTENT_INVALID:
        return CheckResult(
            label,
            "fail",
            "USB Audio Input source intent is invalid or unreadable: "
            + audio.detail,
            reason=REASON_SOURCE_INTENT_INVALID,
        )
    ncm_present = _ncm_function_path().exists()
    uac2_present = _uac2_function_path().exists()
    intent = f"network={want_network} audio={audio.wanted} ({audio.reason})"
    observed = f"ncm.usb0={ncm_present} uac2.usb0={uac2_present}"
    if uac2_present:
        # Consumer state is DISCLOSED here, never a gate (ADR-0191). A composed
        # endpoint with no fan-in DIRECT lane plays into a void, which the
        # household can see and reason about; withdrawing the endpoint instead
        # is the invisible failure. Informational only: an idle box legitimately
        # reads consumed=False, so this must never become a warn.
        observed += (
            f" consumed={fanin_usbsink_lane_is_direct(read_fanin_status(timeout_sec=1.0))}"
        )

    if not want_network and not audio.wanted:
        if ncm_present or uac2_present or USBSINK_GADGET_PATH.exists():
            return CheckResult(
                label, "fail",
                f"gadget present but neither function should exist "
                f"({intent}; observed {observed}). Run "
                f"`systemctl restart {USBGADGET_UNIT}` to recompose (or "
                "tear down) the gadget.",
                reason=REASON_COMPOSITION_STALE,
            )
        return CheckResult(
            label, "ok",
            f"nothing composed, nothing wanted ({intent}) — zero-RAM "
            "contract intact",
            reason=REASON_COMPOSITION_ZERO_RAM,
        )

    mismatches: list[str] = []
    if want_network and not ncm_present:
        mismatches.append("network wanted but ncm.usb0 missing")
    if not want_network and ncm_present:
        mismatches.append("network not wanted but ncm.usb0 present")
    if audio.wanted and not uac2_present:
        mismatches.append("audio wanted but uac2.usb0 missing")
    if not audio.wanted and uac2_present:
        mismatches.append("audio not wanted but uac2.usb0 present")

    if mismatches:
        return CheckResult(
            label, "fail",
            f"{'; '.join(mismatches)} ({intent}; observed {observed}). "
            f"Run `systemctl restart {USBGADGET_UNIT}` to recompose.",
            reason=REASON_COMPOSITION_MISMATCH,
        )
    detail = f"composition matches intent ({intent}; observed {observed})"
    if usb_role.reboot_required:
        return CheckResult(
            label, "warn",
            detail + "; NCM retained only until the pending host-role reboot",
            reason=REASON_COMPOSITION_RETAINED_PENDING_REBOOT,
        )
    return CheckResult(label, "ok", detail)
