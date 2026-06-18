"""jasper-doctor checks — usbsink domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import json
import os
from pathlib import Path
from ._registry import doctor_check
from ._shared import CheckResult, _run

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

    When the service is inactive, verify libcomposite is NOT loaded —
    if it is, the previous stop didn't tear cleanly and the gadget
    descriptor is leaking RAM (~60 KB). Not catastrophic but worth
    surfacing so the operator can `sudo rmmod libcomposite` or reboot.
    """
    active = _systemd_is_active("jasper-usbsink.service")
    libcomp = _module_loaded("libcomposite")

    if not active:
        if libcomp:
            return CheckResult(
                "usbsink state", "warn",
                "service inactive but libcomposite still loaded — "
                "RAM drift from a failed stop. Reboot or "
                "`sudo rmmod u_audio libcomposite` to recover.",
            )
        return CheckResult(
            "usbsink state", "ok",
            "disabled (no RAM cost beyond ~50 KB dwc2 module)",
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
    return CheckResult(
        "usbsink state", "ok",
        f"active, playing={data.get('playing')} "
        f"host_connected={data.get('host_connected')} "
        f"rms_dbfs={data.get('rms_dbfs'):.1f}",
    )

@doctor_check(order=59, group="usbsink")
def check_usbsink_card() -> CheckResult:
    """When jasper-usbsink is enabled, the UAC2Gadget ALSA card MUST
    be present — otherwise the init.service either didn't run or
    failed to bind to UDC."""
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
        "init.service didn't bind the UDC. Check "
        "`systemctl status jasper-usbsink-init` for the failure mode.",
    )

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
            "'Playback Inactive' label. Restart jasper-usbsink-init "
            "and check `journalctl -u jasper-usbsink-init | grep "
            "event=usbsink_name` (a kernel rename of the string degrades "
            "here gracefully; audio is unaffected).",
        )

    # The override must be genuinely patched — the stock string gone.
    try:
        if b"Playback Inactive\x00" in override.read_bytes():
            return CheckResult(
                "usbsink name", "warn",
                f"override {override} still contains the stock string — "
                "patch did not take. Restart jasper-usbsink-init.",
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
        "jasper-usbsink-init to re-apply.",
    )

@doctor_check(order=60, group="usbsink")
def check_usbsink_active_libcomposite() -> CheckResult:
    """The mirror of check_usbsink_state's RAM-drift check: when the
    daemon IS active but libcomposite is NOT loaded, the daemon will
    appear running to systemd but audio won't flow (no gadget = no
    capture endpoint). This asymmetry can happen if a user manually
    `rmmod libcomposite` while the daemon is up, or if init.service
    succeeded its modprobe but a subsequent reload unloaded the
    module. The init.service ↔ daemon PartOf= chain normally prevents
    this, but a manual override breaks the invariant."""
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
        "Run `systemctl restart jasper-usbsink-init.service` to "
        "re-load the kernel module and re-bind the gadget.",
    )

@doctor_check(order=61, group="usbsink")
def check_usbsink_preempt_port_reachable() -> CheckResult:
    """Verify the mux's `_usbsink_set_preempt` URL actually resolves
    to a listening port on the daemon. Detects copy-paste drift
    between mux.USBSINK_PREEMPT_PORT and
    preempt_listener.DEFAULT_PORT — both have env-var defaults that
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
