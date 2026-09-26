# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Policy and status for exporting a selected JTS mic source over USB.

The first shipped slice deliberately reuses the existing UAC2 function: USB
Audio Input must already be enabled, then this feature adds the reverse
(Pi-to-host) mono direction. ``jasper-usbgadget`` owns descriptor composition;
``jasper-usbmic`` owns the relay; this module owns durable intent and its
backend-facing state. Source selection stays downstream of JTS voice/wake routing.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct
import time
from typing import Any, Callable, Mapping

from .atomic_io import (
    locked_update_env_file,
    read_json_mapping,
    read_regular_bytes_nofollow,
)
from .json_fields import as_float, as_mapping as _mapping
from .env_file import read_value
from .env_load import SOURCE_INTENT_ENV, USB_MIC_ENV_FILE as INTENT_PATH
from .music_sources import Source
from .identity.speaker_name import DEFAULT_SPEAKER_NAME, runtime_name
from .source_intent import source_intent_enabled
from .service_units import USBGADGET_SERVICE
from .systemd_probe import unit_active
from .usbgadget import GADGET_CONFIGFS_PATH

INTENT_ENV_OWNER = "JTS /aec USB mic control"
INTENT_KEY = "JASPER_USB_MIC"
USB_MIC_LEG_KEY = "JASPER_USB_MIC_LEG"
USB_MIC_PRIMARY_LEG = "primary"
USB_MIC_RAW_XVF_LEG = "raw0"
GADGET_PATH = GADGET_CONFIGFS_PATH
RELAY_STATUS_PATH = "/run/jasper-usbmic/status.json"
USBGADGET_UNIT = USBGADGET_SERVICE
USBMIC_UNIT = "jasper-usbmic.service"
USB_HOST_MIC_UDP_PORT = 9894
# The dedicated USB-host mic leg carries bridge-emit timing metadata. This is
# intentionally not used by the wake/session legs, whose raw PCM wire contract
# is frozen. The timestamp is bridge emit time, not physical capture time.
USB_MIC_PACKET_MAGIC = b"JM"
USB_MIC_PACKET_VERSION = 2
USB_MIC_HEADER_STRUCT = "<2sBBIQ"
USB_MIC_HEADER_BYTES = struct.calcsize(USB_MIC_HEADER_STRUCT)
USB_MIC_RELAY_SCHEMA_VERSION = 4
USB_MIC_SOURCE_AGE_BASIS = "bridge_emit_monotonic_v2"
USB_MIC_SOURCE_AGE_SCOPE = "bridge_emit_to_alsa_write"
USB_MIC_LATENCY_WARN_MS = 120.0
USB_MIC_BCD_DEVICE = "0x0210"
USB_NO_MIC_BCD_DEVICE = "0x0200"
RELAY_STATUS_FRESH_SECONDS = 3.0
_MAX_ENV_BYTES = 4096
# Bound on the gadget/relay unit probe; /aec polls it.
_UNIT_PROBE_TIMEOUT_SEC = 2.0


@dataclass(frozen=True)
class IntentState:
    enabled: bool
    valid: bool
    detail: str = ""
    # True only when the intent file has never been written — the factory
    # default (disabled) rather than a corrupt/present-but-invalid file.
    absent: bool = False


def read_intent(path: str | os.PathLike[str] = INTENT_PATH) -> IntentState:
    """Read the wizard-owned intent file without treating corruption as On."""

    try:
        text = read_regular_bytes_nofollow(
            path,
            max_bytes=_MAX_ENV_BYTES,
        ).decode("utf-8")
    except FileNotFoundError:
        return IntentState(
            False, False, "USB microphone preference is missing.", absent=True,
        )
    except (OSError, UnicodeDecodeError) as exc:
        return IntentState(False, False, f"USB microphone preference is unreadable: {exc}")
    raw = read_value(text, INTENT_KEY)
    if raw == "enabled":
        return IntentState(True, True)
    if raw == "disabled":
        return IntentState(False, True)
    if raw is None:
        return IntentState(False, False, f"{INTENT_KEY} is missing.")
    return IntentState(False, False, f"Unrecognised {INTENT_KEY} value {raw!r}.")


def usb_mic_enabled(path: str | os.PathLike[str] = INTENT_PATH) -> bool:
    """Return true only for an explicit, valid enabled intent."""

    state = read_intent(path)
    return state.valid and state.enabled


def write_usb_mic_enabled(
    enabled: bool,
    path: str | os.PathLike[str] = INTENT_PATH,
) -> None:
    """Persist household intent atomically under the shared state lock."""

    locked_update_env_file(
        path,
        {INTENT_KEY: "enabled" if enabled else "disabled"},
        mode=0o644,
        max_bytes=_MAX_ENV_BYTES,
        lock_timeout_sec=2.0,
        owner=INTENT_ENV_OWNER,
    )


def read_usb_mic_leg(
    path: str | os.PathLike[str] = INTENT_PATH,
    *,
    default: str = USB_MIC_PRIMARY_LEG,
) -> str:
    """Read the selected USB-export leg, falling back safely to primary."""

    try:
        text = read_regular_bytes_nofollow(
            path,
            max_bytes=_MAX_ENV_BYTES,
        ).decode("utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return default
    return (read_value(text, USB_MIC_LEG_KEY) or "").strip() or default


def write_usb_mic_leg(
    value: str,
    path: str | os.PathLike[str] = INTENT_PATH,
) -> None:
    """Persist a USB-export leg while preserving the enable-intent sibling."""

    leg = value.strip()
    if not leg:
        raise ValueError("USB microphone leg must not be empty")
    locked_update_env_file(
        path,
        {USB_MIC_LEG_KEY: leg},
        mode=0o644,
        max_bytes=_MAX_ENV_BYTES,
        lock_timeout_sec=2.0,
        owner=INTENT_ENV_OWNER,
    )


def usb_mic_leg_choices(env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Return user-selectable export sources for the active chip-beam plan.

    ``primary`` is feature vocabulary: it follows the stream JTS itself uses
    for voice. The existing physical ``raw0`` capture is offered only when an
    XVF chip-beam plan proves that six-channel capture is active; it remains a
    USB-only comparison source and is never voice/wake fallback vocabulary.
    Concrete beam tokens come only from the active hardware plan, so a future
    geometry can add its own choices without changing this host.
    """

    choices: list[dict[str, Any]] = [{
        "value": USB_MIC_PRIMARY_LEG,
        "label": "Same as JTS voice",
        "description": "Follows the microphone stream JTS uses for voice.",
    }]
    from .mics import xvf3800  # lazy: import cost, the XVF profile stays out of jasper-usbmic

    plan = xvf3800.chip_beam_plan_from_env(env)
    if plan is None:
        return choices
    choices.append({
        "value": USB_MIC_RAW_XVF_LEG,
        "label": "Raw microphone (no echo cancellation)",
        "description": (
            "Comparison only — exports physical XVF microphone 0 without "
            "chip or software echo cancellation or JTS voice gain. JTS "
            "voice stays on its managed echo-cancelled source."
        ),
        "comparison_only": True,
    })
    for leg in plan.legs:
        choices.append({
            "value": leg.token,
            "label": leg.label,
            "description": f"Uses the fixed {leg.azimuth_deg:g}° hardware-AEC beam.",
            "azimuth_deg": leg.azimuth_deg,
        })
    return choices


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_relay_status(path: Path) -> dict[str, Any]:
    return read_json_mapping(path) or {}


def _systemd_active(unit: str) -> bool:
    return unit_active(
        unit, timeout=_UNIT_PROBE_TIMEOUT_SEC, activating_is_live=False,
    )


def _status_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _status_text(value: Any) -> str:
    return str(value or "")


# The relay's own status fields the switch payload carries through, in payload
# order, each read the way the relay writes it.
_RELAY_REPORT_FIELDS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("source_age_basis", _status_text),
    ("source_age_scope", _status_text),
    ("source_age_sample_count", _status_int),
    ("source_age_samples_appended", _status_int),
    ("source_age_window_generation", _status_int),
    ("source_age_window_started_epoch_sec", as_float),
    ("source_age_ms_p50", as_float),
    ("source_age_ms_p95", as_float),
    ("source_age_ms_p99", as_float),
    ("packets_lost", _status_int),
    ("sequence_resets", _status_int),
    ("sequence_reorders", _status_int),
    ("sequence_discontinuities", _status_int),
    ("periods_dropped_streaming", _status_int),
    ("periods_dropped_idle", _status_int),
    ("drop_regime_basis", _status_text),
    ("periods_dropped", _status_int),
    ("writer_fill_ms", as_float),
    ("writer_target_ms", as_float),
    ("writer_pcm_rate_hz", _status_int),
    ("writer_pcm_period_frames", _status_int),
    ("writer_pcm_buffer_frames", _status_int),
    ("writer_splices", _status_int),
    ("writer_xruns", _status_int),
    ("writer_resets", _status_int),
)


def relay_audio_issue(relay: Mapping[str, Any]) -> str:
    """Return one stable operator-facing reason for unhealthy relay audio."""

    if not bool(relay.get("audio_stalled")):
        return ""
    if bool(relay.get("source_stalled")):
        return "The selected microphone stream stopped before it reached USB."
    if bool(relay.get("sustained_drops")):
        return "The USB microphone cannot keep up and is dropping audio continuously."
    return "The USB microphone audio path is stalled."


def _speaker_name() -> str:
    try:
        return runtime_name()
    except (OSError, UnicodeError, ValueError):
        return DEFAULT_SPEAKER_NAME


def _source_intent(source_intent_path: str | os.PathLike[str]) -> tuple[bool, str]:
    """USB Audio Input's own intent, and why it cannot be read when it cannot."""
    try:
        return source_intent_enabled(
            Source.USBSINK,
            env_path=os.fspath(source_intent_path),
        ), ""
    except RuntimeError as exc:
        return False, f"USB Audio Input preference is invalid: {exc}"


def _blockers(
    aec_status: Mapping[str, Any],
    intent: IntentState,
    source_enabled: bool,
    source_detail: str,
    uac2_present: bool,
) -> list[str]:
    """Why the switch cannot turn on, first cause first; empty when it can."""
    microphone = _mapping(aec_status.get("microphone"))
    active_profile = str(_mapping(aec_status.get("audio_profile")).get("active") or "")
    blockers: list[str] = []
    if not intent.valid:
        blockers.append(intent.detail)
    if not source_enabled:
        blockers.append(source_detail or "Turn on USB Audio Input in Sources first.")
    if not microphone.get("detected"):
        blockers.append("Connect a supported microphone first.")
    if active_profile == "direct_mic":
        blockers.append("Choose an echo-cancelled microphone mode first.")
    elif not aec_status.get("bridge_active"):
        blockers.append("Waiting for the echo-cancellation microphone path.")
    if source_enabled and not uac2_present:
        blockers.append("Waiting for the USB Audio Input device to be composed.")
    return blockers


def _relay_is_fresh(relay: Mapping[str, Any], now: float) -> bool:
    updated = as_float(relay.get("updated_epoch_sec", 0))
    return bool(relay) and updated is not None and (
        max(0.0, now - updated) <= RELAY_STATUS_FRESH_SECONDS
    )


def _relay_report(relay: Mapping[str, Any], fresh: bool) -> dict[str, Any]:
    """The relay's status as the switch payload carries it: its zero values
    while the relay's status file is stale."""
    report = relay if fresh else {}
    return {
        "host_streaming": bool(report.get("host_streaming")),
        "relay_audio_healthy": fresh and bool(report.get("audio_healthy", True)),
        "relay_audio_issue": relay_audio_issue(report),
        "relay_schema_version": _status_int(report.get("schema_version")),
        **{key: read(report.get(key)) for key, read in _RELAY_REPORT_FIELDS},
        "drop_rate_periods_per_sec": float(
            report.get("drop_rate_periods_per_sec", 0.0) or 0.0
        ),
    }


def _switch_state(
    enabled: bool,
    blockers: list[str],
    *,
    advertised: bool,
    revision_ok: bool,
    relay_active: bool,
    relay_fresh: bool,
    report: Mapping[str, Any],
    microphone_name: str,
) -> tuple[str, str]:
    """The switch's ``(state, detail)``; the first rule that matches wins."""
    if not enabled:
        if advertised or not revision_ok:
            return "stopping", "Removing the computer microphone; USB is reconnecting."
        return "off", blockers[0] if blockers else "Computer microphone is off."
    if blockers:
        return "unavailable", blockers[0]
    if not advertised:
        return "starting", "Adding the computer microphone; USB is reconnecting."
    if not revision_ok:
        return "degraded", (
            "The microphone descriptor revision is stale; "
            "USB needs to reconnect again."
        )
    if relay_active and relay_fresh:
        if report["relay_audio_issue"]:
            return "degraded", report["relay_audio_issue"]
        if report["host_streaming"]:
            return "streaming", f"Your computer is currently using {microphone_name}."
        return "ready", f"{microphone_name} is available on the connected computer."
    if relay_active:
        return "starting", "The computer microphone relay is starting."
    return "degraded", "The microphone is advertised, but its audio relay is not running."


def build_usb_mic_status(
    aec_status: Mapping[str, Any],
    *,
    intent_path: str | os.PathLike[str] = INTENT_PATH,
    source_intent_path: str | os.PathLike[str] = SOURCE_INTENT_ENV,
    gadget_path: str | os.PathLike[str] = GADGET_PATH,
    relay_status_path: str | os.PathLike[str] = RELAY_STATUS_PATH,
    systemd_active: Callable[[str], bool] = _systemd_active,
    now: float | None = None,
) -> dict[str, Any]:
    """Project desired/advertised/relay truth for the wake-page switch."""

    intent = read_intent(intent_path)
    source_enabled, source_detail = _source_intent(source_intent_path)
    gadget = Path(gadget_path)
    function = gadget / "functions/uac2.usb0"
    uac2_present = function.is_dir()
    p_chmask = _read_text(function / "p_chmask")
    advertised = uac2_present and p_chmask == "1"
    bcd_device = _read_text(gadget / "bcdDevice")
    expected_bcd_device = (
        USB_MIC_BCD_DEVICE if intent.enabled else USB_NO_MIC_BCD_DEVICE
    )
    descriptor_revision_ok = (
        not uac2_present or bcd_device == expected_bcd_device
    )
    blockers = _blockers(
        aec_status, intent, source_enabled, source_detail, uac2_present,
    )
    relay = _read_relay_status(Path(relay_status_path))
    relay_fresh = _relay_is_fresh(relay, time.time() if now is None else now)
    relay_active = systemd_active(USBMIC_UNIT)
    microphone_name = f"{_speaker_name()} Mic"
    report = _relay_report(relay, relay_fresh)
    state, detail = _switch_state(
        intent.enabled,
        blockers,
        advertised=advertised,
        revision_ok=descriptor_revision_ok,
        relay_active=relay_active,
        relay_fresh=relay_fresh,
        report=report,
        microphone_name=microphone_name,
    )
    return {
        "schema_version": 1,
        "enabled": bool(intent.enabled),
        "intent_valid": bool(intent.valid),
        "available": not blockers,
        "toggle_enabled": bool(not blockers or intent.enabled),
        "state": state,
        "detail": detail,
        "advertised": advertised,
        "relay_active": relay_active,
        "relay_fresh": relay_fresh,
        **report,
        "source_enabled": source_enabled,
        "uac2_present": uac2_present,
        "p_chmask": p_chmask,
        "bcd_device": bcd_device,
        "descriptor_revision_ok": descriptor_revision_ok,
        "label": microphone_name,
        "notice": (
            "Changing the computer microphone on or off reconnects USB audio "
            "and the USB management link for a few seconds."
        ),
    }
