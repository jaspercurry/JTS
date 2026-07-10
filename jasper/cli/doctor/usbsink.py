# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — usbsink domain.

Originally re-homed verbatim from the monolithic ``jasper/cli/doctor.py``;
reworked for the composite-gadget model (docs/HANDOFF-usb-gadget.md). The
gadget is now ``jasper-usbgadget.service`` — a single ConfigFS owner that
composes up to two functions onto one UDC: ``ncm.usb0`` (the always-on USB
management network) and ``uac2.usb0`` (the wizard-toggled USB Audio Input,
still owned by ``jasper-usbsink.service``). The old invariant "libcomposite
loaded <=> usbsink active" no longer holds — libcomposite can be loaded for
the network function alone with the audio daemon fully off. The checks below
compare observed gadget/function state against the *composed intent*
(network kill-switch + audio enablement + follower-park gate), mirroring the
truth table ``jasper-usbgadget-up``/``jasper-usbgadget-wanted`` compute.
``check_usbsink_low_latency_contract`` is unchanged byte-for-byte — its UAC2
attribute contract does not depend on which other function is composed
alongside it."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from jasper.audio_runtime_plan import UAC2_LOW_LATENCY_EXPECTED_ATTRS
from jasper.audio_validation import route_live_state_issues

from ._registry import doctor_check
from ._shared import CheckResult, _parked_as_bonded_follower, _run

USBSINK_UNIT = "jasper-usbsink.service"
USBGADGET_UNIT = "jasper-usbgadget.service"
USBSINK_GADGET_PATH = Path("/sys/kernel/config/usb_gadget/jts-usb-audio")
UAC2_EXPECTED_LOW_LATENCY_ATTRS = UAC2_LOW_LATENCY_EXPECTED_ATTRS


def _format_rms_dbfs(raw: object) -> tuple[str | None, str | None]:
    if raw is None:
        return "unknown", None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, f"rms_dbfs not numeric: {raw!r}"
    value = float(raw)
    if not math.isfinite(value):
        return None, f"rms_dbfs not finite: {raw!r}"
    return f"{value:.1f}", None


def _systemd_is_active(unit: str) -> bool:
    """Wrapper around `systemctl is-active`. Cheap; ~5 ms per call."""
    return _run(["systemctl", "is-active", unit]).stdout.strip() == "active"

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
    the same convention every other doctor env read in this package
    uses (e.g. ``check_usbsink_host_clock``'s target-fill env read).

    NOT stripped: ``jasper-usbgadget-up`` matches the RAW value (no trim), so
    a whitespace-decorated ``" disabled"`` is a warned near-miss that STAYS
    enabled in bash. The Python readers must agree byte-for-byte, or
    check_usbgadget_composition would false-fail when bash composed ncm but
    Python thought the kill switch was set (review core-7). The fail-safe
    direction is deliberate: a stray space must never silently drop the
    always-on fallback network. Pinned by tests/test_usbgadget_script.py's
    literal matrix (bash) and test_doctor_usbsink.py (Python)."""
    raw = os.environ.get("JASPER_USB_NETWORK", "enabled")
    return raw.lower() != "disabled"


def _audio_wanted() -> tuple[bool, str]:
    """Mirror ``jasper-usbgadget-up``'s audio half of the truth table.

    Wanted when the /sources intent unit (``jasper-usbsink.service``) is
    enabled AND local sources are allowed (a bonded follower parks it).
    Returns ``(wanted, reason)`` — reason is one of the three the bash
    script logs (``enabled`` / ``parked_follower`` / ``intent_disabled``)
    so check details can explain *why* audio isn't composed without a
    second probe."""
    enabled = _run(["systemctl", "is-enabled", "--quiet", USBSINK_UNIT]).returncode == 0
    if not enabled:
        return False, "intent_disabled"
    if _parked_as_bonded_follower():
        return False, "parked_follower"
    return True, "enabled"

@doctor_check(order=57, group="usbsink")
def check_usbsink_dtoverlay() -> CheckResult:
    """Verify dtoverlay=dwc2,dr_mode=peripheral is in
    /boot/firmware/config.txt. Without it, the BCM2712 OTG controller
    stays in host mode and the USB-C port is power-only; the
    jasper-usbsink wizard toggle would be greyed out and turning it
    on (manually via systemctl) would just fail at the ConfigFS UDC
    bind."""
    cfg_path = Path("/boot/firmware/config.txt")
    if not cfg_path.exists():
        return CheckResult(
            "usbsink dtoverlay", "warn",
            f"{cfg_path} missing — not running on a Pi?",
        )
    try:
        content = cfg_path.read_text()
    except OSError as e:
        return CheckResult(
            "usbsink dtoverlay", "warn",
            f"can't read {cfg_path}: {e}",
        )
    needle = "dtoverlay=dwc2,dr_mode=peripheral"
    for line in content.splitlines():
        if line.strip().startswith(needle):
            return CheckResult(
                "usbsink dtoverlay", "ok",
                "dwc2 peripheral mode enabled (USB-C is gadget-capable)",
            )
    # Not present → not a fail, the feature is opt-in. Surface as a
    # warn-with-fix so a user wondering "why is the toggle greyed
    # out?" finds the answer here. install.sh's set_usb_gadget_mode
    # is idempotent so re-running install.sh + reboot recovers.
    return CheckResult(
        "usbsink dtoverlay", "warn",
        "not set; USB sink wizard toggle will show as unavailable. "
        "Re-run scripts/deploy-to-pi.sh (or sudo install.sh) and "
        "reboot to enable.",
    )

@doctor_check(order=58, group="usbsink")
def check_usbsink_state() -> CheckResult:
    """Status check for jasper-usbsink.service.

    When the service is active, verify the state file is being
    written recently (catches a wedged daemon that's somehow still
    showing active to systemd).

    When the service is inactive, verify the host-visible *audio* function
    (uac2.usb0) is also absent. A composed uac2.usb0 with the bridge down is
    a split-brain source state: computers still see JTS as USB audio while
    /sources can otherwise appear off. The composite gadget itself
    (jasper-usbgadget.service / ConfigFS dir) legitimately persists for the
    always-on management network even when audio is off — that alone is
    never a drift signal here; check_usbgadget_composition owns the
    gadget-vs-network-intent story. A leftover libcomposite module with
    NEITHER function composed is RAM drift (network kill-switched + audio
    off, but the module never unloaded)."""
    active = _systemd_is_active(USBSINK_UNIT)
    uac2_present = _uac2_function_path().exists()
    libcomp = _module_loaded("libcomposite")

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
            "parked (bonded follower) — bridge and uac2.usb0 function down"
            + (" (gadget may still carry ncm.usb0 for the management network)"
               if USBSINK_GADGET_PATH.exists() else ""),
        )

    if not active:
        if uac2_present:
            return CheckResult(
                "usbsink state",
                "fail",
                "bridge inactive but USB Audio Input is still advertised "
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
            "always-on USB management network — see check_usbgadget_composition)",
        )

    # Service is active. Verify the daemon is publishing state.
    state_path = Path("/run/jasper-usbsink/state.json")
    if not state_path.exists():
        return CheckResult(
            "usbsink state", "fail",
            f"service active but {state_path} missing — daemon may "
            "have crashed before publishing. Check "
            "`systemctl status jasper-usbsink` and journalctl.",
        )
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult(
            "usbsink state", "fail",
            f"can't parse {state_path}: {e}",
        )
    updated_str = data.get("updated_at")
    if not updated_str:
        return CheckResult(
            "usbsink state", "warn",
            "state file has no updated_at field — schema drift?",
        )
    try:
        from datetime import datetime, timezone
        updated = datetime.fromisoformat(updated_str)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (ValueError, TypeError):
        return CheckResult(
            "usbsink state", "warn",
            f"updated_at not ISO 8601: {updated_str!r}",
        )
    # State publisher writes at 1 Hz. >10 s of staleness = wedge.
    if age > 10:
        return CheckResult(
            "usbsink state", "warn",
            f"state file {age:.0f} s stale; daemon may be wedged "
            "(systemd watchdog should catch it within 15 s — check "
            "again in a moment)",
        )
    rms_text, rms_error = _format_rms_dbfs(data.get("rms_dbfs"))
    if rms_error is not None:
        return CheckResult(
            "usbsink state",
            "warn",
            f"{rms_error} — schema drift?",
        )
    if data.get("standby"):
        # Combo box: jasper-fanin DIRECT-captures the gadget and the bridge
        # stands by (JASPER_USBSINK_AUDIO_STANDBY=1, opens no PCM), so its
        # playing/rms_dbfs are frozen idle defaults that measure nothing — the
        # live audio flows through fan-in's direct lane. Report combo mode rather
        # than the meaningless numbers, so this diagnostic matches the honest
        # /state.renderers.usbsink projection (combo=true, playing/rms nulled)
        # instead of reading as "USB connected but silent" while it plays.
        return CheckResult(
            "usbsink state", "ok",
            "active (USB combo mode — jasper-fanin direct-captures the gadget; "
            "bridge in standby, playing/rms_dbfs not measured here) "
            f"host_connected={data.get('host_connected')}",
        )
    return CheckResult(
        "usbsink state", "ok",
        f"active, playing={data.get('playing')} "
        f"host_connected={data.get('host_connected')} "
        f"rms_dbfs={rms_text}",
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
    """When the route claims low latency, verify the USB bridge contract."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    if not plan.route_profile.low_latency_claim:
        return CheckResult(
            "usbsink low-latency contract",
            "ok",
            f"route_profile={plan.route_profile.route_id} has no USB low-latency claim",
        )

    state_path = Path("/run/jasper-usbsink/state.json")
    if not state_path.exists():
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            f"route_profile={plan.route_profile.route_id} requires Rust bridge state at {state_path}",
        )
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            f"can't parse {state_path}: {e}",
        )
    if data.get("implementation") != "rust":
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            "usb_low_latency_48k requires implementation='rust' in "
            f"{state_path}; got {data.get('implementation')!r}",
        )
    live_issues = tuple(
        issue
        for issue in route_live_state_issues(
            plan.route_latency_identity(),
            usbsink_state=data,
        )
        if issue.startswith("live_usbsink_")
    )
    if live_issues:
        return CheckResult(
            "usbsink low-latency contract",
            "fail",
            "usb_low_latency_48k live Rust bridge state does not match route "
            f"identity: {list(live_issues)}",
        )

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
        f"route_profile={plan.route_profile.route_id}, implementation=rust, "
        f"ring={data.get('ring')}, counters={data.get('counters')}"
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
def check_usbsink_host_clock() -> CheckResult:
    """Stage 1 host-slaved USB clock — default-OFF ladder telemetry.

    The Rust bridge always emits a ``host_clock`` block in
    ``state.json`` (also when the feature is disabled), so a missing
    block after a valid parse means a pre-Stage-1 build rather than a
    real failure. This check is pure telemetry surfacing — it never
    fails, because a mis-slaved host degrades to the same audio the
    speaker already produces without the feature (default-OFF, and
    the ladder itself falls back to neutral pitch on any evidence of
    non-compliance). See docs/HANDOFF-usb-low-latency.md "Host-slaved
    USB clock (Stage 1)" for the ladder/field semantics.

    Skips (ok, no detail beyond the reason) when:
      - the service is inactive (nothing to report)
      - state.json is missing/unparseable (check_usbsink_state already
        owns that failure mode)
      - the block is absent (pre-Stage-1 build)
      - the feature is disabled (default)

    Warns when the ladder has fallen back to L2 (with the reason +
    lifetime demotion count) or is at L1 (elevated commanded ppm).
    Otherwise ok, showing the live ladder/ppm/fill numbers.
    """
    if not _systemd_is_active(USBSINK_UNIT):
        return CheckResult(
            "usbsink host clock", "ok",
            "service disabled — host-clock check skipped",
        )
    state_path = Path("/run/jasper-usbsink/state.json")
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        # check_usbsink_state already reports a missing/corrupt state
        # file as fail; don't duplicate that failure here.
        return CheckResult(
            "usbsink host clock", "ok",
            f"can't read {state_path} — see 'usbsink state' check",
        )
    host_clock = data.get("host_clock")
    if not isinstance(host_clock, dict):
        return CheckResult(
            "usbsink host clock", "ok",
            "no host_clock block in state.json (pre-Stage-1 build)",
        )
    if not host_clock.get("enabled"):
        return CheckResult(
            "usbsink host clock", "ok",
            "disabled (default) — set JASPER_USBSINK_HOST_CLOCK=enabled "
            "to arm the ladder",
        )

    ladder = host_clock.get("ladder")
    pitch_ppm = host_clock.get("pitch_ppm_commanded")
    fill_frames = host_clock.get("fill_frames")
    try:
        target_fill = int(
            os.environ.get(
                "JASPER_USBSINK_HOST_CLOCK_TARGET_FILL_FRAMES", "384",
            ),
        )
    except ValueError:
        target_fill = 384
    detail = f"ladder={ladder} pitch_ppm={pitch_ppm} fill={fill_frames}/{target_fill}"

    if ladder == "l2_fallback":
        reason = host_clock.get("last_transition_reason")
        demotions = host_clock.get("demotions")
        return CheckResult(
            "usbsink host clock", "warn",
            f"{detail} — fell back to neutral pitch (reason={reason}, "
            f"lifetime demotions={demotions}). Host is not honoring "
            "feedback reliably; the ladder will re-probe on the next "
            "session.",
        )
    if ladder == "l1_warn":
        return CheckResult(
            "usbsink host clock", "warn",
            f"{detail} — locked but commanding an unusually large bias; "
            "watch for a demotion to L2.",
        )
    return CheckResult("usbsink host clock", "ok", detail)

@doctor_check(order=62, group="usbsink")
def check_usbsink_name(modules_root: str = "/lib/modules") -> CheckResult:
    """When jasper-usbsink is enabled, verify the host-visible device
    name has been patched to track the Speaker Name.

    The kernel hardcodes the UAC2 AudioStreaming string ("Playback
    Inactive") that macOS shows as the device name; configfs can't set
    it on 6.12, so jasper-usbsink-name-patch builds a name-patched
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

    # The override must be genuinely patched — the stock string gone.
    try:
        if b"Playback Inactive\x00" in override.read_bytes():
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

    # Marker records the (kernel, name, stock-hash) the override was
    # built for; a mismatch means a rename or kernel bump hasn't been
    # re-applied yet.
    try:
        fields = marker.read_text().split("\t")
    except OSError:
        fields = []
    if len(fields) >= 2 and fields[0] == kver and fields[1] == name:
        return CheckResult(
            "usbsink name", "ok",
            f"device name patched to track Speaker Name '{name}' "
            f"(kernel {kver}); host shows it (truncated to 15 chars).",
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
    daemon IS active but libcomposite is NOT loaded, the daemon will
    appear running to systemd but audio won't flow (no gadget = no
    capture endpoint) regardless of whether the composite gadget also
    carries the network function. This asymmetry can happen if a user
    manually `rmmod libcomposite` while the daemon is up, or if
    jasper-usbgadget.service succeeded its modprobe but a subsequent
    reload unloaded the module. The jasper-usbgadget ↔ daemon
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
        "flow even though the daemon appears healthy to systemd. "
        f"Run `systemctl restart {USBGADGET_UNIT}` to "
        "re-load the kernel module and re-compose the gadget.",
    )

@doctor_check(order=60.5, group="usbsink")
def check_usbgadget_composition() -> CheckResult:
    """The composed gadget functions must match the composed *intent*.

    jasper-usbgadget-up computes a function truth table once at start (see
    docs/HANDOFF-usb-gadget.md):

      network intent   audio intent         composed functions
      --------------   -------------------  --------------------
      enabled          enabled/allowed      ncm.usb0 + uac2.usb0
      enabled          off/parked follower  ncm.usb0
      disabled         enabled/allowed      uac2.usb0 (legacy shape)
      disabled         off/parked follower  none (unit skips via ExecCondition)

    This check recomputes the same intent in Python (network kill-switch env
    + `systemctl is-enabled jasper-usbsink` + the follower-park gate — the
    same predicates the bash truth table reads) and compares it against the
    observed ConfigFS function directories. It is the composite-era
    replacement for the old "libcomposite loaded <=> usbsink active"
    invariant, which stopped holding the moment the network function could
    be composed alone. check_usbsink_state/check_usbsink_active_libcomposite
    own the *audio*-function split-brain/RAM-drift stories in more per-daemon
    detail; this check owns the *composition-as-a-whole* story, including the
    "gadget present but neither function should exist" and "network intent
    on but ncm.usb0 missing" cases those per-daemon checks can't see.

    A missing UDC (`/sys/class/udc` empty — pre-reboot fresh install, no
    dtoverlay applied yet) is reported as ok/skip: check_usbsink_dtoverlay
    already owns that gap, and jasper-usbgadget-wanted cleanly skips the
    unit in this state (not a unit failure), so there is nothing to compose
    yet regardless of intent."""
    label = "usbgadget composition"
    udc_dir = Path(os.environ.get("JASPER_UDC_CLASS_DIR", "/sys/class/udc"))
    try:
        has_udc = udc_dir.is_dir() and any(udc_dir.iterdir())
    except OSError:
        has_udc = False
    if not has_udc:
        return CheckResult(
            label, "ok",
            "no UDC present (fresh install pre-reboot, or non-gadget-"
            "capable hardware) — see check_usbsink_dtoverlay",
        )

    want_network = _network_wanted()
    want_audio, audio_reason = _audio_wanted()
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
    return CheckResult(label, "ok", f"composition matches intent ({intent})")

@doctor_check(order=61, group="usbsink")
def check_usbsink_preempt_port_reachable() -> CheckResult:
    """Verify the mux's `_usbsink_set_preempt` URL actually resolves
    to a listening port on the daemon. Detects copy-paste drift
    between mux.USBSINK_PREEMPT_PORT and the Rust jasper-usbsink-audio
    daemon's preempt listener — both have env-var defaults that
    must agree at runtime. A silent mismatch means mux POSTs to
    nowhere; preempt protocol degrades to brief mixing without any
    surface error.

    Skips when usbsink is disabled. When enabled, opens a short TCP
    connect to the configured host:port and reports reachable / not.
    """
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult(
            "usbsink preempt port", "ok",
            "service disabled — port reachability skipped",
        )
    host = os.environ.get("JASPER_USBSINK_PREEMPT_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("JASPER_USBSINK_PREEMPT_PORT", "8781"))
    except ValueError:
        return CheckResult(
            "usbsink preempt port", "fail",
            "JASPER_USBSINK_PREEMPT_PORT is not an integer",
        )
    # Short TCP connect — 500 ms is plenty on localhost; any longer
    # and something else is wrong.
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
    except OSError as e:
        return CheckResult(
            "usbsink preempt port", "fail",
            f"daemon active but {host}:{port} not reachable: {e}. "
            "Mux's preempt POSTs will fail silently — check that "
            "JASPER_USBSINK_PREEMPT_PORT matches between the daemon "
            "and mux env files.",
        )
    finally:
        sock.close()
    return CheckResult(
        "usbsink preempt port", "ok",
        f"daemon listening on {host}:{port} (mux preempts will land)",
    )


def _unit_main_start_epoch(unit: str) -> float | None:
    """Wall-clock epoch (seconds) when ``unit``'s main process last started, or None.

    Derived from systemd's ``ExecMainStartTimestampMonotonic`` (µs since boot) plus
    ``/proc/uptime`` so the comparison is boot-relative and immune to wall-clock
    steps (NTP jumps) between start and now: ``boot_epoch = now - uptime`` then
    ``start_epoch = boot_epoch + start_us/1e6``. Returns None when the field is
    absent / unparsable / ``0`` (never started) or ``/proc/uptime`` is unreadable —
    the caller then skips the drift check rather than guessing.
    """
    import time

    out = _run(
        [
            "systemctl", "show", unit,
            "-p", "ExecMainStartTimestampMonotonic", "--value",
        ]
    )
    raw = out.stdout.strip()
    if not raw.isdigit():
        return None
    start_us = int(raw)
    if start_us == 0:  # not currently running under this main PID
        return None
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    boot_epoch = time.time() - uptime
    return boot_epoch + start_us / 1_000_000.0


# The reconciler writes usbsink.env then restarts the daemon in the same call, so
# on a converged box the daemon start is comfortably AFTER the env mtime. Require
# the env to be newer than the start by this margin before calling it drift, so a
# within-the-same-second write→restart ordering can't read as a false positive.
_USBSINK_ENV_DRIFT_SLACK_SEC = 5.0


@doctor_check(order=61.5, group="usbsink")
def check_usbsink_env_drift() -> CheckResult:
    """Catch jasper-usbsink running STALE env after a rate-limited reconcile (defect D).

    ``jasper-audio-hardware-reconcile`` rewrites ``usbsink.env`` whenever the route
    profile changes, then try-restarts the daemon — but that restart is rate-limited
    (a usbsink restart recomposes the USB gadget → a fresh udev card-add → the
    reconciler runs again; the limiter breaks that storm). When the limiter REFUSES,
    the env file has already been rewritten while the daemon keeps the old geometry,
    and nothing schedules a retry — convergence would otherwise wait unbounded for
    the next udev event / boot / manual run ("I changed the route and nothing
    happened"). This standing check makes that drift observable: if usbsink.env's
    mtime is newer than the running daemon's start, the daemon is serving stale env.

    Skips cleanly when USB Audio isn't wanted (disabled / parked follower — the env
    is irrelevant), when the daemon isn't active, or when the daemon start time /
    env mtime can't be read (indeterminate → don't guess). Remediation is a single
    restart: the limiter only damps automatic reconciler churn, not an operator
    action.
    """
    wanted, reason = _audio_wanted()
    if not wanted:
        return CheckResult(
            "usbsink env drift", "ok",
            f"USB Audio not active ({reason}) — env drift irrelevant",
        )
    if not _systemd_is_active(USBSINK_UNIT):
        return CheckResult(
            "usbsink env drift", "ok",
            "service enabled but not active — env drift check skipped",
        )

    from jasper.fanin.coupling_reconcile import USBSINK_ENV_PATH

    try:
        env_mtime = os.stat(USBSINK_ENV_PATH).st_mtime
    except OSError:
        # No env file → the daemon runs on defaults; there is nothing to drift from.
        return CheckResult(
            "usbsink env drift", "ok",
            f"{USBSINK_ENV_PATH} absent — daemon on defaults, no drift",
        )

    start_epoch = _unit_main_start_epoch(USBSINK_UNIT)
    if start_epoch is None:
        return CheckResult(
            "usbsink env drift", "ok",
            "daemon start time unavailable — drift check skipped",
        )

    drift = env_mtime - start_epoch
    if drift > _USBSINK_ENV_DRIFT_SLACK_SEC:
        return CheckResult(
            "usbsink env drift", "warn",
            f"{USBSINK_ENV_PATH} was rewritten {drift:.0f} s AFTER jasper-usbsink "
            "started — the daemon is serving stale route env (a reconcile restart "
            "was likely rate-limited). Run `sudo systemctl restart "
            "jasper-usbsink.service jasper-usbsink-volume.service` to converge (the "
            "reconciler's limiter only damps automatic churn, not this manual "
            "restart).",
        )
    return CheckResult(
        "usbsink env drift", "ok",
        "daemon started after the current usbsink.env (running live route env)",
    )
