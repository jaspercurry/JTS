# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sources on/off page at /sources/.

Playback-source toggles:

  - AirPlay and Spotify Connect use ordinary systemd lifecycle operations.
  - Bluetooth uses RF-kill, BlueZ power, and its audio/pairing services.
  - USB Audio Input preserves the ordered composite-gadget transition that
    keeps the always-on USB management network up while adding/removing audio.

The web process owns none of those mechanisms. It records one desired source
state and kicks ``jasper-source-intent-reconcile``; that fixed root oneshot is
the sole lifecycle coordinator for all four sources. The state response keeps
desired intent separate from the observed effective state so a failed service
start cannot silently flip the user's choice back.

AirPlay, Bluetooth, and Spotify Connect default ON. USB Audio Input
defaults OFF so it has zero resident RAM cost until explicitly enabled.
The toggle is the only knob; there's no per-source settings on this page.

State polling: clients GET /state every few seconds to reflect external
changes (operator ran `systemctl stop shairport-sync` from SSH, etc.).
When a renderer unit or its hardware is not installed, the page is still
present and explains what is missing. An unavailable source that is already
Off cannot be turned On; a stale desired-On source can always be turned Off so
the safest recovery choice never depends on the missing component.

This page renders on the canonical design system (canonical_page); its
behaviour ships as the static ES module deploy/assets/sources/js/main.js,
not inline <script>. The routes, JSON shapes, CSRF gate, systemctl/DBus
backends, and fail-soft logging are unchanged from the legacy look.

URL surface (after nginx strips /sources/):
  GET  /         page render
  GET  /state    source → {enabled, desired, effective, available, ...}
  POST /set      {source, enabled} → same shape as /state on success
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..bluetooth.availability import (
    BLUETOOTH_CONTROL_PLANE_UNIT,
    BluetoothAvailability,
    bluetooth_unavailable_reason,
    probe_bluetooth_availability,
)
from ..fanin.combo_health import (
    DIRECT_HEALTH_CAPTURING,
    DIRECT_HEALTH_IDLE,
    extract_direct_sample,
)
from ..fanin.status import read_fanin_status
from ..install_profile import install_profile_allows_local_sources, read_install_profile
from ..local_sources import local_source_lifecycle
from ..log_event import log_event
from ..music_sources import MUSIC_SOURCE_SPECS, Source
from ..source_intent import (
    read_source_intents,
    request_source_intent,
    source_intent_enabled,
)
from ._common import (
    JsonBodyError,
    bonded_follower_active,
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    reject_csrf,
    read_json_object,
    send_html_response,
    send_json_response,
    toggle_html,
    guard_read_request,
    guard_mutating_request,
)
from ._unit_snapshot import UnitSnapshot, probe_unit_snapshot

logger = logging.getLogger(__name__)


# /set carries only a source key and boolean; reject larger direct-socket
# bodies before any source lifecycle mutation.
_JSON_BODY_LIMIT = 4096
_BLUETOOTH_STATE_TIMEOUT_SEC = 5.0


def _intent_unit(source: Source) -> str:
    unit = local_source_lifecycle(source).intent_unit
    if unit is None:
        raise RuntimeError(f"{source.value} has no systemd intent unit")
    return unit


def _single_unit(units: tuple[str, ...], label: str) -> str:
    if len(units) != 1:
        raise RuntimeError(f"{label} expected one unit, got {units!r}")
    return units[0]


# jasper-usbsink.service is the derived USB lifecycle unit; canonical intent is
# owned by jasper.source_intent. The composite gadget unit owns the host-visible
# ConfigFS gadget (always-on USB network + the user-toggled uac2 audio function).
USBSINK_UNIT = _intent_unit(Source.USBSINK)
USBSINK_GADGET_UNIT = _single_unit(
    local_source_lifecycle(Source.USBSINK).advertise_units,
    "USB Audio Input advertise units",
)
VALID_SOURCES = tuple(spec.wizard_key for spec in MUSIC_SOURCE_SPECS)
SOURCE_BY_WIZARD_KEY = {spec.wizard_key: spec.id for spec in MUSIC_SOURCE_SPECS}
SOURCE_UNAVAILABLE = {
    "airplay": (
        "AirPlay is not installed on this speaker. Re-run install.sh to "
        "set up the local renderer stack."
    ),
    "spotify_connect": (
        "Spotify Connect is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
    "bluetooth": (
        "Bluetooth audio is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
    "usbsink": (
        "USB Audio Input is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
}
IDLE_SHUTDOWN_SEC = 600.0

# /boot/firmware/config.txt line that install.sh's set_usb_gadget_mode
# writes. Without this, the BCM2712 OTG controller stays in host mode
# (the Pi 5 default) and the USB-C port is power-only — flipping the
# wizard toggle on would just fail at the init.service ConfigFS
# write. The wizard surfaces this as `available: false` so the row
# shows disabled instead of presenting a broken on/off.
BOOT_CONFIG_PATH = "/boot/firmware/config.txt"
USBSINK_DTOVERLAY_LINE = "dtoverlay=dwc2,dr_mode=peripheral"


# The ALSA card the composite gadget's uac2 function registers. Its presence
# is the host-visible "USB audio device is advertised" signal now that the
# gadget unit is always-on (it also carries the USB network), so gadget-active
# is no longer a proxy for audio-advertised.
UAC2_CARD_PATH = "/proc/asound/UAC2Gadget"
BLUETOOTH_RUNTIME_UNITS = local_source_lifecycle(Source.BLUETOOTH).runtime_units
_BLUETOOTH_LIFECYCLE = local_source_lifecycle(Source.BLUETOOTH)
_STATE_UNITS = tuple(dict.fromkeys((
    *local_source_lifecycle(Source.AIRPLAY).health_units,
    *local_source_lifecycle(Source.SPOTIFY).health_units,
    USBSINK_UNIT,
    USBSINK_GADGET_UNIT,
    BLUETOOTH_CONTROL_PLANE_UNIT,
    *_BLUETOOTH_LIFECYCLE.runtime_units,
)))


def _uac2_card_present() -> bool:
    """True iff the composite gadget's uac2 (audio) function is composed —
    the host currently sees JTS as a USB audio device. Fail-soft to False."""
    try:
        return os.path.isdir(UAC2_CARD_PATH)
    except OSError:
        return False


def _usbsink_available() -> bool:
    """True iff the dtoverlay that puts the USB-C port in peripheral
    mode is present in /boot/firmware/config.txt. Fail-soft on read
    errors (treat as unavailable so the toggle is disabled) — the
    operator can re-run install.sh to recover."""
    try:
        with open(BOOT_CONFIG_PATH) as f:
            content = f.read()
    except OSError as e:
        logger.debug("usbsink dtoverlay probe failed: %s", e)
        return False
    # Tolerate leading whitespace and trailing comments.
    for line in content.splitlines():
        if line.strip().startswith(USBSINK_DTOVERLAY_LINE):
            return True
    return False


def _systemctl(*args: str, timeout: int = 10) -> tuple[int, str]:
    """Run `systemctl <args>` and return (rc, stripped-stdout). Errors
    are logged but not raised; the caller decides how to surface them."""
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            check=False, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("systemctl %s failed: %s", " ".join(args), e)
        return 1, ""


def _unit_available(unit: str) -> bool:
    """True iff systemd knows about this unit file.

    Endpoint installs deliberately omit full-speaker renderer units. The
    sources page still exists there, but rows for absent units must be
    disabled and explicit rather than relying on a failing `systemctl start`.
    """
    rc, out = _systemctl("list-unit-files", unit, "--no-legend", timeout=5)
    if rc != 0:
        return False
    for line in out.splitlines():
        fields = line.split()
        if fields and fields[0] == unit:
            return True
    return False


def _unit_active(unit: str) -> bool:
    rc, out = _systemctl("is-active", unit, timeout=5)
    return rc == 0 and out == "active"


def _local_sources_allowed() -> bool:
    """True when this install role may run local source resource groups."""
    try:
        return install_profile_allows_local_sources(read_install_profile())
    except ValueError as e:
        logger.warning("invalid install profile while rendering /sources: %s", e)
        return False


def _source_state(
    *,
    desired: bool,
    observed: bool,
    available: bool,
    parked: bool = False,
    unavailable_reason: str = "",
    degraded_reason: str = "",
) -> dict[str, bool | str]:
    if parked:
        effective = "parked"
    elif not available:
        effective = "unavailable"
    elif degraded_reason or observed != desired:
        effective = "degraded"
    else:
        effective = "on" if observed else "off"
    state: dict[str, bool | str] = {
        # `enabled` is retained as the compatibility name consumed by the
        # existing UI. Both fields are the persisted desired state, never an
        # inference from a process that may have crashed.
        "enabled": bool(desired),
        "desired": bool(desired),
        "effective": effective,
        "available": bool(available),
    }
    if not available and unavailable_reason:
        state["unavailableReason"] = unavailable_reason
    if available and effective == "degraded":
        state["degradedReason"] = degraded_reason or (
            "This source is set to "
            f"{'on' if desired else 'off'}, but its current runtime state "
            "does not match. Check jasper-doctor or try the toggle again."
        )
    return state


def _systemd_source_state(
    source: Source,
    wizard_key: str,
    *,
    desired: bool,
    parked: bool,
    unit_snapshot: UnitSnapshot | None = None,
    local_sources_allowed: bool | None = None,
) -> dict[str, bool | str]:
    lifecycle = local_source_lifecycle(source)
    available_probe = unit_snapshot.available if unit_snapshot else _unit_available
    active_probe = unit_snapshot.active if unit_snapshot else _unit_active
    role_allows_sources = (
        _local_sources_allowed()
        if local_sources_allowed is None
        else local_sources_allowed
    )
    available = role_allows_sources and all(
        available_probe(unit) for unit in lifecycle.health_units
    )
    active = {
        unit: active_probe(unit) for unit in lifecycle.health_units
    }
    observed = all(active.values()) if desired else any(active.values())
    inactive = [unit for unit, running in active.items() if not running]
    degraded_reason = ""
    if desired and inactive:
        degraded_reason = f"required services are inactive: {', '.join(inactive)}"
    elif not desired:
        unexpected = [unit for unit, running in active.items() if running]
        if unexpected:
            degraded_reason = f"services are still active: {', '.join(unexpected)}"
    return _source_state(
        desired=desired,
        observed=observed,
        available=available,
        parked=parked,
        unavailable_reason=SOURCE_UNAVAILABLE[wizard_key],
        degraded_reason=degraded_reason,
    )


async def _bt_state() -> tuple[bool, bool]:
    """Return (powered, has_paired_hid) from the BlueZ control plane.

    Hardware availability is probed independently so an intentionally RF-killed
    adapter remains available to turn back on. ``has_paired_hid`` is true when
    a wireless remote
    (volume knob etc.) is paired — the wizard surfaces this as a
    confirm-before-off prompt so toggling BT doesn't silently kill
    the remote."""
    try:
        from ..bluetooth.adapter import has_paired_hid, state
        s = await state()
        powered = bool(s.get("powered", False))
        hid = False
        try:
            hid = await has_paired_hid()
        except Exception as e:  # noqa: BLE001
            # Non-fatal: the powered toggle still works, we just lose
            # the warning. Logged in case the helper itself breaks.
            logger.debug("has_paired_hid probe failed: %s", e)
        return powered, hid
    except Exception as e:  # noqa: BLE001
        # A stopped/wedged control plane is effective-off, not proof that the
        # hardware is absent. The independent sysfs/unit probe owns available.
        logger.debug("bluetooth state probe failed: %s", e)
        return False, False


def _bluetooth_availability(
    unit_snapshot: UnitSnapshot | None = None,
) -> BluetoothAvailability:
    """Shared adapter + complete activation-unit availability snapshot."""

    unit_available = unit_snapshot.available if unit_snapshot else _unit_available
    return probe_bluetooth_availability(unit_available)


def _gather_state() -> dict[str, dict[str, bool | str]]:
    """One-shot snapshot of all four sources. The BT branch runs an
    asyncio task because dbus-next is async-only; the rest are sync
    systemctl probes."""
    intents = read_source_intents()
    unit_snapshot = probe_unit_snapshot(_STATE_UNITS)
    try:
        bt_powered, bt_has_hid = asyncio.run(asyncio.wait_for(
            _bt_state(),
            timeout=_BLUETOOTH_STATE_TIMEOUT_SEC,
        ))
    except asyncio.TimeoutError:
        logger.warning(
            "Bluetooth state probe exceeded %.1fs",
            _BLUETOOTH_STATE_TIMEOUT_SEC,
        )
        bt_powered, bt_has_hid = False, False
    local_sources_allowed = _local_sources_allowed()
    parked = bonded_follower_active()
    usbsink_main_unit_available = (
        local_sources_allowed and unit_snapshot.available(USBSINK_UNIT)
    )
    usbsink_gadget_unit_available = (
        local_sources_allowed and unit_snapshot.available(USBSINK_GADGET_UNIT)
    )
    usbsink_units_available = (
        usbsink_main_unit_available and usbsink_gadget_unit_available
    )
    usbsink_dtoverlay_available = (
        _usbsink_available() if usbsink_units_available else False
    )
    usbsink_available = usbsink_units_available and usbsink_dtoverlay_available
    if not usbsink_main_unit_available:
        usbsink_reason = SOURCE_UNAVAILABLE["usbsink"]
    elif not usbsink_gadget_unit_available:
        usbsink_reason = (
            "USB Audio Input is missing its composite gadget unit. Re-run "
            "install.sh to repair the local renderer stack."
        )
    elif not usbsink_dtoverlay_available:
        usbsink_reason = (
            "USB gadget mode is not enabled in /boot/firmware/config.txt. "
            "Re-run install.sh and reboot before enabling USB Audio Input."
        )
    else:
        usbsink_reason = ""
    usbsink_main_active = unit_snapshot.active(USBSINK_UNIT)
    # Host-visible audio device presence is the uac2 ALSA card, NOT gadget-unit
    # activity: the composite gadget is always-on (it also carries the USB
    # management network), so its being active no longer implies audio is
    # advertised. The card exists iff the uac2 function is composed.
    usbsink_card_present = _uac2_card_present()
    usbsink_starting = unit_snapshot.activating(USBSINK_UNIT)
    fanin_status = read_fanin_status()
    usbsink_direct_sample = extract_direct_sample(fanin_status)
    usbsink_direct_present = usbsink_direct_sample is not None
    usbsink_direct_healthy = (
        usbsink_direct_sample is not None
        and usbsink_direct_sample.present
        and usbsink_direct_sample.health
        in {DIRECT_HEALTH_IDLE, DIRECT_HEALTH_CAPTURING}
    )
    usbsink_desired = intents[Source.USBSINK]
    # jasper-usbsink is the lifecycle/standby owner in combo mode; fan-in is the
    # real PCM consumer. Desired-On therefore requires all three boundaries:
    # standby owner, advertised UAC2 card, and a healthy direct fan-in lane.
    # Desired-Off treats any surviving boundary as drift.
    usbsink_effectively_on = (
        (usbsink_main_active or usbsink_starting)
        and usbsink_card_present
        and usbsink_direct_healthy
        if usbsink_desired
        else (
            usbsink_main_active
            or usbsink_starting
            or usbsink_card_present
            or usbsink_direct_present
        )
    )
    usbsink_degraded_reason = ""
    if usbsink_available and usbsink_desired:
        if usbsink_card_present and not (
            usbsink_main_active or usbsink_starting
        ):
            usbsink_degraded_reason = (
                "USB Audio Input is advertised to hosts, but its lifecycle "
                "service is not active yet. Toggle it off to hide the USB "
                "device, or "
                "check jasper-doctor if it stays here."
            )
        elif (usbsink_main_active or usbsink_starting) and not usbsink_card_present:
            usbsink_degraded_reason = (
                "USB Audio Input's lifecycle service is running, but the "
                "host-visible audio device is not advertised. Check "
                "jasper-doctor or turn "
                "the source off until gadget mode is repaired."
            )
        elif not usbsink_direct_present:
            usbsink_degraded_reason = (
                "USB Audio Input is advertised, but fan-in has no direct USB "
                "capture lane. Check jasper-doctor or toggle the source again."
            )
        elif not usbsink_direct_healthy:
            usbsink_degraded_reason = (
                "USB Audio Input's direct fan-in capture lane is not healthy "
                f"({usbsink_direct_sample.health or 'unknown'})."
            )
    bt_availability = _bluetooth_availability(unit_snapshot)
    bt_any_soft_blocked = bt_availability.any_soft_blocked
    bt_all_soft_blocked = bt_availability.all_soft_blocked
    bt_rfkill_error = bt_availability.error
    bt_unit_active = {
        unit: unit_snapshot.active(unit) for unit in BLUETOOTH_RUNTIME_UNITS
    }
    bt_runtime_active = all(bt_unit_active.values())
    bt_hardware_available = bt_availability.available
    bt_available_for_role = local_sources_allowed and bt_hardware_available
    if not local_sources_allowed:
        bt_unavailable_reason = SOURCE_UNAVAILABLE["bluetooth"]
    elif not bt_hardware_available:
        bt_unavailable_reason = bluetooth_unavailable_reason(bt_availability)
    else:
        bt_unavailable_reason = ""
    bt_desired = intents[Source.BLUETOOTH]
    bt_observed_on = (
        bt_powered and bt_runtime_active and bt_any_soft_blocked is not True
    )
    bt_degraded: list[str] = []
    if bt_rfkill_error:
        bt_degraded.append(f"RF-kill state is unreadable: {bt_rfkill_error}")
    if bt_desired:
        if bt_any_soft_blocked is True:
            bt_degraded.append("the Bluetooth radio is RF-killed")
        if not bt_powered:
            bt_degraded.append("BlueZ reports the adapter powered off")
        inactive = [unit for unit in BLUETOOTH_RUNTIME_UNITS if not bt_unit_active[unit]]
        if inactive:
            bt_degraded.append(f"required services are inactive: {', '.join(inactive)}")
    else:
        active = [unit for unit, is_active in bt_unit_active.items() if is_active]
        if active:
            bt_degraded.append(f"services are still active: {', '.join(active)}")
        if bt_powered:
            bt_degraded.append("BlueZ still reports the adapter powered on")
        if bt_all_soft_blocked is False:
            bt_degraded.append("the Bluetooth radio is not RF-killed")
    return {
        # Sibling key, not a source: the JS iterates a fixed SOURCES list,
        # so this rides alongside safely. Satellite-only installs park every
        # local source; bonded followers on full/streambox installs are parked
        # by the grouping reconciler. The page disables toggles and explains;
        # POST /set 409s.
        "pair": {"parked": parked},
        "airplay": _systemd_source_state(
            Source.AIRPLAY,
            "airplay",
            desired=intents[Source.AIRPLAY], parked=parked,
            unit_snapshot=unit_snapshot,
            local_sources_allowed=local_sources_allowed,
        ),
        "bluetooth": {
            **_source_state(
                desired=bt_desired,
                observed=bt_observed_on,
                available=bt_available_for_role,
                parked=parked,
                unavailable_reason=bt_unavailable_reason,
                degraded_reason="; ".join(bt_degraded),
            ),
            "hasPairedHid": bt_has_hid,
        },
        "spotify_connect": _systemd_source_state(
            Source.SPOTIFY,
            "spotify_connect",
            desired=intents[Source.SPOTIFY], parked=parked,
            unit_snapshot=unit_snapshot,
            local_sources_allowed=local_sources_allowed,
        ),
        "usbsink": _source_state(
            desired=usbsink_desired,
            observed=usbsink_effectively_on,
            available=usbsink_available,
            parked=parked,
            unavailable_reason=usbsink_reason,
            degraded_reason=usbsink_degraded_reason,
        ),
    }


def _apply(source: str, enabled: bool) -> None:
    """Record desired state and ask the one root coordinator to converge it."""
    if source == "airplay":
        if enabled and not _local_sources_allowed():
            raise RuntimeError(SOURCE_UNAVAILABLE["airplay"])
        if enabled and not all(
            _unit_available(unit)
            for unit in local_source_lifecycle(Source.AIRPLAY).health_units
        ):
            raise RuntimeError(SOURCE_UNAVAILABLE["airplay"])
        target = Source.AIRPLAY
    elif source == "spotify_connect":
        if enabled and not _local_sources_allowed():
            raise RuntimeError(SOURCE_UNAVAILABLE["spotify_connect"])
        if enabled and not all(
            _unit_available(unit)
            for unit in local_source_lifecycle(Source.SPOTIFY).health_units
        ):
            raise RuntimeError(SOURCE_UNAVAILABLE["spotify_connect"])
        target = Source.SPOTIFY
    elif source == "bluetooth":
        if enabled and not _local_sources_allowed():
            raise RuntimeError(SOURCE_UNAVAILABLE["bluetooth"])
        if enabled:
            availability = _bluetooth_availability()
            if not availability.available:
                raise RuntimeError(bluetooth_unavailable_reason(availability))
        target = Source.BLUETOOTH
    elif source == "usbsink":
        if enabled and not _local_sources_allowed():
            raise RuntimeError(SOURCE_UNAVAILABLE["usbsink"])
        if enabled and not _unit_available(USBSINK_UNIT):
            raise RuntimeError(SOURCE_UNAVAILABLE["usbsink"])
        if enabled and not _unit_available(USBSINK_GADGET_UNIT):
            raise RuntimeError(
                "USB Audio Input is missing its composite gadget unit. Re-run "
                "install.sh to repair the local renderer stack."
            )
        if enabled and not _usbsink_available():
            raise RuntimeError(
                "USB gadget mode is not enabled in /boot/firmware/config.txt. "
                "Re-run install.sh and reboot before enabling USB Audio Input."
            )
        target = Source.USBSINK
    else:  # guarded by VALID_SOURCES at the route boundary
        target = SOURCE_BY_WIZARD_KEY[source]
    request_source_intent(target, enabled)


# Per-page CSS layered on app.css. Just the source-row layout + notes; the
# toggle, card, header, and banner are shared primitives in app.css. Status
# colour is the one knob: the unavailable note reuses --status-warn.
_PAGE_CSS = """
.sources { display: flex; flex-direction: column; }
.source-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; padding: 0.9rem 0;
  border-bottom: 1px solid var(--border);
}
.source-row:last-child { border-bottom: none; }
.source-text { min-width: 0; }
.source-name { font-weight: 600; color: var(--text); }
.source-note { color: var(--muted); font-size: 0.9rem; margin-top: 0.2rem; }
.source-note.warn { color: var(--status-warn); }
.source-note code {
  font-size: 0.95em; padding: 1px 5px;
  border-radius: var(--radius-sm); background: var(--foreground-005);
}
"""


def _source_row(
    *, name: str, input_id: str, note_html: str = "", unavailable_html: str = "",
) -> str:
    """One source row: name + optional notes on the left, toggle on the
    right. The toggle is disabled at first paint; the ES module's /state
    poll hydrates checked/disabled within a poll cycle (mirrors the
    legacy behaviour)."""
    notes = ""
    if note_html:
        notes += note_html
    if unavailable_html:
        notes += unavailable_html
    return f"""
    <div class="source-row">
      <div class="source-text">
        <div class="source-name">{name}</div>
        {notes}
      </div>
      {toggle_html(input_id, disabled=True)}
    </div>
    """


def _index_html(csrf_token: str = "", *, status_msg: str = "") -> bytes:
    """Render the sources page. Initial toggle state is loaded from the
    server on the first /state poll (one extra round trip on page load —
    keeps the HTML static and cache-friendly)."""
    pair_note = (
        '<div class="info-card info-card--accent" id="pair-note" '
        'style="display:none" role="note">This speaker is part of a '
        "stereo pair — music plays through the pair leader, so local "
        "sources are parked. Unpair on "
        '<a href="/rooms/">the Speakers page</a> to use them again.'
        "</div>"
    )
    state_error = (
        '<div class="info-card info-card--danger" id="sources-state-error" '
        'style="display:none" role="alert">Source settings could not be read. '
        "Controls are paused to avoid showing a false state. Run jasper-doctor "
        "or re-run install.sh, then retry.</div>"
    )
    rows = "".join([
        _source_row(
            name="AirPlay", input_id="t-airplay",
            unavailable_html=(
                '<div class="source-note warn" id="airplay-unavailable-note" '
                'style="display:none">AirPlay is not installed on this speaker. '
                "Re-run install.sh to set up the local renderer stack.</div>"
            ),
        ),
        _source_row(
            name="Bluetooth", input_id="t-bluetooth",
            note_html=(
                '<div class="source-note warn" id="bt-note" style="display:none">'
                "Bluetooth adapter not available on this device.</div>"
            ),
        ),
        _source_row(
            name="Spotify Connect", input_id="t-spotify_connect",
            unavailable_html=(
                '<div class="source-note warn" '
                'id="spotify_connect-unavailable-note" style="display:none">'
                "Spotify Connect is not installed on this speaker. Re-run "
                "install.sh to set up the local renderer stack.</div>"
            ),
        ),
        _source_row(
            name="USB Audio Input", input_id="t-usbsink",
            note_html=(
                '<div class="source-note" id="usbsink-note">'
                "Plug a computer into the Pi's USB data/OTG port through a "
                "compatible power/data splitter or hub. Your computer sees "
                "the speaker as a USB audio output device. (The USB link also "
                "provides a management-network path to this speaker's web UI "
                "even with Wi-Fi off — that stays on regardless of this "
                "toggle.)</div>"
            ),
            unavailable_html=(
                '<div class="source-note warn" id="usbsink-unavailable-note" '
                'style="display:none">USB gadget mode not enabled in '
                "<code>/boot/firmware/config.txt</code> — re-run install.sh "
                "and reboot.</div>"
            ),
        ),
    ])
    body = f"""
{canonical_header("Music sources")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Turn each playback source on or off. Every choice
  persists across reboots, including Bluetooth. USB Audio Input is off by
  default — flip it on to use JTS as a USB audio output
  for a computer plugged into the Pi's USB data/OTG port through a
  compatible power/data splitter or hub.</p>

  <section class="info-card">
    <h2 class="section__title">Sources</h2>
    <div class="sources" id="sources">
      {state_error}{pair_note}{rows}
    </div>
  </section>
</main>
<script type="module" src="/assets/sources/js/main.js"></script>
"""
    return canonical_page(
        "Music sources", body, csrf_token=csrf_token, page_css=_PAGE_CSS,
    )


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            send_json_response(self, payload, status=status)

        def _read_json(self) -> dict[str, Any]:
            try:
                return read_json_object(self, max_bytes=_JSON_BODY_LIMIT)
            except JsonBodyError:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(
                    self,
                    _index_html(ctx["csrf_token"], status_msg=ctx["flash"]),
                )
                return
            if path == "/state":
                if not guard_read_request(self):
                    return
                try:
                    self._send_json(_gather_state())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/state failed")
                    self._send_json({"error": str(e)}, status=502)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/set":
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
                body = self._read_json()
                source = str(body.get("source") or "")
                if source not in VALID_SOURCES:
                    self._send_json(
                        {"error": f"unknown source {source!r}"}, status=400,
                    )
                    return
                enabled_value = body.get("enabled")
                if not isinstance(enabled_value, bool):
                    self._send_json(
                        {"error": "enabled must be true or false"}, status=400,
                    )
                    return
                enabled = enabled_value
                if bonded_follower_active():
                    # The pair owns its input surface while bonded. Keep a
                    # follower from accumulating hidden member-local desired
                    # changes that would surprise the household on unpair.
                    self._send_json(
                        {"error": "sources are managed by the stereo "
                                  "pair while this speaker is a "
                                  "follower — unpair on /rooms/ to "
                                  "change local sources"},
                        status=409,
                    )
                    return
                try:
                    _apply(source, enabled)
                except Exception as e:  # noqa: BLE001
                    logger.exception("toggle %s -> %s failed", source, enabled)
                    # The intent write happens before reconciliation. If apply
                    # fails, read it back so the client keeps the user's durable
                    # choice checked and shows runtime degradation instead of
                    # falsely rolling intent back to the old observed state.
                    try:
                        state = _gather_state()
                    except (OSError, RuntimeError, ValueError):
                        logger.exception("failed toggle state readback")
                        payload: dict[str, Any] = {"error": str(e)}
                        try:
                            durable_desired = source_intent_enabled(
                                SOURCE_BY_WIZARD_KEY[source],
                            )
                        except (OSError, RuntimeError, ValueError):
                            logger.exception("failed isolated intent readback")
                        else:
                            payload["desired"] = durable_desired
                            payload["intentRecorded"] = (
                                durable_desired is enabled
                            )
                        self._send_json(payload, status=502)
                    else:
                        self._send_json(
                            {"error": str(e), "state": state}, status=502,
                        )
                    return
                log_event(
                    logger,
                    "sources.set",
                    source=source,
                    enabled=enabled,
                    client=self.address_string(),
                )
                # Read-back the state we just applied so the client UI
                # reconciles against truth (in case systemctl no-op'd
                # or DBus rejected the property write).
                try:
                    state = _gather_state()
                except Exception as e:  # noqa: BLE001
                    logger.exception("/set readback failed")
                    self._send_json(
                        {
                            "error": str(e),
                            "desired": enabled,
                            "intentRecorded": True,
                        },
                        status=502,
                    )
                    return
                self._send_json(state)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(target) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per _systemd.make_http_server's contract."""
    from . import _systemd
    return _systemd.make_http_server(target, _make_handler())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-sources-web",
        description="Audio source on/off toggles for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_SOURCES_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_SOURCES_WEB_PORT", "8773")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target)

    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )
    _systemd.install_request_idle_bump(server.RequestHandlerClass, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-sources-web adopting systemd fd (idle=%ds)",
            int(IDLE_SHUTDOWN_SEC),
        )
    else:
        logger.info(
            "jasper-sources-web listening on http://%s:%d",
            args.host, args.port,
        )
    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
