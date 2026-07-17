# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — usbsink domain.

Originally re-homed verbatim from the monolithic ``jasper/cli/doctor.py``;
reworked for the composite-gadget model (docs/HANDOFF-usb-gadget.md). The
gadget is now ``jasper-usbgadget.service`` — a single ConfigFS owner that
composes up to two functions onto one UDC: ``ncm.usb0`` (the USB
management network) and ``uac2.usb0`` (the wizard-toggled USB Audio Input,
whose readiness marker is ``jasper-usbsink.service``). The old invariant
"libcomposite loaded <=> usbsink active" no longer holds — libcomposite can be
loaded for the network function alone with USB audio fully off. The checks below
compare observed gadget/function state against the *composed intent*
(network kill-switch + canonical audio authorization + derived lifecycle
readiness), mirroring the truth table
``jasper-usbgadget-up``/``jasper-usbgadget-wanted`` compute.
``check_usbsink_low_latency_contract`` reads the actual fan-in direct-capture
lane; the oneshot marker is lifecycle/readiness state, not data-plane liveness
or latency evidence."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time

from jasper.audio_hardware.usb_port_role import gadget_unavailable_detail
from jasper.audio_runtime_plan import UAC2_LOW_LATENCY_EXPECTED_ATTRS
from jasper.audio_validation import route_live_state_issues
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
from ._shared import CheckResult, _parked_as_bonded_follower, _run

USBSINK_UNIT = "jasper-usbsink.service"
USBGADGET_UNIT = "jasper-usbgadget.service"
USBSINK_GADGET_PATH = Path("/sys/kernel/config/usb_gadget/jts-usb-audio")
UAC2_EXPECTED_LOW_LATENCY_ATTRS = UAC2_LOW_LATENCY_EXPECTED_ATTRS
USB_NAME_PATCH_SCHEMA = "3"


def _systemd_is_active(unit: str) -> bool:
    """Wrapper around `systemctl is-active`. Cheap; ~5 ms per call."""
    return _run(["systemctl", "is-active", unit]).stdout.strip() == "active"

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


def _uac2_function_path() -> Path:
    return USBSINK_GADGET_PATH / "functions" / "uac2.usb0"


def _ncm_function_path() -> Path:
    return USBSINK_GADGET_PATH / "functions" / "ncm.usb0"


def _network_wanted() -> bool:
    """Mirror ``jasper-usbgadget-up``'s network half of the truth table.

    Network is wanted unless the kill switch is the exact literal
    ``disabled`` (case-insensitive); any other value is treated as
    enabled, same as ``JASPER_SHAIRPORT_SUPERVISOR`` /
    ``JASPER_SYSTEM_SUPERVISOR``. Read from ``os.environ`` (not a fresh
    file parse) because ``jasper.env_load`` already unions
    ``/etc/jasper/jasper.env`` into ``os.environ`` at CLI startup —
    the same convention every other doctor env read in this package uses.

    NOT stripped: ``jasper-usbgadget-up`` matches the RAW value (no trim), so
    a whitespace-decorated ``" disabled"`` is a warned near-miss that STAYS
    enabled in bash. The Python readers must agree byte-for-byte, or
    check_usbgadget_composition would false-fail when bash composed ncm but
    Python thought the kill switch was set (review core-7). The fail-safe
    direction is deliberate: a stray space must never silently drop the
    default-on fallback network when hardware permits it. Pinned by
    tests/test_usbgadget_script.py's
    literal matrix (bash) and test_doctor_usbsink.py (Python)."""
    raw = os.environ.get("JASPER_USB_NETWORK", "enabled")
    return raw.lower() != "disabled"


def _audio_wanted() -> tuple[bool, str]:
    """Return canonical USB-audio authorization before lifecycle readiness.

    Wanted when the canonical /sources intent is enabled AND local sources are
    allowed (a bonded follower parks it). Unit enablement is derived state and
    is checked separately where drift matters.
    Returns ``(wanted, reason)`` so callers can distinguish intent Off,
    follower parking, invalid intent, and effective authorization."""
    try:
        enabled = source_intent_enabled(Source.USBSINK)
    except RuntimeError as exc:
        return False, f"intent_invalid:{exc}"
    if not enabled:
        return False, "intent_disabled"
    if _parked_as_bonded_follower():
        return False, "parked_follower"
    return True, "enabled"


def _audio_composition_wanted() -> tuple[bool, str]:
    """Apply every gadget audio-readiness gate to authorization.

    Keep this in lockstep with ``jasper-usbgadget-up`` and
    ``jasper-usbgadget-wanted``: advertising UAC2 is safe only after canonical
    authorization, derived unit enablement, and a live fan-in DIRECT consumer.
    """

    allowed, reason = _audio_wanted()
    if not allowed:
        return False, reason
    if _run(
        ["systemctl", "is-enabled", "--quiet", USBSINK_UNIT]
    ).returncode != 0:
        return False, "derived_unit_disabled"
    if not fanin_usbsink_lane_is_direct(read_fanin_status(timeout_sec=1.0)):
        return False, "direct_lane_unarmed"
    return True, "enabled"

@doctor_check(order=57, group="usbsink")
def check_usb_data_role() -> CheckResult:
    """Explain the resolved host/peripheral role and pending reboot state."""

    try:
        state = current_usb_data_role()
    except (OSError, RuntimeError, ValueError) as exc:
        return CheckResult(
            "USB data role", "warn", f"capability state unavailable: {exc}"
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
        )
    if state.gadget_available:
        return CheckResult(
            "USB data role", "ok", f"{detail}; USB gadget available"
        )
    return CheckResult(
        "USB data role",
        "ok" if state.desired_role == "host" else "warn",
        f"{detail}; {gadget_unavailable_detail(state)}",
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
            )
        return CheckResult(
            "usbsink state",
            "ok",
            f"USB Audio Input unavailable as resolved ({usb_role.reason})",
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
            )
        return CheckResult(
            "usbsink state",
            "ok",
            "parked (bonded follower) — readiness marker and uac2.usb0 function down"
            + (" (gadget may still carry ncm.usb0 for the management network)"
               if USBSINK_GADGET_PATH.exists() else ""),
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
            )
        if libcomp and not USBSINK_GADGET_PATH.exists():
            return CheckResult(
                "usbsink state", "warn",
                "service inactive, uac2.usb0 absent, but libcomposite still "
                "loaded with no gadget directory — RAM drift from a failed "
                "stop. Reboot or `sudo rmmod u_audio libcomposite` to "
                "recover.",
            )
        return CheckResult(
            "usbsink state", "ok",
            "USB Audio Input disabled (uac2.usb0 not composed; the "
            "composite gadget/libcomposite may still be resident for the "
            "hardware-conditional USB management network — see "
            "check_usbgadget_composition)",
        )

    if not uac2_present:
        return CheckResult(
            "usbsink state",
            "fail",
            "readiness marker active but uac2.usb0 is absent — restart "
            f"{USBGADGET_UNIT} so the marker re-runs its bounded card gate.",
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
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink card", "ok",
            "service disabled — card check skipped",
        )
    if Path("/proc/asound/UAC2Gadget").is_dir():
        return CheckResult(
            "usbsink card", "ok",
            "UAC2Gadget card present (host will see the speaker as USB audio)",
        )
    return CheckResult(
        "usbsink card", "fail",
        "service active but /proc/asound/UAC2Gadget missing — "
        f"{USBGADGET_UNIT} didn't compose/bind uac2.usb0. Check "
        f"`systemctl status {USBGADGET_UNIT}` for the failure mode.",
    )


@doctor_check(order=59.5, group="usbsink")
def check_usbsink_low_latency_contract() -> CheckResult:
    """When the route claims low latency, verify the live USB data plane."""

    usb_role = current_usb_data_role()
    if not usb_role.gadget_available:
        return CheckResult(
            "usbsink low-latency contract",
            "ok",
            f"not applicable: USB gadget unavailable ({usb_role.reason})",
        )

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    if not plan.route_profile.low_latency_claim:
        return CheckResult(
            "usbsink low-latency contract",
            "ok",
            f"route_profile={plan.route_profile.route_id} has no USB low-latency claim",
        )

    audio_wanted, audio_reason = _audio_wanted()
    if not audio_wanted:
        if audio_reason.startswith("intent_invalid:"):
            return CheckResult(
                "usbsink low-latency contract",
                "fail",
                f"USB source intent is invalid: {audio_reason.removeprefix('intent_invalid:')}",
            )
        return CheckResult(
            "usbsink low-latency contract",
            "ok",
            "live USB low-latency check not applicable: "
            f"route_profile={plan.route_profile.route_id}, {audio_reason}",
        )

    try:
        fanin_status = read_status_socket(FANIN_STATUS_SOCKET)
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            f"can't read fan-in STATUS at {FANIN_STATUS_SOCKET}: {e}",
        )
    live_issues = tuple(
        route_live_state_issues(
            plan.route_latency_identity(),
            fanin_status=fanin_status,
            allow_idle_direct_lane=True,
        )
    )
    if live_issues:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            "usb_low_latency_48k live fan-in direct-capture state does not match route "
            f"identity: {list(live_issues)}",
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
        )
    if missing:
        return CheckResult(
            "usbsink low-latency contract",
            "warn",
            detail + "; kernel does not expose UAC2 attrs: " + ", ".join(missing),
        )
    return CheckResult("usbsink low-latency contract", "ok", detail)


@doctor_check(order=59.7, group="usbsink")
def check_usb_mic_export() -> CheckResult:
    """Cross-check USB-microphone intent, descriptor, relay, and privacy."""

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
        )
    if not intent.enabled:
        if advertised:
            return CheckResult(
                "USB microphone export",
                "fail",
                "preference is Off but UAC2 p_chmask=1 still advertises a host "
                f"microphone; restart {USBGADGET_UNIT}",
            )
        if function.is_dir() and bcd_device != USB_NO_MIC_BCD_DEVICE:
            return CheckResult(
                "USB microphone export",
                "fail",
                "preference is Off but the composed UAC2 descriptor revision is "
                f"{bcd_device or 'missing'}, expected {USB_NO_MIC_BCD_DEVICE}; "
                f"restart {USBGADGET_UNIT}",
            )
        return CheckResult(
            "USB microphone export", "ok", "disabled; host microphone absent",
        )

    audio_wanted, audio_reason = _audio_composition_wanted()
    if not audio_wanted:
        return CheckResult(
            "USB microphone export",
            "warn",
            "preference is On but USB Audio Input is unavailable "
            f"({audio_reason}); saved intent will apply when that source recovers",
        )
    if not advertised:
        return CheckResult(
            "USB microphone export",
            "fail",
            "preference is On but UAC2 does not advertise the mono host input "
            f"(p_chmask={p_chmask or 'missing'}); restart {USBGADGET_UNIT}",
        )
    if bcd_device != USB_MIC_BCD_DEVICE:
        return CheckResult(
            "USB microphone export",
            "fail",
            "host microphone is advertised but the descriptor revision is "
            f"{bcd_device or 'missing'}, expected {USB_MIC_BCD_DEVICE}; "
            f"restart {USBGADGET_UNIT}",
        )
    if not _systemd_is_active(USBMIC_UNIT):
        return CheckResult(
            "USB microphone export",
            "fail",
            f"host microphone is advertised but {USBMIC_UNIT} is inactive",
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
        )
    if host_streaming and source_age_p95_ms is None:
        return CheckResult(
            "USB microphone export",
            "warn",
            "advertised and relay healthy, but active capture latency "
            "telemetry is not yet available",
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

    Three facts that must agree on a healthy combo box:

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
      was never armed (the coupling kick did not land — the PR #1197 nit: a
      failed wizard kick otherwise leaves no durable surface), or the combo is
      armed while canonical permission is Off.
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
        )

    gadget = read_usb_gadget_available()
    audio_wanted, audio_reason = _audio_wanted()
    if audio_reason.startswith("intent_invalid:"):
        return CheckResult(
            "USB combo consistency",
            "fail",
            "USB Audio Input source intent is invalid or unreadable: "
            + audio_reason.removeprefix("intent_invalid:"),
        )
    from jasper.env_file import read_value
    from jasper.fanin.coupling_reconcile import FANIN_ENV_PATH

    try:
        fanin_text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        fanin_text = ""

    armed = read_value(fanin_text, USB_DIRECT_ENV_VAR) == USB_COMBO_ENABLED_VALUE

    if not gadget:
        return CheckResult(
            "USB combo consistency", "ok",
            "resolved USB gadget unavailable — combo not applicable "
            "(see 'USB data role')",
        )
    if audio_wanted and not armed:
        return CheckResult(
            "USB combo consistency", "warn",
            "USB Audio Input is effectively wanted (intent enabled and role "
            "allowed) and the gadget is present, but the combo is NOT armed in "
            "fanin.env (JASPER_FANIN_USB_DIRECT != enabled) — the coupling "
            "reconcile likely did not run (a failed "
            "post-toggle kick). Re-run the /sources/ toggle or `sudo systemctl "
            "start jasper-fanin-coupling-auto.service`.",
        )
    if armed and not audio_wanted:
        return CheckResult(
            "USB combo consistency", "warn",
            f"combo is armed in fanin.env but USB Audio Input is not effectively "
            f"wanted ({audio_reason}) — a stale arm. `sudo systemctl start "
            "jasper-fanin-coupling-auto.service` to reconcile.",
        )
    if armed:
        return CheckResult(
            "USB combo consistency", "ok",
            "combo armed from canonical source intent (fan-in direct-captures "
            "the gadget as the sole live ingress owner)",
        )
    return CheckResult(
        "USB combo consistency", "ok",
        f"combo disarmed (USB Audio Input {audio_reason}) — the fan-in DIRECT lane "
        "is off (USB audio inactive, as intended).",
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
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink name", "ok",
            "service disabled — device-name check skipped",
        )

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
            "and check `journalctl -u jasper-usbsink-name-patch | grep "
            "event=usbsink_name` (a kernel rename of the string degrades "
            "here gracefully; audio is unaffected).",
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
            )
    except OSError as exc:
        return CheckResult(
            "usbsink name", "warn",
            f"can't read {override}: {exc}",
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
        )
    return CheckResult(
        "usbsink name", "warn",
        f"override present but stale for Speaker Name '{name}' / kernel "
        f"{kver} (marker={fields or 'missing'}). Restart "
        f"{USBGADGET_UNIT} to re-apply.",
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
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink active+modules", "ok",
            "service disabled — module check skipped",
        )
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
    )

@doctor_check(order=60.5, group="usbsink")
def check_usbgadget_composition() -> CheckResult:
    """The composed gadget functions must match the composed *intent*.

    jasper-usbgadget-up computes a function truth table once at start (see
    docs/HANDOFF-usb-gadget.md):

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
            )
        return CheckResult(
            label,
            "ok",
            f"nothing composed; USB gadget unavailable ({usb_role.reason})",
        )
    if not usb_role.gadget_available and _uac2_function_path().exists():
        return CheckResult(
            label,
            "fail",
            "USB audio remains composed during a management-only role "
            f"transition ({usb_role.reason}); restart {USBGADGET_UNIT}.",
        )
    udc_dir = Path(os.environ.get("JASPER_UDC_CLASS_DIR", "/sys/class/udc"))
    try:
        has_udc = udc_dir.is_dir() and any(udc_dir.iterdir())
    except OSError:
        has_udc = False
    if not has_udc:
        return CheckResult(
            label, "ok",
            "no UDC present (fresh install pre-reboot, or non-gadget-"
            "capable hardware) — see check_usb_data_role",
        )

    want_network = _network_wanted()
    want_audio, audio_reason = _audio_composition_wanted()
    if audio_reason.startswith("intent_invalid:"):
        return CheckResult(
            label,
            "fail",
            "USB Audio Input source intent is invalid or unreadable: "
            + audio_reason.removeprefix("intent_invalid:"),
        )
    ncm_present = _ncm_function_path().exists()
    uac2_present = _uac2_function_path().exists()
    intent = f"network={want_network} audio={want_audio} ({audio_reason})"
    observed = f"ncm.usb0={ncm_present} uac2.usb0={uac2_present}"

    if not want_network and not want_audio:
        if ncm_present or uac2_present or USBSINK_GADGET_PATH.exists():
            return CheckResult(
                label, "fail",
                f"gadget present but neither function should exist "
                f"({intent}; observed {observed}). Run "
                f"`systemctl restart {USBGADGET_UNIT}` to recompose (or "
                "tear down) the gadget.",
            )
        return CheckResult(
            label, "ok",
            f"nothing composed, nothing wanted ({intent}) — zero-RAM "
            "contract intact",
        )

    mismatches: list[str] = []
    if want_network and not ncm_present:
        mismatches.append("network wanted but ncm.usb0 missing")
    if not want_network and ncm_present:
        mismatches.append("network not wanted but ncm.usb0 present")
    if want_audio and not uac2_present:
        mismatches.append("audio wanted but uac2.usb0 missing")
    if not want_audio and uac2_present:
        mismatches.append("audio not wanted but uac2.usb0 present")

    if mismatches:
        return CheckResult(
            label, "fail",
            f"{'; '.join(mismatches)} ({intent}; observed {observed}). "
            f"Run `systemctl restart {USBGADGET_UNIT}` to recompose.",
        )
    status = "warn" if usb_role.reboot_required else "ok"
    suffix = (
        "; NCM retained only until the pending host-role reboot"
        if usb_role.reboot_required
        else ""
    )
    return CheckResult(
        label,
        status,
        f"composition matches intent ({intent}){suffix}",
    )
