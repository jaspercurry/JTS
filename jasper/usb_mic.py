# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Policy and status for exporting the cleaned JTS mic over USB.

The first shipped slice deliberately reuses the existing UAC2 function: USB
Audio Input must already be enabled, then this feature adds the reverse
(Pi-to-host) mono direction.  ``jasper-usbgadget`` owns descriptor composition;
``jasper-usbmic`` owns the audio relay; this module owns only durable intent and
the backend-facing view of desired versus observed state.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import struct
import subprocess
import time
from typing import Any, Callable, Mapping

from .atomic_io import locked_update_env_file, read_regular_bytes_nofollow
from .env_file import read_value
from .music_sources import Source
from .speaker_name import DEFAULT_SPEAKER_NAME, runtime_name
from .source_intent import source_intent_enabled

INTENT_PATH = "/var/lib/jasper/usb_mic.env"
INTENT_KEY = "JASPER_USB_MIC"
SOURCE_INTENT_PATH = "/var/lib/jasper/source_intent.env"
GADGET_PATH = "/sys/kernel/config/usb_gadget/jts-usb-audio"
RELAY_STATUS_PATH = "/run/jasper-usbmic/status.json"
USBGADGET_UNIT = "jasper-usbgadget.service"
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


@dataclass(frozen=True)
class IntentState:
    enabled: bool
    valid: bool
    detail: str = ""


def read_intent(path: str | os.PathLike[str] = INTENT_PATH) -> IntentState:
    """Read the wizard-owned intent file without treating corruption as On."""

    try:
        text = read_regular_bytes_nofollow(
            path,
            max_bytes=_MAX_ENV_BYTES,
        ).decode("utf-8")
    except FileNotFoundError:
        return IntentState(False, False, "USB microphone preference is missing.")
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


def main(argv: list[str] | None = None) -> int:
    """Import-cheap helper used by the root gadget composition script."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-intent", action="store_true")
    parser.add_argument("--intent-path", default=INTENT_PATH)
    args = parser.parse_args(argv)
    if args.check_intent:
        return 0 if usb_mic_enabled(args.intent_path) else 1
    parser.error("one action is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


def write_usb_mic_enabled(
    enabled: bool,
    path: str | os.PathLike[str] = INTENT_PATH,
) -> None:
    """Persist household intent atomically under the shared state lock."""

    locked_update_env_file(
        path,
        {INTENT_KEY: "enabled" if enabled else "disabled"},
        mode=0o644,
        group_from_parent=True,
        lock_mode=0o660,
        max_bytes=_MAX_ENV_BYTES,
        lock_timeout_sec=2.0,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_relay_status(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _systemd_active(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _status_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def relay_audio_issue(relay: Mapping[str, Any]) -> str:
    """Return one stable operator-facing reason for unhealthy relay audio."""

    if not bool(relay.get("audio_stalled")):
        return ""
    if bool(relay.get("source_stalled")):
        return "The cleaned microphone stream stopped before it reached USB."
    if bool(relay.get("sustained_drops")):
        return "The USB microphone cannot keep up and is dropping audio continuously."
    return "The USB microphone audio path is stalled."


def _speaker_name() -> str:
    try:
        return runtime_name()
    except (OSError, UnicodeError, ValueError):
        return DEFAULT_SPEAKER_NAME


def build_usb_mic_status(
    aec_status: Mapping[str, Any],
    *,
    intent_path: str | os.PathLike[str] = INTENT_PATH,
    source_intent_path: str | os.PathLike[str] = SOURCE_INTENT_PATH,
    gadget_path: str | os.PathLike[str] = GADGET_PATH,
    relay_status_path: str | os.PathLike[str] = RELAY_STATUS_PATH,
    systemd_active: Callable[[str], bool] = _systemd_active,
    now: float | None = None,
) -> dict[str, Any]:
    """Project desired/advertised/relay truth for the wake-page switch."""

    intent = read_intent(intent_path)
    try:
        source_enabled = source_intent_enabled(
            Source.USBSINK,
            env_path=os.fspath(source_intent_path),
        )
        source_detail = ""
    except RuntimeError as exc:
        source_enabled = False
        source_detail = f"USB Audio Input preference is invalid: {exc}"

    microphone = _mapping(aec_status.get("microphone"))
    audio_profile = _mapping(aec_status.get("audio_profile"))
    mic_detected = bool(microphone.get("detected"))
    bridge_active = bool(aec_status.get("bridge_active"))
    active_profile = str(audio_profile.get("active") or "")

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
    relay = _read_relay_status(Path(relay_status_path))
    current_time = time.time() if now is None else now
    try:
        relay_age = max(0.0, current_time - float(relay.get("updated_epoch_sec", 0)))
    except (TypeError, ValueError):
        relay_age = float("inf")
    relay_fresh = bool(relay) and relay_age <= RELAY_STATUS_FRESH_SECONDS
    relay_active = systemd_active(USBMIC_UNIT)
    host_streaming = bool(relay.get("host_streaming")) if relay_fresh else False
    audio_issue = relay_audio_issue(relay) if relay_fresh else ""
    microphone_name = f"{_speaker_name()} Mic"

    blockers: list[str] = []
    if not intent.valid:
        blockers.append(intent.detail)
    if not source_enabled:
        blockers.append(source_detail or "Turn on USB Audio Input in Sources first.")
    if not mic_detected:
        blockers.append("Connect a supported microphone first.")
    if active_profile == "direct_mic":
        blockers.append("Choose an echo-cancelled microphone mode first.")
    elif not bridge_active:
        blockers.append("Waiting for the echo-cancellation microphone path.")
    if source_enabled and not uac2_present:
        blockers.append("Waiting for the USB Audio Input device to be composed.")

    can_enable = (
        intent.valid
        and source_enabled
        and mic_detected
        and bridge_active
        and active_profile != "direct_mic"
        and uac2_present
    )

    if not intent.enabled:
        if advertised or not descriptor_revision_ok:
            state = "stopping"
            detail = "Removing the computer microphone; USB is reconnecting."
        else:
            state = "off"
            detail = blockers[0] if blockers else "Computer microphone is off."
    elif blockers and not can_enable:
        state = "unavailable"
        detail = blockers[0]
    elif not advertised:
        state = "starting"
        detail = "Adding the computer microphone; USB is reconnecting."
    elif not descriptor_revision_ok:
        state = "degraded"
        detail = (
            "The microphone descriptor revision is stale; "
            "USB needs to reconnect again."
        )
    elif relay_active and relay_fresh and audio_issue:
        state = "degraded"
        detail = audio_issue
    elif relay_active and relay_fresh:
        state = "streaming" if host_streaming else "ready"
        detail = (
            f"Your computer is currently using {microphone_name}."
            if host_streaming
            else f"{microphone_name} is available on the connected computer."
        )
    elif relay_active:
        state = "starting"
        detail = "The computer microphone relay is starting."
    else:
        state = "degraded"
        detail = "The microphone is advertised, but its audio relay is not running."

    return {
        "schema_version": 1,
        "enabled": bool(intent.enabled),
        "intent_valid": bool(intent.valid),
        "available": bool(can_enable),
        "toggle_enabled": bool(can_enable or intent.enabled),
        "state": state,
        "detail": detail,
        "advertised": advertised,
        "relay_active": relay_active,
        "relay_fresh": relay_fresh,
        "host_streaming": host_streaming,
        "relay_audio_healthy": bool(relay.get("audio_healthy", True))
        if relay_fresh
        else False,
        "relay_audio_issue": audio_issue,
        "relay_schema_version": _status_int(relay.get("schema_version"))
        if relay_fresh
        else 0,
        "source_age_basis": str(relay.get("source_age_basis") or "")
        if relay_fresh
        else "",
        "source_age_scope": str(relay.get("source_age_scope") or "")
        if relay_fresh
        else "",
        "source_age_sample_count": _status_int(
            relay.get("source_age_sample_count")
        )
        if relay_fresh
        else 0,
        "source_age_samples_appended": _status_int(
            relay.get("source_age_samples_appended")
        )
        if relay_fresh
        else 0,
        "source_age_window_generation": _status_int(
            relay.get("source_age_window_generation")
        )
        if relay_fresh
        else 0,
        "source_age_window_started_epoch_sec": _status_optional_float(
            relay.get("source_age_window_started_epoch_sec")
        )
        if relay_fresh
        else None,
        "source_age_ms_p50": _status_optional_float(
            relay.get("source_age_ms_p50")
        )
        if relay_fresh
        else None,
        "source_age_ms_p95": _status_optional_float(
            relay.get("source_age_ms_p95")
        )
        if relay_fresh
        else None,
        "source_age_ms_p99": _status_optional_float(
            relay.get("source_age_ms_p99")
        )
        if relay_fresh
        else None,
        "packets_lost": _status_int(relay.get("packets_lost"))
        if relay_fresh
        else 0,
        "sequence_resets": _status_int(relay.get("sequence_resets"))
        if relay_fresh
        else 0,
        "sequence_reorders": _status_int(relay.get("sequence_reorders"))
        if relay_fresh
        else 0,
        "sequence_discontinuities": _status_int(
            relay.get("sequence_discontinuities")
        )
        if relay_fresh
        else 0,
        "periods_dropped_streaming": _status_int(
            relay.get("periods_dropped_streaming")
        )
        if relay_fresh
        else 0,
        "periods_dropped_idle": _status_int(
            relay.get("periods_dropped_idle")
        )
        if relay_fresh
        else 0,
        "drop_regime_basis": str(relay.get("drop_regime_basis") or "")
        if relay_fresh
        else "",
        "periods_dropped": _status_int(relay.get("periods_dropped"))
        if relay_fresh
        else 0,
        "writer_fill_ms": _status_optional_float(relay.get("writer_fill_ms"))
        if relay_fresh
        else None,
        "writer_target_ms": _status_optional_float(relay.get("writer_target_ms"))
        if relay_fresh
        else None,
        "writer_pcm_rate_hz": _status_int(relay.get("writer_pcm_rate_hz"))
        if relay_fresh
        else 0,
        "writer_pcm_period_frames": _status_int(
            relay.get("writer_pcm_period_frames")
        )
        if relay_fresh
        else 0,
        "writer_pcm_buffer_frames": _status_int(
            relay.get("writer_pcm_buffer_frames")
        )
        if relay_fresh
        else 0,
        "writer_splices": _status_int(relay.get("writer_splices"))
        if relay_fresh
        else 0,
        "writer_xruns": _status_int(relay.get("writer_xruns"))
        if relay_fresh
        else 0,
        "writer_resets": _status_int(relay.get("writer_resets"))
        if relay_fresh
        else 0,
        "drop_rate_periods_per_sec": float(
            relay.get("drop_rate_periods_per_sec", 0.0) or 0.0
        )
        if relay_fresh
        else 0.0,
        "source_enabled": source_enabled,
        "uac2_present": uac2_present,
        "p_chmask": p_chmask,
        "bcd_device": bcd_device,
        "descriptor_revision_ok": descriptor_revision_ok,
        "label": microphone_name,
        "notice": "Changing this reconnects USB audio and the USB management link for a few seconds.",
    }
