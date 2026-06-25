# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC and wake-threshold helpers behind jasper-control endpoints."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from ..audio_profile_state import (
    AecIntent,
    MicProbe,
    PROFILE_XVF_CHIP_AEC_TESTING,
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
from ..chip_aec_policy import gate_from_runtime_env, resolve_chip_aec_dac_gate
from ..wake_models import WAKE_MODEL_FILE

_AEC_MODE_FILE = "/var/lib/jasper/aec_mode.env"
_WAKE_MODEL_FILE = WAKE_MODEL_FILE
_JASPER_ENV_FILE = "/etc/jasper/jasper.env"

# Default leg policy — must match deploy/install.sh's reconcile_aec_state
# and deploy/bin/jasper-aec-reconcile's ensure_mode_file. Raw is on
# by default (~5 MB / negligible CPU, gives OR-fusion wake-rate
# recovery), DTLN is off by default (~75 MB / ~25% one core, opt-in),
# chip-AEC is off by default (hardware-conditional, mutually exclusive
# with raw/DTLN — the chip-AEC promotion).
_LEG_DEFAULT_RAW = True
_LEG_DEFAULT_DTLN = False
_LEG_DEFAULT_CHIP_AEC = False
_PROFILE_DEFAULT = "custom"

# Operator-facing wake-leg toggle name -> jasper.wake_legs token(s). Values
# are tuples because one operator toggle can arm more than one leg: the
# "chip_aec" toggle (JASPER_WAKE_LEG_CHIP_AEC) arms BOTH fixed-beam legs
# (chip_aec_150 + chip_aec_210), with the reconciler fanning the single
# boolean out to JASPER_MIC_DEVICE_CHIP_AEC_150/_210. The chip-direct /
# AEC-OFF leg is exposed to operators (the /wake/ card, /aec/leg, the
# JASPER_WAKE_LEG_RAW env var, the bash reconciler) as "raw", but its frozen
# wire token is "off". Do NOT confuse "raw" with the "raw0" corpus-only leg
# (chip channel 2, no toggle). This map is the single place those mappings
# are spelled out; leg-toggle validation goes through its keys. See
# docs/HANDOFF-mic-fusion-architecture.md.
_TOGGLE_TO_TOKEN = {
    "raw": ("off",),
    "dtln": ("dtln",),
    "chip_aec": ("chip_aec_150", "chip_aec_210"),
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
    key = f"JASPER_WAKE_LEG_{leg.upper()}"
    _atomic_rewrite_env(
        _AEC_MODE_FILE,
        {
            key: "1" if enabled else "0",
            "JASPER_AUDIO_INPUT_PROFILE": "custom",
        },
    )


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
) -> dict[str, Any]:
    """Read-only mic/profile status for the /wake/ page.

    This is intentionally descriptive and side-effect-free: it reads the
    reconciler-owned env file plus the XVF profile's firmware helpers,
    then classifies intent vs observed runtime. It does not probe audio
    streams or open devices on the hot polling path.
    """
    env = _fresh_jasper_env()
    runtime = runtime_env_from_mapping(env, process_env=os.environ)
    try:
        from ..mics import xvf3800
        runtime_profile = xvf3800.detect_runtime_profile()
        xvf_present = runtime_profile.present
        capture_channels = runtime_profile.capture_channels
        recommended_channels = xvf3800.RECOMMENDED_CAPTURE_CHANNELS
        display_name = runtime_profile.display_name
        alsa_card_name = runtime_profile.alsa_card_name
        variant_id = runtime_profile.variant_id
        geometry = runtime_profile.geometry
        chip_beam_plan = runtime_profile.chip_beam_plan_id
        probe_error = None
    except Exception:  # noqa: BLE001
        xvf_present = False
        capture_channels = None
        recommended_channels = 6
        display_name = "Seeed ReSpeaker XVF3800 (USB UA)"
        alsa_card_name = ""
        variant_id = ""
        geometry = ""
        chip_beam_plan = ""
        probe_error = "firmware probe failed"

    return build_audio_profile_status(
        AecIntent(
            mode=state["mode"],
            raw_enabled=bool(state["leg_raw"]),
            dtln_enabled=bool(state["leg_dtln"]),
            chip_aec_enabled=bool(state["leg_chip_aec"]),
            profile_selection=str(state.get("profile") or ""),
        ),
        runtime,
        MicProbe(
            xvf_present=xvf_present,
            capture_channels=capture_channels,
            recommended_channels=recommended_channels,
            display_name=display_name,
            alsa_card_name=alsa_card_name,
            variant_id=variant_id,
            geometry=geometry,
            chip_beam_plan=chip_beam_plan,
            probe_error=probe_error,
        ),
        bridge_active=bridge_active,
        chip_available=chip_available,
        chip_gate=chip_gate,
    )


def _chip_aec_available() -> bool:
    """True when the detected XVF variant has a validated beam plan."""
    try:
        from ..mics import xvf3800
        return xvf3800.detect_runtime_profile().chip_aec_supported
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
        gate = runtime_gate
    else:
        gate = resolve_chip_aec_dac_gate(
            env.get("JASPER_AUDIO_DAC_ID", "unknown"),
            testing_requested=testing_requested,
        )
    payload = gate.to_dict()
    payload["mic_available"] = mic_available
    payload["production_available"] = bool(mic_available and gate.production_allowed)
    payload["testing_available"] = bool(mic_available and gate.testing_allowed)
    payload["available"] = bool(
        mic_available
        and (
            gate.production_allowed
            or (testing_requested and gate.testing_allowed)
        )
    )
    blockers: list[str] = []
    if not mic_available:
        blockers.append("mic_beam_plan")
    dac_available_for_selection = gate.production_allowed or (
        testing_requested and gate.testing_allowed
    )
    if not dac_available_for_selection:
        blockers.append("dac_gate")
    payload["blockers"] = blockers
    return payload


def _mic_status(
    state: dict[str, Any],
    *,
    bridge_active: bool,
    chip_available: bool,
) -> dict[str, Any]:
    """Compatibility wrapper for callers that only need mic status."""
    return _audio_profile_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
    )["microphone"]


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
    # Wrap defensively so a profile probe failure can never 500 a status
    # GET the /wake/ page polls every 3 s.
    chip_available = _chip_aec_available()
    chip_gate = _chip_aec_gate(env, state, mic_available=chip_available)
    effective = resolve_audio_input_intent(
        AecIntent(
            mode=state["mode"],
            raw_enabled=bool(state["leg_raw"]),
            dtln_enabled=bool(state["leg_dtln"]),
            chip_aec_enabled=bool(state["leg_chip_aec"]),
            profile_selection=str(state.get("profile") or ""),
        ),
        chip_available=bool(chip_gate.get("available")),
    )
    profile_status = _audio_profile_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
        chip_gate=chip_gate,
    )
    requested_profile = (
        profile_status["audio_profile"].get("validation_profile")
        or validation_profile(profile_status["audio_profile"].get("requested"))
    )
    validation_filters = _audio_validation_filter_kwargs(
        requested_profile=requested_profile,
        system_env=env,
    )
    return {
        "mode": effective.mode,
        "profile": state["profile"],
        "raw_intent": {
            "mode": state["mode"],
            "leg_raw": state["leg_raw"],
            "leg_dtln": state["leg_dtln"],
            "leg_chip_aec": state["leg_chip_aec"],
        },
        "bridge_active": bridge_active,
        "legs": {
            "raw": {"configured": effective.raw_enabled},
            "dtln": {"configured": effective.dtln_enabled},
            "chip_aec": {
                "configured": effective.chip_aec_enabled,
                "available": chip_gate["available"],
                "production_available": chip_gate["production_available"],
                "testing_available": chip_gate["testing_available"],
            },
        },
        "threshold": _read_wake_threshold(),
        "wake_word": _read_wake_word_status(),
        "chip_aec_gate": chip_gate,
        "audio_profile": profile_status["audio_profile"],
        "microphone": profile_status["microphone"],
        "validation": _audio_validation_summary(**validation_filters),
    }
