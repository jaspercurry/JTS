# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC and wake-threshold helpers behind jasper-control endpoints."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from ..audio_profile_state import (
    AecIntent,
    MicProbe,
    PROFILE_DIRECT_MIC,
    PROFILE_XVF_CHIP_AEC,
    PROFILE_XVF_CHIP_AEC_TESTING,
    PROFILE_XVF_SOFTWARE_AEC3,
    RuntimeAecEnv,
    build_audio_profile_status,
    infer_audio_input_profile,
    normalize_audio_input_profile,
    parse_env_bool as _parse_audio_profile_bool,
    profile_env_updates,
    resolve_audio_input_intent,
    runtime_env_from_mapping,
    validation_profile,
)
from ..audio_validation import (
    current_artifact_filter_kwargs as _audio_validation_filter_kwargs,
)
from ..audio_validation import latest_artifact_summary as _audio_validation_summary
from ..atomic_io import locked_update_env_file
from ..audio_input_view import build_microphone_settings_view
from ..usb_mic import (
    build_usb_mic_status,
    read_usb_mic_leg,
    usb_mic_leg_choices,
)
from ..chip_aec_policy import (
    combine_mic_availability,
    gate_from_runtime_env,
    resolve_chip_aec_dac_gate,
)
from ..wake_models import WAKE_MODEL_FILE

_AEC_MODE_FILE = "/var/lib/jasper/aec_mode.env"
_WAKE_MODEL_FILE = WAKE_MODEL_FILE
_JASPER_ENV_FILE = "/etc/jasper/jasper.env"
_XVF_FIRMWARE_UPDATE_STATE_FILE = "/var/lib/jasper/xvf-firmware-update.json"
_XVF_FIRMWARE_UPDATE_SERVICE = "jasper-xvf-firmware-update.service"
_AEC_BRIDGE_STATS_FILE = "/run/jasper/aec_bridge_stats.json"
_AEC_BRIDGE_STATS_FRESH_SECONDS = 3.0

# Default leg policy — must match deploy/install.sh's reconcile_aec_state
# and deploy/bin/jasper-aec-reconcile's ensure_mode_file. Raw is on for
# software-AEC defaults, DTLN is off, and chip-AEC's extra beam detectors
# are off. The chip-AEC profile itself may still be selected by `auto`;
# these defaults only decide whether voice opens extra detector instances
# beyond the primary/session leg.
_LEG_DEFAULT_RAW = True
_LEG_DEFAULT_DTLN = False
_LEG_DEFAULT_CHIP_AEC = False
_LEG_DEFAULT_CHIP_AEC_150 = False
_LEG_DEFAULT_CHIP_AEC_210 = False
_PROFILE_DEFAULT = "custom"

# Operator-facing wake-leg toggle name -> jasper.wake_legs token(s). The
# chip-direct / AEC-OFF leg is exposed as "raw", but its frozen wire token is
# "off". Do NOT confuse "raw" with the "raw0" corpus-only leg. Chip-AEC
# production mode is selected by the profile (`JASPER_WAKE_LEG_CHIP_AEC`);
# the two per-beam toggles below only add extra wake detectors.
_TOGGLE_TO_TOKEN = {
    "raw": ("off",),
    "dtln": ("dtln",),
    "chip_aec_150": ("chip_aec_150",),
    "chip_aec_210": ("chip_aec_210",),
}
_TOGGLE_TO_ENV_KEY = {
    "raw": "JASPER_WAKE_LEG_RAW",
    "dtln": "JASPER_WAKE_LEG_DTLN",
    "chip_aec_150": "JASPER_WAKE_LEG_CHIP_AEC_150",
    "chip_aec_210": "JASPER_WAKE_LEG_CHIP_AEC_210",
}


def _parse_env_bool(raw: str, default: bool) -> bool:
    """Same normalization the bash reconciler does — accept yes/no/etc."""
    return _parse_audio_profile_bool(raw, default)


def _read_aec_state() -> dict:
    """Full /var/lib/jasper/aec_mode.env state — mode + both leg
    booleans. Missing keys fall back to the documented defaults so a
    partial file from a pre-leg-toggle deploy still parses sanely.

    The reconciler's ensure_mode_file appends any missing keys on its
    next run, so this fallback is a one-pass deal — but it must be
    correct for the GET that races that first reconcile."""
    state = {
        "mode": "auto",
        "leg_raw": _LEG_DEFAULT_RAW,
        "leg_dtln": _LEG_DEFAULT_DTLN,
        "leg_chip_aec": _LEG_DEFAULT_CHIP_AEC,
        "leg_chip_aec_150": _LEG_DEFAULT_CHIP_AEC_150,
        "leg_chip_aec_210": _LEG_DEFAULT_CHIP_AEC_210,
        "profile": "",
    }
    file_found = False
    try:
        with open(_AEC_MODE_FILE) as f:
            file_found = True
            for line in f:
                line = line.strip()
                if line.startswith("JASPER_AEC_MODE="):
                    val = line.split("=", 1)[1].strip().strip("'\"") or "auto"
                    state["mode"] = val
                elif line.startswith("JASPER_WAKE_LEG_RAW="):
                    state["leg_raw"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_RAW,
                    )
                elif line.startswith("JASPER_WAKE_LEG_DTLN="):
                    state["leg_dtln"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_DTLN,
                    )
                elif line.startswith("JASPER_WAKE_LEG_CHIP_AEC="):
                    state["leg_chip_aec"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_CHIP_AEC,
                    )
                elif line.startswith("JASPER_WAKE_LEG_CHIP_AEC_150="):
                    state["leg_chip_aec_150"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_CHIP_AEC_150,
                    )
                elif line.startswith("JASPER_WAKE_LEG_CHIP_AEC_210="):
                    state["leg_chip_aec_210"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_CHIP_AEC_210,
                    )
                elif line.startswith("JASPER_AUDIO_INPUT_PROFILE="):
                    state["profile"] = normalize_audio_input_profile(
                        line.split("=", 1)[1],
                        default=_PROFILE_DEFAULT,
                    )
    except OSError:
        pass
    if not state["profile"]:
        if file_found:
            state["profile"] = infer_audio_input_profile(
                AecIntent(
                    mode=state["mode"],
                    raw_enabled=bool(state["leg_raw"]),
                    dtln_enabled=bool(state["leg_dtln"]),
                    chip_aec_enabled=bool(state["leg_chip_aec"]),
                    chip_aec_150_enabled=bool(state["leg_chip_aec_150"]),
                    chip_aec_210_enabled=bool(state["leg_chip_aec_210"]),
                ),
            )
        else:
            state["profile"] = "auto"
    return state


def _read_aec_mode() -> str:
    """Compatibility shim — returns just the mode string."""
    return _read_aec_state()["mode"]


def _write_aec_mode(mode: str) -> None:
    """Atomic write of the AEC mode key, preserving leg keys."""
    if mode not in ("auto", "disabled"):
        raise ValueError(f"invalid mode: {mode!r}")
    _atomic_rewrite_env(
        _AEC_MODE_FILE,
        {
            "JASPER_AEC_MODE": mode,
            "JASPER_AUDIO_INPUT_PROFILE": "custom",
        },
    )


def _write_aec_leg(leg: str, enabled: bool) -> None:
    """Atomic write of one wake-leg boolean, preserving every other key
    in aec_mode.env (mode, the other leg).

    Caller is responsible for kicking the reconciler — this just
    persists the user's intent. Restart blast-radius lives in the
    reconciler since it has the actual mode + presence context."""
    if leg not in _TOGGLE_TO_TOKEN:
        raise ValueError(f"invalid leg: {leg!r}")
    _atomic_rewrite_env(
        _AEC_MODE_FILE,
        {
            _TOGGLE_TO_ENV_KEY[leg]: "1" if enabled else "0",
            "JASPER_AUDIO_INPUT_PROFILE": "custom",
        },
    )


def _leg_status(
    *,
    configured: bool,
    available: bool,
    active: bool,
    disabled_reason: str = "",
) -> dict[str, Any]:
    if not available:
        status = disabled_reason or "unavailable"
    elif active:
        status = "active"
    elif configured:
        status = "starting"
    else:
        status = "off"
    return {
        "configured": configured,
        "available": available,
        "active": active,
        "disabled_reason": disabled_reason if not available else "",
        "status": status,
    }


def _write_audio_input_profile(profile: str) -> None:
    """Write a canonical audio input profile plus rollback-safe leg keys."""

    normalized = normalize_audio_input_profile(profile, default="")
    if not normalized or normalized == "custom":
        raise ValueError(f"invalid profile: {profile!r}")
    _atomic_rewrite_env(_AEC_MODE_FILE, profile_env_updates(normalized))


def _atomic_rewrite_env(path: str, updates: dict) -> None:
    """Read-modify-write of a systemd env file. Updates the given keys,
    preserves all others. Cooperating writers are serialized with an
    advisory flock, and readers see atomic whole-file replacement."""
    locked_update_env_file(path, updates, mode=0o644)


def _read_wake_threshold() -> float:
    """Read JASPER_WAKE_THRESHOLD from /var/lib/jasper/wake_model.env
    (the /wake/ wizard's home) with the daemon's compiled-in default
    (0.3) as fallback. Same precedence the daemon uses on startup."""
    try:
        from ..web._common import read_env_file
        val = read_env_file(_WAKE_MODEL_FILE).get("JASPER_WAKE_THRESHOLD", "")
    except OSError:
        val = ""
    if not val:
        val = os.environ.get("JASPER_WAKE_THRESHOLD", "")
    try:
        # Mirror the daemon's compiled-in default (in jasper/config.py:
        # `wake_threshold=_env_float("JASPER_WAKE_THRESHOLD", 0.3)`, also
        # shipped in .env.example) so the slider + /state show what's
        # actually live. A higher fallback here would make a Save at the
        # displayed value silently raise the real threshold.
        return float(val) if val else 0.3
    except ValueError:
        return 0.3


def _write_wake_threshold(value: float) -> None:
    """Atomic write of JASPER_WAKE_THRESHOLD into wake_model.env,
    preserving JASPER_WAKE_MODEL. Both keys are wizard-managed by the
    /wake/ page (model picker writes JASPER_WAKE_MODEL via the form
    save; sensitivity slider posts to /wake/sensitivity which lands
    here)."""
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"threshold out of range: {value}")
    locked_update_env_file(
        _WAKE_MODEL_FILE,
        {"JASPER_WAKE_THRESHOLD": f"{value:.2f}"},
        mode=0o644,
    )


def _aec_bridge_active() -> bool:
    """True if jasper-aec-bridge.service is currently active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "jasper-aec-bridge.service"],
            capture_output=True, text=True, timeout=2.0,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _unit_active(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=2.0,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _read_xvf_firmware_update_state() -> dict[str, Any]:
    try:
        with open(_XVF_FIRMWARE_UPDATE_STATE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _xvf_firmware_update_status() -> dict[str, Any]:
    try:
        from ..mics import xvf3800
        profile = xvf3800.detect_runtime_profile()
        return xvf3800.firmware_update_status(
            profile,
            service_active=_unit_active(_XVF_FIRMWARE_UPDATE_SERVICE),
            last_update=_read_xvf_firmware_update_state(),
        )
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        return {
            "schema_version": 1,
            "state": "unknown",
            "required": False,
            "updating": False,
            "title": "Microphone firmware status unavailable",
            "detail": str(exc),
            "current": {},
            "target": None,
            "last_update": _read_xvf_firmware_update_state(),
            "action": {
                "enabled": False,
                "label": "Download and update firmware",
                "danger": True,
            },
        }


def _start_xvf_firmware_update() -> None:
    subprocess.run(
        ["systemctl", "start", "--no-block", _XVF_FIRMWARE_UPDATE_SERVICE],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )


def _kick_aec_reconciler() -> None:
    """Apply a persisted AEC-mode/leg change through the reconciler.

    Use `restart`, not `start`: the reconciler is a Type=oneshot unit.
    A rapid toggle can write new intent while the previous reconcile is
    still active; `systemctl start` would be a no-op in that state and
    leave runtime env one click behind the UI.
    """
    subprocess.Popen(
        ["systemctl", "restart", "--no-block",
         "jasper-aec-reconcile.service"],
    )


def _fresh_jasper_env() -> dict[str, str]:
    """Fresh view of /etc/jasper/jasper.env.

    jasper-control is long-lived while the AEC reconciler mutates this
    file when mic mode changes, so `os.environ` can be stale. Status
    surfaces should prefer the file and fall back to process env only for
    keys absent from the file.
    """
    from ..env_load import parse_env_file
    return parse_env_file(os.environ.get("JASPER_ENV_FILE", _JASPER_ENV_FILE))


def _read_wake_word_status() -> dict[str, Any]:
    """Wake model label for the /wake/ status card."""
    from .. import wake_models
    from ..web._common import read_env_file
    try:
        state = read_env_file(_WAKE_MODEL_FILE)
    except OSError:
        state = {}
    model = (state.get("JASPER_WAKE_MODEL") or "").strip()
    if not model:
        model = os.environ.get("JASPER_WAKE_MODEL", "").strip() or "hey_jarvis"
    entry = wake_models.by_model(model)
    return {
        "model": model,
        "label": entry.label if entry else model,
        "pronunciation": entry.pronunciation if entry else "",
        "custom": entry is None,
    }


def _audio_profile_status(
    state: dict[str, Any],
    *,
    bridge_active: bool,
    chip_available: bool,
    chip_gate: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    runtime: RuntimeAecEnv | None = None,
    mic_probe: MicProbe | None = None,
) -> dict[str, Any]:
    """Read-only mic/profile status for the /wake/ page.

    This is intentionally descriptive and side-effect-free: it reads the
    reconciler-owned env file plus the XVF profile's firmware helpers,
    then classifies intent vs observed runtime. It does not probe audio
    streams or open devices on the hot polling path.
    """
    if env is None:
        env = _fresh_jasper_env()
    if runtime is None:
        runtime = runtime_env_from_mapping(env, process_env=os.environ)
    if mic_probe is None:
        mic_probe = _xvf_mic_probe()

    return build_audio_profile_status(
        AecIntent(
            mode=state["mode"],
            raw_enabled=bool(state["leg_raw"]),
            dtln_enabled=bool(state["leg_dtln"]),
            chip_aec_enabled=bool(state["leg_chip_aec"]),
            chip_aec_150_enabled=bool(state["leg_chip_aec_150"]),
            chip_aec_210_enabled=bool(state["leg_chip_aec_210"]),
            profile_selection=str(state.get("profile") or ""),
        ),
        runtime,
        mic_probe,
        bridge_active=bridge_active,
        chip_available=chip_available,
        chip_gate=chip_gate,
    )


def _xvf_mic_probe() -> MicProbe:
    """Return one cheap XVF profile snapshot for a status request."""

    try:
        from ..mics import xvf3800
        runtime_profile = xvf3800.detect_runtime_profile()
        return MicProbe(
            xvf_present=runtime_profile.present,
            capture_channels=runtime_profile.capture_channels,
            recommended_channels=xvf3800.RECOMMENDED_CAPTURE_CHANNELS,
            display_name=runtime_profile.display_name,
            alsa_card_name=runtime_profile.alsa_card_name,
            variant_id=runtime_profile.variant_id,
            geometry=runtime_profile.geometry,
            chip_beam_plan=runtime_profile.chip_beam_plan_id,
            probe_error=None,
        )
    except Exception:  # noqa: BLE001
        return MicProbe(
            xvf_present=False,
            capture_channels=None,
            recommended_channels=6,
            display_name="Seeed ReSpeaker XVF3800 (USB UA)",
            alsa_card_name="",
            variant_id="",
            geometry="",
            chip_beam_plan="",
            probe_error="firmware probe failed",
        )


def _chip_aec_available(mic_probe: MicProbe) -> bool:
    """True when one mic snapshot names a production-validated beam plan."""
    if not mic_probe.xvf_present or not mic_probe.chip_beam_plan:
        return False
    try:
        from ..mics import xvf3800
        plan = xvf3800.chip_beam_plan(mic_probe.chip_beam_plan)
        return bool(plan and plan.production_validated)
    except Exception:  # noqa: BLE001
        return False


def _chip_aec_gate(
    env: dict[str, str],
    state: dict[str, Any],
    *,
    mic_available: bool,
) -> dict[str, Any]:
    """Resolve chip-AEC gate status for /aec without probing devices."""

    selection = normalize_audio_input_profile(
        str(state.get("profile") or ""),
        default=infer_audio_input_profile(
            AecIntent(
                mode=str(state.get("mode") or "auto"),
                raw_enabled=bool(state.get("leg_raw")),
                dtln_enabled=bool(state.get("leg_dtln")),
                chip_aec_enabled=bool(state.get("leg_chip_aec")),
                chip_aec_150_enabled=bool(state.get("leg_chip_aec_150")),
                chip_aec_210_enabled=bool(state.get("leg_chip_aec_210")),
            ),
        ),
    )
    testing_requested = selection == PROFILE_XVF_CHIP_AEC_TESTING
    runtime_gate = (
        gate_from_runtime_env(env)
        if env.get("JASPER_AEC_CHIP_AEC_DAC_STATUS")
        else None
    )
    if runtime_gate is not None and (
        not testing_requested or runtime_gate.arm_allowed
    ):
        dac_gate = runtime_gate
    else:
        dac_gate = resolve_chip_aec_dac_gate(
            env.get("JASPER_AUDIO_DAC_ID", "unknown"),
            testing_requested=testing_requested,
        )
    # Fold the input-mic fact into the DAC-only gate so blockers +
    # recommended_action use chip_aec_policy's single canonical vocabulary
    # (BLOCKER_MIC / BLOCKER_DAC). Do not re-add a parallel code scheme here.
    gate = combine_mic_availability(
        dac_gate,
        mic_available=mic_available,
        testing_requested=testing_requested,
    )
    payload = gate.to_dict()
    payload["mic_available"] = mic_available
    payload["production_available"] = bool(
        mic_available and gate.production_allowed
    )
    payload["testing_available"] = bool(mic_available and gate.testing_allowed)
    payload["available"] = bool(
        mic_available
        and (
            gate.production_allowed
            or (testing_requested and gate.testing_allowed)
        )
    )
    return payload


def _aec_full_status() -> dict:
    """JSON shape returned by GET /aec — the single source of truth
    for the /wake/ page's detection card. Includes both the configured
    state (from aec_mode.env) and the observed bridge service state.

    Per-leg observed state isn't returned separately today. A
    configured leg is implicitly "active" when (a) AEC mode is auto,
    (b) the bridge is active, and (c) the leg is configured on. DTLN
    load failures surface via jasper-doctor's check_aec_bridge_dtln_engine,
    which the /system Diagnostics disclosure runs on demand.

    The chip-AEC leg also carries an `available` flag: production chip
    beams require a detected XVF profile with a validated beam plan, so the
    /wake/ toggle stays disabled when the connected geometry has no plan."""
    state = _read_aec_state()
    bridge_active = _aec_bridge_active()
    env = _fresh_jasper_env()
    runtime = runtime_env_from_mapping(env, process_env=os.environ)
    # Wrap defensively so a profile probe failure can never 500 a status
    # GET the /wake/ page polls every 3 s.
    mic_probe = _xvf_mic_probe()
    chip_available = _chip_aec_available(mic_probe)
    chip_gate = _chip_aec_gate(env, state, mic_available=chip_available)
    requested_intent = AecIntent(
        mode=state["mode"],
        raw_enabled=bool(state["leg_raw"]),
        dtln_enabled=bool(state["leg_dtln"]),
        chip_aec_enabled=bool(state["leg_chip_aec"]),
        chip_aec_150_enabled=bool(state["leg_chip_aec_150"]),
        chip_aec_210_enabled=bool(state["leg_chip_aec_210"]),
        profile_selection=str(state.get("profile") or ""),
    )
    profile_status = _audio_profile_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
        chip_gate=chip_gate,
        env=env,
        runtime=runtime,
        mic_probe=mic_probe,
    )
    effective = _applied_aec_intent(
        requested_intent,
        runtime=runtime,
        profile_status=profile_status["audio_profile"],
    )
    configured = resolve_audio_input_intent(
        requested_intent,
        chip_available=bool(chip_gate["available"]),
    )
    software_aec3 = _software_aec3_status(
        effective,
        bridge_active=bridge_active,
        profile_status=profile_status["audio_profile"],
    )
    requested_profile = (
        profile_status["audio_profile"].get("validation_profile")
        or validation_profile(profile_status["audio_profile"].get("requested"))
    )
    validation_filters = _audio_validation_filter_kwargs(
        requested_profile=requested_profile,
        system_env=env,
    )
    payload = {
        "mode": effective.mode,
        "profile": state["profile"],
        "raw_intent": {
            "mode": state["mode"],
            "leg_raw": state["leg_raw"],
            "leg_dtln": state["leg_dtln"],
            "leg_chip_aec": state["leg_chip_aec"],
            "leg_chip_aec_150": state["leg_chip_aec_150"],
            "leg_chip_aec_210": state["leg_chip_aec_210"],
        },
        "bridge_active": bridge_active,
        "bridge_role": _bridge_role(
            effective,
            profile_status=profile_status["audio_profile"],
        ),
        "software_aec3": software_aec3,
        "legs": {
            "raw": _leg_status(
                configured=effective.raw_enabled,
                available=(
                    effective.mode == "auto"
                    and not effective.chip_aec_enabled
                ),
                active=bool(
                    effective.mode == "auto"
                    and not effective.chip_aec_enabled
                    and bridge_active
                    and runtime.raw_device
                ),
                disabled_reason=(
                    "Software streams are bypassed by hardware AEC."
                    if effective.mode == "auto" and effective.chip_aec_enabled
                    else "Advanced wake streams require the AEC bridge."
                ),
            ),
            "dtln": _leg_status(
                configured=effective.dtln_enabled,
                available=(
                    effective.mode == "auto"
                    and not effective.chip_aec_enabled
                ),
                active=bool(
                    effective.mode == "auto"
                    and not effective.chip_aec_enabled
                    and bridge_active
                    and runtime.dtln_device
                ),
                disabled_reason=(
                    "Software streams are bypassed by hardware AEC."
                    if effective.mode == "auto" and effective.chip_aec_enabled
                    else "Advanced wake streams require the AEC bridge."
                ),
            ),
            "chip_aec": {
                "configured": effective.chip_aec_enabled,
                "available": chip_gate["available"],
                "production_available": chip_gate["production_available"],
                "testing_available": chip_gate["testing_available"],
            },
            "chip_aec_150": _leg_status(
                configured=effective.chip_aec_150_enabled,
                available=bool(
                    configured.mode == "auto"
                    and configured.chip_aec_enabled
                    and chip_gate["available"]
                ),
                active=bool(
                    bridge_active
                    and effective.chip_aec_enabled
                    and runtime.chip_aec_150_device
                ),
                disabled_reason=(
                    "Use hardware echo cancellation first."
                    if not configured.chip_aec_enabled
                    else "Hardware AEC is unavailable for this mic/DAC path."
                ),
            ),
            "chip_aec_210": _leg_status(
                configured=effective.chip_aec_210_enabled,
                available=bool(
                    configured.mode == "auto"
                    and configured.chip_aec_enabled
                    and chip_gate["available"]
                ),
                active=bool(
                    bridge_active
                    and effective.chip_aec_enabled
                    and runtime.chip_aec_210_device
                ),
                disabled_reason=(
                    "Use hardware echo cancellation first."
                    if not configured.chip_aec_enabled
                    else "Hardware AEC is unavailable for this mic/DAC path."
                ),
            ),
        },
        "threshold": _read_wake_threshold(),
        "wake_word": _read_wake_word_status(),
        "chip_aec_gate": chip_gate,
        "audio_profile": profile_status["audio_profile"],
        "microphone": profile_status["microphone"],
        "validation": _audio_validation_summary(**validation_filters),
        "firmware_update": _xvf_firmware_update_status(),
    }
    usb_mic = build_usb_mic_status(payload)
    usb_mic["source_selection"] = _usb_mic_source_selection(
        env,
        bridge_active=bridge_active,
    )
    payload["usb_mic"] = usb_mic
    payload["mic_settings"] = build_microphone_settings_view(payload)
    return payload


def _fresh_bridge_usb_mic_source(
    *,
    bridge_active: bool,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return only an authoritative, fresh bridge-applied source.

    Persisted intent changes before the non-blocking bridge restart completes,
    so status must never present the request as applied. The bridge stats file
    is the runtime authority; missing, stale, malformed, or inactive state has
    no applied answer.
    """

    if not bridge_active:
        return None
    try:
        payload = json.loads(
            Path(_AEC_BRIDGE_STATS_FILE).read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            return None
        updated = float(payload.get("updated_epoch_sec"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    age = (time.time() if now is None else now) - updated
    if not math.isfinite(updated) or age < 0 or age > _AEC_BRIDGE_STATS_FRESH_SECONDS:
        return None
    active_plan = payload.get("active_capture_plan")
    if not isinstance(active_plan, dict):
        return None
    source = active_plan.get("usb_mic_source")
    if not isinstance(source, dict):
        return None
    mode = str(source.get("mode") or "").strip()
    leg = str(source.get("leg") or "").strip()
    selection = str(source.get("selection") or "").strip()
    if not selection or not mode or not leg:
        return None
    return {
        "selection": selection,
        "mode": mode,
        "leg": leg,
        "fallback_active": bool(source.get("fallback_active")),
    }


def _usb_mic_effective_label(
    source: dict[str, Any],
    choices: list[dict[str, Any]],
) -> str:
    """Name the physical source proved by fresh bridge stats.

    ``selection`` is persisted intent and can stay on a chip beam while the
    bridge falls back to software AEC.  The applied label must therefore come
    from runtime ``mode`` + physical ``leg``, never from selection alone.
    """

    mode = source["mode"]
    leg = source["leg"]
    if mode == "software_aec3" or leg == "clean":
        return "Software-clean microphone"
    physical_choice = next(
        (
            choice
            for choice in choices
            if isinstance(choice, dict) and choice.get("value") == leg
        ),
        None,
    )
    if physical_choice is not None:
        return str(physical_choice.get("label") or leg)
    return leg


def _usb_mic_source_selection(
    env: dict[str, str],
    *,
    bridge_active: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Build requested/available/applied computer-mic source status."""

    requested = read_usb_mic_leg()
    choices = usb_mic_leg_choices(env)
    applied_source = _fresh_bridge_usb_mic_source(
        bridge_active=bridge_active,
        now=now,
    )
    applied: dict[str, Any] | None = None
    if applied_source is not None:
        applied_value = applied_source["selection"]
        mode = applied_source["mode"]
        leg = applied_source["leg"]
        choice = next(
            (
                candidate
                for candidate in choices
                if candidate.get("value") == applied_value
            ),
            None,
        )
        applied = {
            "value": applied_value,
            "label": (
                str(choice.get("label") or applied_value)
                if choice is not None
                else applied_value
            ),
            "mode": mode,
            "leg": leg,
            "effective_label": _usb_mic_effective_label(
                applied_source,
                choices,
            ),
            "fallback_active": bool(applied_source.get("fallback_active")),
        }
    return {
        "requested": requested,
        "choices": choices,
        "applied": applied,
    }


def _applied_aec_intent(
    requested: AecIntent,
    *,
    runtime: RuntimeAecEnv,
    profile_status: dict[str, Any],
) -> AecIntent:
    """Translate reconciler-applied runtime env into the active AEC path.

    ``aec_mode.env`` is user intent. ``/etc/jasper/jasper.env`` is what the
    reconciler actually applied after mic/DAC gates, hotplug, and fail-closed
    fallback. Status surfaces must expose the latter for active engine/leg state
    while keeping the former visible as raw intent.
    """

    selection = str(profile_status.get("selection") or requested.profile_selection)
    active = str(profile_status.get("active") or "")
    if requested.mode != "auto" or active == PROFILE_DIRECT_MIC:
        return AecIntent(
            mode="disabled",
            raw_enabled=False,
            dtln_enabled=False,
            chip_aec_enabled=False,
            chip_aec_150_enabled=False,
            chip_aec_210_enabled=False,
            profile_selection=selection,
        )
    if active in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}:
        return AecIntent(
            mode="auto",
            raw_enabled=False,
            dtln_enabled=False,
            chip_aec_enabled=True,
            chip_aec_150_enabled=bool(runtime.chip_aec_150_device),
            chip_aec_210_enabled=bool(runtime.chip_aec_210_device),
            profile_selection=selection,
        )
    if active != PROFILE_XVF_SOFTWARE_AEC3:
        return AecIntent(
            mode="auto",
            raw_enabled=False,
            dtln_enabled=False,
            chip_aec_enabled=False,
            chip_aec_150_enabled=False,
            chip_aec_210_enabled=False,
            profile_selection=selection,
        )
    return AecIntent(
        mode="auto",
        raw_enabled=bool(runtime.raw_device),
        dtln_enabled=bool(runtime.dtln_enabled or runtime.dtln_device),
        chip_aec_enabled=False,
        chip_aec_150_enabled=False,
        chip_aec_210_enabled=False,
        profile_selection=selection or PROFILE_XVF_SOFTWARE_AEC3,
    )


def _bridge_role(intent: AecIntent, *, profile_status: dict[str, Any]) -> str:
    active_profile = str(profile_status.get("active") or "")
    if intent.mode != "auto" or active_profile == PROFILE_DIRECT_MIC:
        return "off"
    if active_profile in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}:
        return "chip_aec_carrier"
    if active_profile == PROFILE_XVF_SOFTWARE_AEC3:
        return "software_aec3"
    return "pending"


def _software_aec3_status(
    intent: AecIntent,
    *,
    bridge_active: bool,
    profile_status: dict[str, Any],
) -> dict[str, Any]:
    """Derived WebRTC/AEC3 status, separate from the shared bridge carrier.

    Chip-AEC still needs ``jasper-aec-bridge`` as the UDP carrier into
    jasper-voice, but that carrier does not instantiate the WebRTC AEC3 engine.
    Keep that distinction explicit so operator surfaces never present "bridge
    running" as "software AEC3 running."
    """
    active_profile = str(profile_status.get("active") or "")
    requested_profile = str(profile_status.get("requested") or "")
    profile_reason = str(profile_status.get("reason") or "")

    if intent.mode != "auto" or active_profile == PROFILE_DIRECT_MIC:
        return {
            "configured": False,
            "active": False,
            "bypassed": False,
            "reason": "AEC bridge is disabled by the direct-mic profile.",
        }
    if active_profile in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}:
        return {
            "configured": False,
            "active": False,
            "bypassed": True,
            "reason": (
                "Chip-AEC profile selected; WebRTC AEC3 is bypassed while "
                "the bridge carries the chip beam to voice."
            ),
        }
    software_selected = bool(
        active_profile == PROFILE_XVF_SOFTWARE_AEC3
        or requested_profile == PROFILE_XVF_SOFTWARE_AEC3
    )
    software_active = bool(
        bridge_active and active_profile == PROFILE_XVF_SOFTWARE_AEC3
    )
    if not software_active and profile_reason:
        return {
            "configured": software_selected,
            "active": False,
            "bypassed": False,
            "reason": profile_reason,
        }
    return {
        "configured": software_selected,
        "active": software_active,
        "bypassed": False,
        "reason": (
            "Software AEC3 bridge is active."
            if software_active
            else "Software AEC3 is selected; waiting for the bridge."
        ),
    }
