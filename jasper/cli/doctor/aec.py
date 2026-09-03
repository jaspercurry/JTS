# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — aec domain."""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import NamedTuple
from ... import enhanced_aec
from ...aec_ready import aec_bridge_ready_marker_path, read_aec_bridge_ready
from ...audio_profile_state import (
    AEC_MODE_ENV,
    AecIntent,
    DEFAULT_AEC_MODE_PATH,
    MicProbe,
    PROFILE_CUSTOM,
    PROFILE_XVF_CHIP_AEC,
    PROFILE_XVF_CHIP_AEC_TESTING,
    RuntimeAecEnv,
    build_audio_profile_status,
    infer_audio_input_profile,
    normalize_audio_input_profile,
    parse_env_bool,
    runtime_env_from_mapping,
    validation_profile as _audio_validation_profile,
)
from ...audio_validation import CHIP_AEC_PROFILE
from ...audio_validation import current_artifact_filter_kwargs as _audio_validation_filter_kwargs
from ...audio_validation import latest_artifact_summary as _audio_validation_summary
from ...chip_aec.health import STATUS_READY
from ...chip_aec.policy import (
    STATUS_APPROVED,
    effective_chip_aec_dac_gate,
    resolve_chip_aec_dac_gate,
)
from ...env_load import parse_env_file as _shared_parse_env_file
from ..aec_bridge_config import (
    OUTPUTD_REF_UDP_HOST_ENV,
    OUTPUTD_REF_UDP_PORT_ENV,
    REF_SOURCE_ENV,
)
from ..aec_bridge_engines import DTLN_ENABLED_ENV
from ..aec_bridge_telemetry import BRIDGE_STATS_PATH_ENV
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _CHIP_AEC_PASSIVE_REQUIRED_CHECKS,
    _loopback_playback_active,
    _parked_follower_result,
    _run,
    _sha256_file,
)

# One snake_case constant per distinct decision branch across the aec-domain
# checks below. `detail` stays the human sentence (free to reword); `reason`
# is what tests pin instead (AGENTS.md: assert types/codes/structured fields,
# never prose). Grouped by check.


REASON_AUDIO_PROFILE_OK = "audio_profile_ok"
REASON_AUDIO_PROFILE_NEEDS_ATTENTION = "audio_profile_needs_attention"

REASON_ALIGNMENT_NO_VERDICT = "alignment_no_verdict"
REASON_ALIGNMENT_READY = "alignment_ready"
REASON_ALIGNMENT_NOT_READY = "alignment_not_ready"

REASON_ENHANCED_AEC_NOT_REQUESTED = "enhanced_aec_not_requested"
REASON_ENHANCED_AEC_STATUS_UNAVAILABLE = "enhanced_aec_status_unavailable"
REASON_ENHANCED_AEC_INSTALLED = "enhanced_aec_installed"
REASON_ENHANCED_AEC_INSTALLING = "enhanced_aec_installing"
REASON_ENHANCED_AEC_UNAVAILABLE = "enhanced_aec_unavailable"

REASON_VALIDATION_NOT_CHIP_PROFILE = "validation_not_chip_profile"
REASON_VALIDATION_CURRENT_PASS = "validation_current_pass"
REASON_VALIDATION_PASSIVE_EVIDENCE = "validation_passive_evidence"
REASON_VALIDATION_ADVISORY = "validation_advisory"

REASON_BRIDGE_RUNNING = "bridge_running"
REASON_BRIDGE_COMMISSIONING = "bridge_commissioning_in_progress"
REASON_BRIDGE_MODE_DISABLED = "bridge_mode_disabled"
REASON_BRIDGE_CHIP_ABSENT = "bridge_off_chip_absent"
REASON_BRIDGE_WRONG_FIRMWARE = "bridge_off_wrong_firmware"
REASON_BRIDGE_DOWN_READY_PRESENT = "bridge_down_ready_marker_present"
REASON_BRIDGE_DOWN_READY_ABSENT = "bridge_down_ready_marker_absent"

# `_assess_aec_reference_input_from_stats`'s schema-v4 contract branches.
REASON_REF_CONTRACT_NOT_OBJECT = "ref_contract_not_object"
REASON_REF_CONTRACT_MISSING_FIELD = "ref_contract_missing_field"
REASON_REF_CONTRACT_INVALID_NUMERIC = "ref_contract_invalid_numeric"
REASON_REF_CONTRACT_INVALID_SOURCE = "ref_contract_invalid_source"
REASON_REF_CONTRACT_INVALID_ENDPOINT = "ref_contract_invalid_endpoint"
REASON_REF_CONTRACT_INVALID_FRAMES_ENQUEUED = "ref_contract_invalid_frames_enqueued"
REASON_REF_CONTRACT_SNAPSHOT_IN_FUTURE = "ref_contract_snapshot_in_future"
REASON_REF_CONTRACT_PROCESS_AGE_EXCEEDS_SNAPSHOT = (
    "ref_contract_process_age_exceeds_snapshot"
)
REASON_REF_STATS_WRITER_STALE = "ref_stats_writer_stale"
REASON_REF_CONTRACT_FRAME_AGE_NOT_NULL = "ref_contract_frame_age_not_null"
REASON_REF_CONTRACT_FRAME_AGE_EXCEEDS_PROCESS_AGE = (
    "ref_contract_frame_age_exceeds_process_age"
)
REASON_REF_ROUTE_MISMATCH = "ref_route_mismatch"
REASON_REF_STARTUP_GRACE = "ref_startup_grace"
REASON_REF_ZERO_FRAMES_AFTER_GRACE = "ref_zero_frames_after_grace"
REASON_REF_RECEIVER_STALE = "ref_receiver_stale"
REASON_REF_RECEIVER_CURRENT = "ref_receiver_current"

# `_assess_aec_bridge_output` + `check_aec_bridge_output_health`'s own branches.
REASON_BRIDGE_OUTPUT_BRIDGE_NOT_RUNNING = "bridge_output_bridge_not_running"
REASON_BRIDGE_OUTPUT_JOURNAL_UNREADABLE = "bridge_output_journal_unreadable"
REASON_BRIDGE_OUTPUT_REF_SILENT_NO_MUSIC = "bridge_output_ref_silent_no_music"
# The four `_aec_reference_failure_remediation` localization outcomes: which
# hop the remediation could name, each pointing at a different fix.
REASON_BRIDGE_OUTPUT_REF_SILENT = "bridge_output_ref_silent"
REASON_BRIDGE_OUTPUT_REF_SILENT_TARGET_UNKNOWN = (
    "bridge_output_ref_silent_target_unknown"
)
REASON_BRIDGE_OUTPUT_REF_SILENT_ENDPOINT_MISMATCH = (
    "bridge_output_ref_silent_endpoint_mismatch"
)
REASON_BRIDGE_OUTPUT_REF_SILENT_UNCONFIRMED = "bridge_output_ref_silent_unconfirmed"
REASON_BRIDGE_OUTPUT_NO_WINDOWS = "bridge_output_no_windows"
REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY = "bridge_output_ref_proven_healthy"
REASON_BRIDGE_OUTPUT_CHIP_ONLY = "bridge_output_chip_only"
REASON_BRIDGE_OUTPUT_IDLE = "bridge_output_idle"
REASON_BRIDGE_OUTPUT_HEALTHY_WORK = "bridge_output_healthy_work"

REASON_DTLN_DISABLED = "dtln_disabled"
REASON_DTLN_SIZE_NOT_INTEGER = "dtln_size_not_integer"
REASON_DTLN_SIZE_NOT_REGISTERED = "dtln_size_not_registered"
REASON_DTLN_MODEL_FILES_MISSING = "dtln_model_files_missing"
REASON_DTLN_MODEL_HASH_MISMATCH = "dtln_model_hash_mismatch"
REASON_DTLN_BRIDGE_NOT_RUNNING = "dtln_bridge_not_running"
REASON_DTLN_JOURNAL_UNREADABLE = "dtln_journal_unreadable"
REASON_DTLN_LOADED_FROM_STATS = "dtln_loaded_from_stats"
REASON_DTLN_ENGINE_UNAVAILABLE = "dtln_engine_unavailable"
REASON_DTLN_NOT_STARTED_WITH_LEG = "dtln_not_started_with_leg"
REASON_DTLN_LOADED_FROM_JOURNAL = "dtln_loaded_from_journal"
REASON_DTLN_LOAD_FAILED = "dtln_load_failed"
REASON_DTLN_NO_INIT_LINE = "dtln_no_init_line"

REASON_XVF_FIRMWARE_CARD_ABSENT = "xvf_firmware_card_absent"
REASON_XVF_FIRMWARE_WRONG_CHANNELS = "xvf_firmware_wrong_channels"
REASON_XVF_MIXER_CARD_ABSENT = "xvf_mixer_card_absent"
REASON_XVF_MIXER_CGET_FAILED = "xvf_mixer_cget_failed"
REASON_XVF_MIXER_DRIFT = "xvf_mixer_drift"


def _aec_mode_env() -> dict[str, str]:
    """The wizard-owned mode file, read fresh per call; missing or unreadable
    reads as empty so each setting falls back to its reconcile_aec_state seed."""
    return _shared_parse_env_file(str(DEFAULT_AEC_MODE_PATH))


def _aec_mode_setting() -> str:
    return _aec_mode_env().get(AEC_MODE_ENV) or "auto"


def _aec_profile_setting() -> str:
    """Empty string means pre-profile config; audio_profile_state infers the
    nearest legacy profile from JASPER_AEC_MODE + leg booleans."""

    return _aec_mode_env().get("JASPER_AUDIO_INPUT_PROFILE", "")


def _wake_leg_setting(key: str, default: bool) -> bool:
    raw = _aec_mode_env().get(key)
    return default if raw is None else parse_env_bool(raw, default)


def _doctor_env_file() -> dict[str, str]:
    """Parse the reconciler-applied runtime env fresh.

    The doctor is a one-shot CLI, so it reads the env file rather than
    trusting whatever the calling shell inherited.
    """

    return _shared_parse_env_file(
        os.environ.get("JASPER_ENV_FILE", "/etc/jasper/jasper.env"),
    )


def _doctor_aec_intent() -> AecIntent:
    """The operator-requested AEC state, from the wizard-owned mode file."""

    return AecIntent(
        mode=_aec_mode_setting(),
        raw_enabled=_wake_leg_setting("JASPER_WAKE_LEG_RAW", True),
        dtln_enabled=_wake_leg_setting("JASPER_WAKE_LEG_DTLN", False),
        chip_aec_enabled=_wake_leg_setting("JASPER_WAKE_LEG_CHIP_AEC", False),
        profile_selection=_aec_profile_setting(),
    )


def _doctor_audio_input_selection() -> str:
    """The profile this box is on — the same resolution /aec applies."""

    intent = _doctor_aec_intent()
    return normalize_audio_input_profile(
        intent.profile_selection,
        default=infer_audio_input_profile(intent),
    )


def _chip_aec_available_for_doctor() -> bool:
    try:
        from ...mics import xvf3800
        return xvf3800.detect_runtime_profile().chip_aec_supported
    except Exception:  # noqa: BLE001
        return False

def _audio_profile_status_for_doctor(
    *,
    bridge_active: bool | None = None,
    env: dict[str, str] | None = None,
    mic_probe: MicProbe | None = None,
) -> dict:
    """Build the same read-only audio-profile status used by /aec.

    The doctor is a one-shot CLI, but it still reads the reconciler-owned
    env file fresh so it reports the applied runtime env rather than only
    whatever the calling shell inherited.
    """

    if bridge_active is None:
        bridge_active = (
            _run(["systemctl", "is-active", "jasper-aec-bridge.service"])
            .stdout.strip() == "active"
        )
    if env is None:
        env = _doctor_env_file()
    runtime = runtime_env_from_mapping(env, process_env=os.environ)

    if mic_probe is None:
        try:
            from ...mics import xvf3800
            runtime_profile = xvf3800.detect_runtime_profile()
            mic_probe = MicProbe(
                xvf_present=runtime_profile.present,
                capture_channels=runtime_profile.capture_channels,
                recommended_channels=xvf3800.RECOMMENDED_CAPTURE_CHANNELS,
                display_name=runtime_profile.display_name,
                alsa_card_name=runtime_profile.alsa_card_name,
                variant_id=runtime_profile.variant_id,
                geometry=runtime_profile.geometry,
                chip_beam_plan=runtime_profile.chip_beam_plan_id,
                chip_aec_supported=runtime_profile.chip_aec_supported,
            )
        except Exception:  # noqa: BLE001
            mic_probe = MicProbe(
                xvf_present=False,
                capture_channels=None,
                probe_error="firmware probe failed",
            )

    chip_available = mic_probe.chip_aec_supported
    profile_selection = _aec_profile_setting()
    testing_requested = (
        normalize_audio_input_profile(profile_selection, default="")
        == PROFILE_XVF_CHIP_AEC_TESTING
    )
    gate = effective_chip_aec_dac_gate(env, testing_requested=testing_requested)
    status = build_audio_profile_status(
        _doctor_aec_intent(),
        runtime,
        mic_probe,
        bridge_active=bridge_active,
        chip_available=chip_available,
        chip_gate=gate.to_dict(),
    )
    status["chip_aec_gate"] = gate.to_dict()
    return status

def _assess_audio_profile(status: dict) -> CheckResult:
    profile = status.get("audio_profile") or {}
    mic = status.get("microphone") or {}
    raw_warnings = mic.get("warnings")
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    state = str(profile.get("state") or "unknown")
    active = profile.get("active") or "none"
    legs = mic.get("wake_legs")
    if isinstance(legs, list) and legs:
        legs_text = ", ".join(str(leg) for leg in legs)
    else:
        legs_text = "none"
    detail = (
        f"requested={profile.get('requested') or 'unknown'}, "
        f"active={active}, state={state}; "
        f"mode={mic.get('processing_mode') or 'unknown'}, "
        f"session={mic.get('session_source') or 'unknown'}, "
        f"legs={legs_text}"
    )
    gate = status.get("chip_aec_gate")
    if isinstance(gate, dict):
        detail += (
            f"; chip_aec_gate={gate.get('status') or 'unknown'}"
            f"/{gate.get('source') or 'unknown'}"
        )
    if warnings:
        detail += "; " + " ".join(str(w) for w in warnings)

    if state in {"active", "disabled"} and not warnings:
        result = "ok"
        reason = REASON_AUDIO_PROFILE_OK
    else:
        result = "warn"
        reason = REASON_AUDIO_PROFILE_NEEDS_ATTENTION
    return CheckResult("Audio profile", result, detail, reason=reason)

@doctor_check(order=46, group="aec")
def check_audio_profile_runtime() -> CheckResult:
    """Summarise requested vs applied mic/AEC profile runtime truth."""
    parked = _parked_follower_result("Audio profile")
    if parked is not None:
        return parked

    return _assess_audio_profile(_audio_profile_status_for_doctor())


def _assess_chip_aec_alignment(
    runtime: RuntimeAecEnv, selection: str,
) -> CheckResult:
    """Render the published chip-AEC alignment record as one doctor row.

    `jasper.chip_aec.health` judges the record and `AlignmentHealth.
    applies_to` says which selection it answers for; this only reports what
    they say. An absent or unowned record is not a fault: on `custom` the
    chip arms on the live evidence `_wake_engine` checks, and on a managed
    selection the profile stays pending until the reconciler publishes one.

    Severity mirrors what the Audio profile row already gave these states:
    ready is ok and every other status warns.
    """

    health = runtime.chip_aec_alignment
    if not health.status or not health.applies_to(
        selection, custom_profile=PROFILE_CUSTOM,
    ):
        return CheckResult(
            "Chip-AEC alignment", "ok",
            f"no alignment verdict published for {selection or 'this profile'}",
            reason=REASON_ALIGNMENT_NO_VERDICT,
        )

    legs = [
        name
        for name, device in (
            ("chip_aec_150", runtime.chip_aec_150_device),
            ("chip_aec_210", runtime.chip_aec_210_device),
        )
        if device
    ]
    detail = (
        f"state={health.status}, "
        f"selection={health.selection or 'unstamped'}, "
        f"armed={'+'.join(legs) if runtime.chip_enabled and legs else 'none'}"
    )
    if health.reason:
        detail += f"; {health.reason}"
    if health.status != STATUS_READY and health.action:
        detail += f"; action={health.action}"
    ready = health.status == STATUS_READY
    return CheckResult(
        "Chip-AEC alignment",
        "ok" if ready else "warn",
        detail,
        reason=REASON_ALIGNMENT_READY if ready else REASON_ALIGNMENT_NOT_READY,
    )


@doctor_check(order=45.5, group="aec")
def check_chip_aec_alignment() -> CheckResult:
    """Report the reconciler's chip-AEC alignment verdict, unaltered."""
    parked = _parked_follower_result("Chip-AEC alignment")
    if parked is not None:
        return parked
    return _assess_chip_aec_alignment(
        runtime_env_from_mapping(_doctor_env_file(), process_env=os.environ),
        _doctor_audio_input_selection(),
    )


def _assess_enhanced_aec_status(payload: dict) -> CheckResult:
    """Translate the optional install lifecycle into one advisory row."""

    state = str(payload.get("state") or "unavailable")
    detail = str(payload.get("detail") or payload.get("summary") or "").strip()
    if state in {"installed", "not_needed"}:
        return CheckResult(
            "Enhanced AEC",
            "ok",
            detail or f"optional enhancement state={state}",
            reason=REASON_ENHANCED_AEC_INSTALLED,
        )
    if state == "installing":
        return CheckResult(
            "Enhanced AEC",
            "ok",
            detail or "optional enhancement is installing in the background",
            reason=REASON_ENHANCED_AEC_INSTALLING,
        )
    return CheckResult(
        "Enhanced AEC",
        "warn",
        (
            detail or f"requested optional enhancement state={state}"
        )
        + "; standard echo cancellation remains available — retry from /system/",
        reason=REASON_ENHANCED_AEC_UNAVAILABLE,
    )


@doctor_check(order=46.5, group="aec")
def check_enhanced_aec() -> CheckResult:
    """Report only an explicitly requested optional enhancement.

    A missing intent is healthy and deliberately avoids fingerprinting native
    inputs on every doctor run. Once requested, failure/staleness remains an
    advisory because mandatory v1 is still the supported fallback.
    """

    if not enhanced_aec.read_intent()["requested"]:
        return CheckResult(
            "Enhanced AEC",
            "ok",
            "not requested; standard echo cancellation is installed",
            reason=REASON_ENHANCED_AEC_NOT_REQUESTED,
        )

    chip_active = False
    try:
        audio_profile = (
            _audio_profile_status_for_doctor().get("audio_profile") or {}
        )
        active_profile = normalize_audio_input_profile(
            str(audio_profile.get("active") or ""),
            default="",
        )
        chip_active = active_profile in {
            PROFILE_XVF_CHIP_AEC,
            PROFILE_XVF_CHIP_AEC_TESTING,
        }
        service_state = _run(
            ["systemctl", "is-active", "jasper-enhanced-aec-install.service"]
        ).stdout.strip()
        payload = enhanced_aec.status(
            chip_aec_active=chip_active,
            service_active=service_state
            in {"active", "activating", "reloading"},
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return CheckResult(
            "Enhanced AEC",
            "warn",
            "requested optional enhancement status could not be inspected "
            f"({type(exc).__name__}); standard echo cancellation remains "
            "available — retry from /system/",
            reason=REASON_ENHANCED_AEC_STATUS_UNAVAILABLE,
        )
    return _assess_enhanced_aec_status(payload)


def _assess_audio_validation_summary(
    summary: dict[str, object],
    *,
    requested_profile: str | None,
) -> CheckResult:
    state = str(summary.get("state") or "unknown")
    status = str(summary.get("status") or "unknown")
    recommendation = str(summary.get("recommendation") or "none")
    validated_at = str(summary.get("validated_at") or "never")
    path = str(summary.get("artifact_path") or "unknown")
    detail = (
        f"profile={requested_profile or 'unknown'}, validation={state}, "
        f"status={status}, validated_at={validated_at}, "
        f"recommendation={recommendation}, path={path}"
    )
    reason = summary.get("reason")
    if reason:
        detail += f"; {reason}"

    if requested_profile != CHIP_AEC_PROFILE:
        return CheckResult(
            "Audio validation",
            "ok",
            detail + "; advisory because chip-AEC is not the requested profile",
            reason=REASON_VALIDATION_NOT_CHIP_PROFILE,
        )
    if state == "current" and status == "pass":
        return CheckResult(
            "Audio validation", "ok", detail,
            reason=REASON_VALIDATION_CURRENT_PASS,
        )
    passive_pair = _chip_aec_passive_evidence_pair(summary)
    if passive_pair is not None:
        return CheckResult(
            "Audio validation",
            "ok",
            detail
            + f"; mic={passive_pair[0]}/dac={passive_pair[1]} passed passive "
            "hardware validation on an approved chip-AEC DAC; optional "
            "acoustic drift/delay probe not implemented/run",
            reason=REASON_VALIDATION_PASSIVE_EVIDENCE,
        )
    if recommendation in {"run_hardware_validation", "run_drift_delay_validation"}:
        command = "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout"
    else:
        command = "sudo jasper-audio-validate --stdout"
    return CheckResult(
        "Audio validation",
        "warn",
        detail + f"; advisory: consider `{command}` after chip-AEC is active",
        reason=REASON_VALIDATION_ADVISORY,
    )

def _chip_aec_passive_evidence_pair(
    summary: dict[str, object],
) -> tuple[str, str] | None:
    """Return the (mic, DAC) pair whose passive evidence clears the warn.

    The artifact stays partial because no acoustic drift/delay probe exists
    yet. Clean passive evidence on a registry-approved DAC is enough to stop
    warning (ADR-0101): the gate below already answers "is this hardware
    known good", so no second hard-coded pair table is kept here. The caller
    discloses the pair.
    """

    if str(summary.get("state") or "unknown") != "current":
        return None
    if str(summary.get("recommendation") or "none") != "run_drift_delay_validation":
        return None
    hardware = summary.get("hardware")
    if not isinstance(hardware, dict):
        return None
    mic_id = str(hardware.get("mic_id") or "unknown")
    dac_id = str(hardware.get("dac_id") or "unknown")
    gate = resolve_chip_aec_dac_gate(dac_id)
    if gate.status != STATUS_APPROVED:
        return None
    statuses = summary.get("check_statuses")
    if not isinstance(statuses, dict):
        return None
    if not all(
        statuses.get(check_name) == "pass"
        for check_name in _CHIP_AEC_PASSIVE_REQUIRED_CHECKS
    ):
        return None
    return mic_id, dac_id

@doctor_check(order=47, group="aec")
def check_audio_validation_readiness() -> CheckResult:
    """Report latest schema-v1 validation artifact as advisory readiness."""

    profile_status = _audio_profile_status_for_doctor().get("audio_profile") or {}
    requested_profile = (
        profile_status.get("validation_profile")
        or _audio_validation_profile(profile_status.get("requested"))
    )
    validation_filters = _audio_validation_filter_kwargs(
        requested_profile=requested_profile,
        system_env=_doctor_env_file(),
    )
    return _assess_audio_validation_summary(
        _audio_validation_summary(**validation_filters),
        requested_profile=requested_profile,
    )

def _dfu_flash_remedy() -> str:
    """`xvf3800.dfu_flash_command` owns the command text so no hint drifts from it."""
    from ...mics import xvf3800
    return ("BRINGUP.md 'XVF firmware: switch to 6-channel variant via DFU' has the "
            f"procedure — in-system DFU, no Safe Mode entry: {xvf3800.dfu_flash_command()}")


@doctor_check(order=45, group="aec")
def check_aec_bridge_running() -> CheckResult:
    """jasper-aec-bridge runs WebRTC AEC3 echo cancellation on the XVF
    chip's ASR-tap channel (1 of the 6-ch firmware, see
    jasper/mics/xvf3800.py MIC_CHANNEL_INDEX), with the
    renderer→camilla loopback as far-end reference. Output goes over
    UDP localhost, which jasper-voice consumes as its mic source.

    AEC is the *desired* state — wake word fires more cleanly and
    false wakes during music playback drop dramatically. So we treat
    any "AEC could be on but isn't" state as a warning (gentle
    nudge), only suppressing it to ok when the operator explicitly
    opted out via JASPER_AEC_MODE=disabled. A silent-disabled bridge
    shows up as a hard fail."""
    parked = _parked_follower_result("AEC bridge")
    if parked is not None:
        return parked
    from ...mics import xvf3800
    is_active = _run(["systemctl", "is-active", "jasper-aec-bridge.service"]).stdout.strip()
    is_enabled = _run(["systemctl", "is-enabled", "jasper-aec-bridge.service"]).stdout.strip()

    if is_active == "active":
        # Which AEC the running bridge carries is the Audio profile row's
        # fact, and its alignment is the Chip-AEC alignment row's.
        return CheckResult(
            "AEC bridge service", "ok", "running", reason=REASON_BRIDGE_RUNNING,
        )

    # The commissioner stops the whole AEC stack for its audible measurement
    # (minutes) and its live marker parks every reconcile, so a down bridge is
    # the intended state — not a failure with a restart remedy.
    commission_state = _run(
        ["systemctl", "is-active", "jasper-aec-commission.service"]
    ).stdout.strip()
    if commission_state in {"active", "activating", "reloading"} or Path(
        "/run/jasper-chip-aec-commission/active"
    ).exists():
        return CheckResult(
            "AEC bridge service", "ok",
            "chip-AEC commissioning in progress; bridge intentionally stopped",
            reason=REASON_BRIDGE_COMMISSIONING,
        )

    aec_mode = _aec_mode_setting()
    capture_ch = xvf3800.capture_channels()
    chip_present = capture_ch is not None
    is_6ch = capture_ch == xvf3800.RECOMMENDED_FIRMWARE.capture_channels

    if aec_mode != "auto":
        # Explicit operator opt-out is fine.
        return CheckResult(
            "AEC bridge service", "ok",
            f"disabled (JASPER_AEC_MODE={aec_mode})",
            reason=REASON_BRIDGE_MODE_DISABLED,
        )

    if not chip_present:
        return CheckResult(
            "AEC bridge service", "warn",
            f"off — {xvf3800.DISPLAY_NAME} not present. Software AEC needs it; "
            "plug it in and the reconciler will enable AEC on next event.",
            reason=REASON_BRIDGE_CHIP_ABSENT,
        )

    if not is_6ch:
        return CheckResult(
            "AEC bridge service", "warn",
            f"off — XVF chip is on {capture_ch}-channel firmware, not "
            f"{xvf3800.RECOMMENDED_FIRMWARE.capture_channels}-ch. After flashing: "
            f"sudo systemctl start jasper-aec-reconcile. {_dfu_flash_remedy()}",
            reason=REASON_BRIDGE_WRONG_FIRMWARE,
        )

    # An alignment the reconciler could not apply no longer stops the bridge
    # (ADR-0101) — it runs software AEC3 and discloses — so a bridge that is
    # down here is a bridge that failed OR one the reconciler never admitted.
    # The ready marker separates the two (ADR-0224): PID 1 refuses to even
    # start the unit while it is absent, so a missing verdict is a reconciler
    # problem and a present one is a bridge problem. The alignment disclosure
    # itself reaches the operator through check_chip_aec_alignment, which
    # carries the reconciler's reason/action verbatim in every state.
    ready = read_aec_bridge_ready()
    if ready.ready:
        verdict = (
            f"ready marker present (reason={ready.reason or 'unknown'}), so the "
            "reconciler admitted the bridge and the bridge itself is down. "
            "Run: journalctl -u jasper-aec-bridge -e"
        )
        down_reason = REASON_BRIDGE_DOWN_READY_PRESENT
    else:
        verdict = (
            f"ready marker {aec_bridge_ready_marker_path()} absent, so no "
            "reconcile pass has admitted the bridge and systemd skips its start "
            "condition. Run: sudo systemctl start jasper-aec-reconcile && "
            "journalctl -u jasper-aec-reconcile -e"
        )
        down_reason = REASON_BRIDGE_DOWN_READY_ABSENT
    return CheckResult(
        "AEC bridge service", "fail",
        f"is-active='{is_active}', is-enabled='{is_enabled}'. "
        "AEC should be on (mode=auto, 6-ch firmware loaded) but the bridge "
        f"isn't running: {verdict}",
        reason=down_reason,
    )


# The bridge emits one of two RMS window shapes per 5 s, chosen by whether
# production chip AEC is armed (jasper/cli/aec_bridge.py):
#   software AEC3: "rms over 5.0s: ref=15694 mic=2077 aec=311 →
#                   attenuation=-16.5 dB (...)"
#   chip AEC:      "chip_aec rms over 5.0s: ref=15694 near=chip_aec_210:2077
#                   primary=chip_aec_150:311 level_delta=-16.5 dB raw0=2411
#                   (...)"
# `raw0` is the raw mic-0 capture channel. Optional: builds before it emitted
# only the chip-cancelled beams, and the bridge omits it for a window that
# drained no raw0 frames.
_AEC_RMS_RE = re.compile(
    r"rms over [\d.]+s: ref=(?P<ref>\d+) mic=(?P<mic>\d+) aec=\d+ → "
    r"attenuation=(?P<level>-?\d+\.\d+) dB"
)
_CHIP_AEC_RMS_RE = re.compile(
    r"chip_aec rms over [\d.]+s: ref=(?P<ref>\d+) near=[^\s:]+:(?P<near>\d+) "
    r"primary=[^\s:]+:\d+ level_delta=-?\d+\.\d+ dB(?: raw0=(?P<raw0>\d+))?"
)


class _RmsWindow(NamedTuple):
    """`mic` is the least-cancelled near-end level each shape offers: AEC3
    `mic` (capture lane 1), chip `raw0` (truly-raw capture channel 2), or the
    cancelled `near` beam when a chip line carries no `raw0`.
    `level_db` is AEC3 `attenuation` and is None on chip AEC, whose
    `level_delta` compares two beams the chip already cancelled."""

    ref: int
    mic: int
    level_db: float | None
    chip: bool


def _parse_rms_window(line: str) -> _RmsWindow | None:
    """Parse either bridge RMS log shape into one record, else None."""
    if m := _CHIP_AEC_RMS_RE.search(line):
        return _RmsWindow(
            ref=int(m["ref"]), mic=int(m["raw0"] or m["near"]),
            level_db=None, chip=True,
        )
    if m := _AEC_RMS_RE.search(line):
        return _RmsWindow(
            ref=int(m["ref"]), mic=int(m["mic"]),
            level_db=float(m["level"]), chip=False,
        )
    return None


# Thresholds for `check_aec_bridge_output_health`.
# Ambient room (no music) puts mic at ~600 RMS at our chip-side AGC
# config; music playback puts it in the 1500-3000+ range. Threshold
# 1500 distinguishes "music playing" from "idle".
_AEC_MIC_MUSIC_THRESHOLD = 1500  # RMS

# Reference is essentially silent below this. Healthy ref during
# music is 1000+ RMS.
_AEC_REF_SILENT_THRESHOLD = 50

# The bridge rewrites its stats snapshot every 0.5 s. A snapshot older than
# this belongs to a stopped/wedged writer and retains the journal fallback used
# by older bridge revisions.
_BRIDGE_STATS_FRESH_SEC = 30.0

# Schema 4 adds authoritative receiver-side progress for the reference input.
# UDP send success is not delivery proof, so only the bridge's successful
# conversion + bounded-queue enqueue advances this signal.
_AEC_REFERENCE_INPUT_STATS_SCHEMA_VERSION = 4
# A bridge younger than this has not necessarily bound its receiver yet.
_AEC_REFERENCE_INPUT_STARTUP_GRACE_SEC = 10.0
# outputd publishes a 20 ms reference frame continuously, so a gap this long
# past the grace is a stopped receiver, not a lull.
_AEC_REFERENCE_INPUT_STALE_SEC = 5.0


def _outputd_reference_localization(
    status: dict[str, object] | None,
    *,
    expected_endpoint: str,
    error: str | None = None,
) -> str:
    """Describe sender-side state without treating it as receiver proof."""

    if error:
        return f"outputd STATUS unavailable ({error})"
    if not isinstance(status, dict):
        return "outputd STATUS unavailable"
    reference_outputs = status.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return "outputd STATUS missing reference_outputs"

    target = reference_outputs.get("udp_target")
    active = reference_outputs.get("udp_active")
    error_count = reference_outputs.get("udp_error_count")
    if target != expected_endpoint:
        return (
            f"outputd STATUS udp_target={target!r}, expected "
            f"{expected_endpoint!r}"
        )
    if active is not True:
        return (
            "outputd STATUS reports UDP inactive "
            f"(cumulative udp_error_count={error_count!r})"
        )
    if isinstance(error_count, int) and not isinstance(error_count, bool):
        error_detail = f", cumulative udp_error_count={error_count}"
    else:
        error_detail = ", udp_error_count unavailable"
    return (
        "outputd STATUS claims UDP sender active"
        f"{error_detail}; send success is not receiver proof"
    )


def _assess_aec_reference_input_from_stats(
    stats: dict[str, object],
    now_monotonic: float,
    *,
    configured_source: str,
    expected_endpoint: str,
    outputd_status: dict[str, object] | None = None,
    outputd_status_error: str | None = None,
) -> tuple[CheckResult, bool] | None:
    """Assess current bridge-side reference receiver progress.

    Returns ``(result, startup_grace)`` for exact schema v4. ``None`` preserves
    the journal assessment for missing, older, and unknown-future schemas. A
    malformed or stale declared-v4 snapshot fails closed instead of aging into
    the legacy fallback. The second element lets the caller suppress previous-
    process journal windows during the explicit startup grace.

    ``configured_source`` is the route the CALLER resolved from the env plus
    the bridge's own published snapshot, not the env value alone (see
    ``check_aec_bridge_output_health``): those two diverge on a box parked by
    a pre-P7-1 reconciler, and gating on the env would skip this
    authoritative freshness check on exactly the box whose configuration is
    already known to be stale.
    """

    if configured_source != "outputd_udp":
        return None
    schema_version = stats.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != _AEC_REFERENCE_INPUT_STATS_SCHEMA_VERSION
    ):
        return None

    localization = _outputd_reference_localization(
        outputd_status,
        expected_endpoint=expected_endpoint,
        error=outputd_status_error,
    )

    def fail_contract(detail: str, reason: str) -> tuple[CheckResult, bool]:
        return (
            CheckResult(
                "AEC bridge output", "fail",
                f"bridge reference freshness schema v4 is untrustworthy: "
                f"{detail}. {localization}",
                reason=reason,
            ),
            False,
        )

    def nonnegative_number(value: object, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} is not a number")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field} is not representable") from exc
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{field} must be finite and nonnegative")
        return number

    reference_input = stats.get("reference_input")
    if not isinstance(reference_input, dict):
        return fail_contract(
            "reference_input is missing or not an object",
            REASON_REF_CONTRACT_NOT_OBJECT,
        )
    try:
        source = reference_input["source"]
        endpoint = reference_input["endpoint"]
        frames_enqueued = reference_input["frames_enqueued"]
        last_frame_age_ms = reference_input["last_frame_age_ms"]
        snapshot_monotonic_ms = nonnegative_number(
            reference_input["snapshot_monotonic_ms"],
            "reference_input.snapshot_monotonic_ms",
        )
        process_age_ms = nonnegative_number(
            reference_input["process_age_ms"],
            "reference_input.process_age_ms",
        )
        current_monotonic_sec = nonnegative_number(
            now_monotonic,
            "doctor monotonic clock",
        )
    except KeyError as exc:
        return fail_contract(
            f"missing required field {exc.args[0]!r}",
            REASON_REF_CONTRACT_MISSING_FIELD,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return fail_contract(str(exc), REASON_REF_CONTRACT_INVALID_NUMERIC)

    if not isinstance(source, str) or not source:
        return fail_contract(
            "reference_input.source is not a nonempty string",
            REASON_REF_CONTRACT_INVALID_SOURCE,
        )
    if not isinstance(endpoint, str) or not endpoint:
        return fail_contract(
            "reference_input.endpoint is not a nonempty string",
            REASON_REF_CONTRACT_INVALID_ENDPOINT,
        )
    if (
        isinstance(frames_enqueued, bool)
        or not isinstance(frames_enqueued, int)
        or frames_enqueued < 0
        or frames_enqueued > (1 << 64) - 1
    ):
        return fail_contract(
            "reference_input.frames_enqueued is not a nonnegative uint64",
            REASON_REF_CONTRACT_INVALID_FRAMES_ENQUEUED,
        )

    snapshot_monotonic_sec = snapshot_monotonic_ms / 1000.0
    process_age_at_snapshot_sec = process_age_ms / 1000.0
    snapshot_age_sec = current_monotonic_sec - snapshot_monotonic_sec
    if snapshot_age_sec < 0:
        return fail_contract(
            "reference_input.snapshot_monotonic_ms is in the future",
            REASON_REF_CONTRACT_SNAPSHOT_IN_FUTURE,
        )
    if process_age_at_snapshot_sec > snapshot_monotonic_sec:
        return fail_contract(
            "reference_input.process_age_ms exceeds the same-boot monotonic snapshot",
            REASON_REF_CONTRACT_PROCESS_AGE_EXCEEDS_SNAPSHOT,
        )
    if snapshot_age_sec > _BRIDGE_STATS_FRESH_SEC:
        return fail_contract(
            f"stats writer has not advanced for {snapshot_age_sec:.2f}s "
            f"(limit {_BRIDGE_STATS_FRESH_SEC:g}s)",
            REASON_REF_STATS_WRITER_STALE,
        )

    process_age_sec = process_age_at_snapshot_sec + snapshot_age_sec
    if frames_enqueued == 0:
        if last_frame_age_ms is not None:
            return fail_contract(
                "last_frame_age_ms must be null when frames_enqueued is zero",
                REASON_REF_CONTRACT_FRAME_AGE_NOT_NULL,
            )
        receiver_age_sec: float | None = None
    else:
        try:
            receiver_age_at_snapshot_sec = nonnegative_number(
                last_frame_age_ms,
                "reference_input.last_frame_age_ms",
            ) / 1000.0
        except (TypeError, ValueError, OverflowError) as exc:
            return fail_contract(str(exc), REASON_REF_CONTRACT_INVALID_NUMERIC)
        if receiver_age_at_snapshot_sec > process_age_at_snapshot_sec:
            return fail_contract(
                "last_frame_age_ms exceeds process_age_ms",
                REASON_REF_CONTRACT_FRAME_AGE_EXCEEDS_PROCESS_AGE,
            )
        receiver_age_sec = receiver_age_at_snapshot_sec + snapshot_age_sec

    if source != configured_source or endpoint != expected_endpoint:
        return (
            CheckResult(
                "AEC bridge output", "fail",
                "bridge reference receiver does not match configured "
                f"outputd UDP input: source={source!r}, endpoint={endpoint!r}, "
                f"expected source='outputd_udp', endpoint={expected_endpoint!r}; "
                f"{localization}",
                reason=REASON_REF_ROUTE_MISMATCH,
            ),
            False,
        )

    if process_age_sec <= _AEC_REFERENCE_INPUT_STARTUP_GRACE_SEC:
        age_detail = (
            "no reference frame received yet"
            if receiver_age_sec is None
            else f"last reference frame age={receiver_age_sec:.2f}s"
        )
        return (
            CheckResult(
                "AEC bridge output", "ok",
                f"reference receiver is within its "
                f"{_AEC_REFERENCE_INPUT_STARTUP_GRACE_SEC:g}s startup grace "
                f"({age_detail}; process age={process_age_sec:.2f}s)",
                reason=REASON_REF_STARTUP_GRACE,
            ),
            True,
        )

    if receiver_age_sec is None:
        return (
            CheckResult(
                "AEC bridge output", "fail",
                "bridge has received zero complete 20 ms reference frames "
                f"after {process_age_sec:.1f}s; outputd UDP reference receiver "
                f"is not making progress. {localization}",
                reason=REASON_REF_ZERO_FRAMES_AFTER_GRACE,
            ),
            False,
        )
    if receiver_age_sec > _AEC_REFERENCE_INPUT_STALE_SEC:
        return (
            CheckResult(
                "AEC bridge output", "fail",
                f"bridge outputd UDP reference receiver is stale: last complete "
                f"20 ms frame age={receiver_age_sec:.2f}s exceeds "
                f"{_AEC_REFERENCE_INPUT_STALE_SEC:g}s; frames_enqueued="
                f"{frames_enqueued}. A carried-forward AEC frame and historical "
                f"RMS cannot prove current receiver progress. {localization}",
                reason=REASON_REF_RECEIVER_STALE,
            ),
            False,
        )
    return (
        CheckResult(
            "AEC bridge output", "ok",
            f"outputd UDP reference receiver current: last complete 20 ms frame "
            f"age={receiver_age_sec:.2f}s, frames_enqueued={frames_enqueued}",
            reason=REASON_REF_RECEIVER_CURRENT,
        ),
        False,
    )

def _assess_aec_bridge_output(
    journal_text: str,
    music_chain_active: bool | None = None,
    *,
    bridge_stats: dict | None = None,
    trusted_reference_identity: tuple[str, str | None] | None = None,
    outputd_status: dict | None = None,
    now: float | None = None,
) -> CheckResult:
    """Pure-function assessment of the bridge's `rms over` log
    output. Split out from `check_aec_bridge_output_health` so the
    parser can be unit-tested without mocking subprocess.

    Counts three quantities across the journal window:
      - silent_ref_count: windows with mic-loud (>threshold) + ref-silent
      - healthy_ref_windows: windows where ref ≥ silent-threshold (any signal)
      - healthy_windows: windows with mic-loud + meaningful attenuation

    `healthy_ref_windows` is the key signal: as long as the ref path
    delivered signal in at least ONE recent window, the reference
    chain demonstrably works. silent_ref windows in that case are
    explained by acoustic sources the speaker never played (room
    voice and ambient noise, pumped by the ASR-beam AGC) and are not
    a bug. Assistant TTS is not one of them: it rides the same
    fan-in → CamillaDSP → outputd path as music, so it reaches the
    reference like any other program audio.

    `music_chain_active` short-circuits the FAIL for pure-voice
    sessions: when no loopback renderer is writing, the reference is
    correctly silent (there is no program audio to reference) so the
    ref-silent + mic-loud pattern proves nothing about the reference
    chain. The gate observes only the snd-aloop renderer lanes — USB
    Audio Input and any ring-armed renderer lane (U3/P6) reach the DAC
    without opening one — so False means "no snd-aloop renderer lane is
    open", NOT "the speaker is silent". Pass False when a check upstream
    has verified the loopback playback side is closed; the FAIL branch
    will then return OK with an explanatory message instead. Default
    None preserves the old behavior (used by tests that want to
    exercise the journal parser in isolation).
    """
    silent_ref_count = 0
    healthy_ref_windows = 0
    healthy_windows = 0
    total_windows = 0
    chip_windows = 0

    for line in journal_text.split("\n"):
        w = _parse_rms_window(line)
        if w is None:
            continue
        total_windows += 1
        if w.chip:
            chip_windows += 1
        # ref ≥ silent-threshold = the reference chain delivered
        # real samples in this window. Any single occurrence proves the
        # chain works end-to-end. Both shapes carry the same `ref`: the
        # outputd speaker monitor, upstream of whichever AEC runs.
        if w.ref >= _AEC_REF_SILENT_THRESHOLD:
            healthy_ref_windows += 1
        # mic > music-threshold = something acoustic was loud enough to
        # plausibly be music (ambient is ~600 RMS, well below). ref <
        # silent-threshold = ref path silent in this window.
        # The threshold carries over to chip AEC because `mic` is `raw0`
        # there, measured at ~1.2-1.8x the cancelled beams
        # (docs/AEC-DIAG-06-xvf-format-level-profile.md:252).
        if w.mic > _AEC_MIC_MUSIC_THRESHOLD and w.ref < _AEC_REF_SILENT_THRESHOLD:
            silent_ref_count += 1
        # "Healthy AEC work" = music-loud mic + meaningful attenuation.
        # Below the music threshold AEC output is just noise floor so we
        # can't tell whether the attenuation number means anything.
        # `level_db is None` excludes chip windows, which carry no
        # attenuation-equivalent to threshold.
        if (
            w.level_db is not None
            and w.mic > _AEC_MIC_MUSIC_THRESHOLD
            and w.level_db <= -8.0
        ):
            healthy_windows += 1

    # Failure mode 1 — ref path broken. Only fail when NO window has ref
    # signal at all; otherwise the silent-ref windows are mic-only
    # artifacts (room voice, ambient noise), a false-positive mode.
    if silent_ref_count >= 5 and healthy_ref_windows == 0:
        # Second false-positive guard: if the music chain isn't
        # currently active (no renderer writing the loopback), a
        # silent ref is expected rather than suspicious. The mic-loud
        # bursts are most likely room voice or ambient noise — though
        # this gate only sees snd-aloop renderer lanes, so it cannot
        # prove the speaker was silent. Either way ref-silent proves
        # nothing about the reference chain here.
        if music_chain_active is False:
            return CheckResult(
                "AEC bridge output", "ok",
                f"{silent_ref_count} mic-loud windows have "
                f"ref<{_AEC_REF_SILENT_THRESHOLD} but loopback playback is "
                f"closed (no snd-aloop renderer lane open) — mic-loud "
                f"bursts are most likely room voice or ambient noise. This "
                f"gate sees only the snd-aloop renderer lanes. If the "
                f"speaker WAS playing — USB Audio Input and any ring-armed "
                f"renderer lane are invisible here — the silent ref is "
                f"unexplained; check outputd's reference publisher. The "
                f"reference itself is outputd's speaker monitor, so program "
                f"audio on ANY transport exercises the ref path; only this "
                f"gate is snd-aloop-scoped.",
                reason=REASON_BRIDGE_OUTPUT_REF_SILENT_NO_MUSIC,
            )
        remediation_text, remediation_reason = _aec_reference_failure_remediation(
            bridge_stats=bridge_stats,
            outputd_status=outputd_status,
            now=time.time() if now is None else now,
            trusted_reference_identity=trusted_reference_identity,
        )
        return CheckResult(
            "AEC bridge output", "fail",
            f"{silent_ref_count} recent windows show mic>{_AEC_MIC_MUSIC_THRESHOLD} "
            f"RMS with ref<{_AEC_REF_SILENT_THRESHOLD} RMS and zero windows show "
            f"ref signal — bridge's reference path is delivering silence "
            f"while the mic captures audio. AEC can't cancel without a "
            f"reference. " + remediation_text,
            reason=remediation_reason,
        )

    # An active bridge writes an RMS window every 5 s, so an empty 90 s
    # window is missing evidence, not evidence of health: a restart loop, a
    # wedged processing thread, or a journal not capturing INFO all look
    # like this. Warn rather than assert an unverified ok.
    if total_windows == 0:
        return CheckResult(
            "AEC bridge output", "warn",
            "no recent RMS windows logged while the bridge is running "
            "(expected one per 5 s) — bridge may have just restarted, or "
            "its processing loop is wedged. Check: journalctl -u "
            "jasper-aec-bridge -e",
            reason=REASON_BRIDGE_OUTPUT_NO_WINDOWS,
        )

    # silent_ref bursts with a healthy ref path are room/ambient noise,
    # not a fault: loud room voice raises mic above the music threshold
    # while the reference (correctly) carries no program audio. Surface
    # the diagnosis so an operator can confirm the path is fine.
    if silent_ref_count >= 5 and healthy_ref_windows > 0:
        return CheckResult(
            "AEC bridge output", "ok",
            f"{silent_ref_count} mic-loud windows have ref<{_AEC_REF_SILENT_THRESHOLD} "
            f"(likely room voice or ambient noise — sound the speaker never "
            f"played is absent from the reference by design); ref path proven "
            f"healthy in {healthy_ref_windows}/{total_windows} windows",
            reason=REASON_BRIDGE_OUTPUT_REF_PROVEN_HEALTHY,
        )

    # Chip AEC: the bridge runs no canceller, so the line carries no
    # attenuation-equivalent to evaluate. Report the evidence that does
    # exist; the ref-path branches above remain the verdict-bearing part on
    # this profile. Only when EVERY window is chip-shaped — a mixed journal
    # (a restart across a profile change) still owes the AEC3 assessment.
    if chip_windows == total_windows:
        return CheckResult(
            "AEC bridge output", "ok",
            f"chip AEC: {total_windows} recent windows; ref carried signal "
            f"in {healthy_ref_windows}/{total_windows} (the chip cancels "
            f"upstream, so this profile logs no attenuation to evaluate)",
            reason=REASON_BRIDGE_OUTPUT_CHIP_ONLY,
        )

    mixed = (
        f"; {chip_windows}/{total_windows} windows are chip-shaped and carry "
        f"no attenuation" if chip_windows else ""
    )

    # All windows quiet — speaker has been idle, nothing to assess.
    if healthy_windows == 0 and silent_ref_count == 0:
        return CheckResult(
            "AEC bridge output", "ok",
            f"no music activity in last 90 s "
            f"({total_windows} log windows; no AEC work to evaluate){mixed}",
            reason=REASON_BRIDGE_OUTPUT_IDLE,
        )

    summary = (
        f"{healthy_windows}/{total_windows} recent windows show real AEC "
        f"work (mic>{_AEC_MIC_MUSIC_THRESHOLD} + attenuation≤-8 dB)"
        f"{mixed}"
    )
    if silent_ref_count:
        # Non-zero silent_ref without hitting the FAIL threshold —
        # surface as diagnostic so partial ref-path glitches are visible
        # before they tip into a sustained outage.
        summary += f"; silent-ref={silent_ref_count} (<5 = below alarm)"
    return CheckResult(
        "AEC bridge output", "ok", summary,
        reason=REASON_BRIDGE_OUTPUT_HEALTHY_WORK,
    )

@doctor_check(order=48, group="aec", exclusive_group="audio-probe")
def check_aec_bridge_output_health() -> CheckResult:
    """Verify the bridge isn't silently producing garbage. The bare
    `is-active` check passes whenever the process is running — but
    the bridge can be running and STILL be in a degraded state: the
    AEC reference path can deliver silence while `systemctl is-active`
    says ok, leaving the wake detector consuming an un-cancelled mic
    with music blasting through it.

    Exact schema-v4 monotonic stats are authoritative for current
    outputd-UDP receiver progress: a freshness failure returns before
    RMS or the USB-blind loopback heuristic can hide it. A freshness
    success proves only transport/queue admission, so journal content
    is still assessed. Missing, older, unknown-future, and unreadable
    schemas retain the bridge's last 90 s of `rms over` lines as a
    rolling-deploy fallback; malformed or stale declared-v4 stats fail
    closed. 90 s rides past the transient that install.sh produces
    during an older-bridge deploy without missing a sustained outage.
    Both assessment paths are pure functions so they can be
    unit-tested without subprocess mocks."""
    parked = _parked_follower_result("AEC bridge output")
    if parked is not None:
        return parked
    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        # Already covered by check_aec_bridge_running; nothing of this
        # check's own domain (RMS windows, reference stats) exists to assess.
        return CheckResult(
            "AEC bridge output", "skipped",
            "(bridge not running — see AEC bridge service check above)",
            reason=REASON_BRIDGE_OUTPUT_BRIDGE_NOT_RUNNING,
        )

    env_source = os.environ.get(
        REF_SOURCE_ENV, "outputd_udp",
    ).strip().lower()
    expected_endpoint = (
        f"{os.environ.get(OUTPUTD_REF_UDP_HOST_ENV, '127.0.0.1').strip()}:"
        f"{os.environ.get(OUTPUTD_REF_UDP_PORT_ENV, '9891').strip()}"
    )
    bridge_stats = _read_bridge_stats_snapshot()
    # EITHER end saying `outputd_udp` enables the authoritative v4 freshness
    # contract (the fail-closed direction): the env states intent, the
    # bridge's own snapshot states what it applied, and the two diverge on a
    # box parked by a pre-P7-1 reconciler (retired `alsa` spelling on disk
    # while the bridge converged). Gating on the env alone would return OK
    # for a dead reference behind a closed music lane. See the
    # grace/past-grace pair in tests/test_doctor_aec.py.
    applied_source = _applied_reference_source(bridge_stats)
    configured_source = (
        "outputd_udp"
        if "outputd_udp" in (applied_source, env_source)
        else env_source
    )
    now_monotonic = time.monotonic()

    stats_assessment: tuple[CheckResult, bool] | None = None
    trusted_reference_identity: tuple[str, str | None] | None = None
    if isinstance(bridge_stats, dict):
        stats_assessment = _assess_aec_reference_input_from_stats(
            bridge_stats,
            now_monotonic,
            configured_source=configured_source,
            expected_endpoint=expected_endpoint,
        )
    if stats_assessment is not None:
        stats_result, startup_grace = stats_assessment
        if stats_result.status == "fail":
            localized = _assess_aec_reference_input_from_stats(
                bridge_stats,
                now_monotonic,
                configured_source=configured_source,
                expected_endpoint=expected_endpoint,
                outputd_status=_read_outputd_status_for_aec_reference(),
            )
            if localized is not None:
                return localized[0]
            return stats_result
        if startup_grace:
            return stats_result
        # A non-startup OK from the exact-v4 assessor means it already proved
        # that reference_input source/endpoint match this outputd route. Carry
        # that identity into journal-content remediation; do not re-rank it
        # through the legacy epoch-based active_capture_plan fallback.
        trusted_reference_identity = ("outputd_udp", expected_endpoint)

    # Rolling-deploy fallback: use a 90-second window, not 5 minutes.
    # Rationale: install.sh restarts an older bridge, and there's a transient
    # (~30-90 s) where the bridge is running but its ref capture
    # hasn't reconnected yet. Within 90 s of deploy completion, that
    # transient looks like the broken state we're trying to catch.
    # Looking at the most recent 90 s only avoids the false-positive
    # while still being long enough to confirm sustained failures.
    proc = _run(
        ["journalctl", "-u", "jasper-aec-bridge.service",
         "--since", "90 sec ago", "--no-pager", "--output", "cat"],
        timeout=8.0,
    )
    if proc.returncode != 0:
        return CheckResult(
            "AEC bridge output", "warn",
            f"could not read journal: {proc.stderr.strip() or 'unknown error'}",
            reason=REASON_BRIDGE_OUTPUT_JOURNAL_UNREADABLE,
        )

    now_epoch = time.time()
    legacy_provenance = (
        _bridge_reference_provenance(bridge_stats, now_epoch)
        if trusted_reference_identity is None
        else None
    )
    music_chain_active = _loopback_playback_active()
    journal_result = _assess_aec_bridge_output(
        proc.stdout,
        music_chain_active=music_chain_active,
        bridge_stats=bridge_stats,
        trusted_reference_identity=trusted_reference_identity,
        now=now_epoch,
    )
    if (
        journal_result.status == "fail"
        and (
            (
                trusted_reference_identity is not None
                and trusted_reference_identity[0] == "outputd_udp"
            )
            or (
                legacy_provenance is not None
                and legacy_provenance[0] == "outputd_udp"
            )
        )
    ):
        journal_result = _assess_aec_bridge_output(
            proc.stdout,
            music_chain_active=music_chain_active,
            bridge_stats=bridge_stats,
            trusted_reference_identity=trusted_reference_identity,
            outputd_status=_read_outputd_status_for_aec_reference(),
            now=now_epoch,
        )
    if stats_assessment is not None:
        journal_result.detail += f"; {stats_assessment[0].detail}"
    return journal_result


def _read_bridge_stats_snapshot() -> dict | None:
    """Read the bridge's one live stats snapshot source."""
    stats_path = Path(os.environ.get(
        BRIDGE_STATS_PATH_ENV,
        "/run/jasper/aec_bridge_stats.json",
    ))
    try:
        stats = json.loads(stats_path.read_text())
    except (OSError, UnicodeError, ValueError, OverflowError):
        return None
    return stats if isinstance(stats, dict) else None


def _applied_reference_source(stats: dict | None) -> str | None:
    """The reference source the running bridge APPLIED, or None if unreadable.

    The bridge resolves ``JASPER_AEC_REF_SOURCE`` before anything reads it
    (``aec_bridge_config.resolved_reference_source``) and publishes the resolved
    value into its stats snapshot, so this is the box's runtime truth where
    the env file is only its intent — and a box parked by a pre-P7-1
    reconciler still carries the retired ``alsa`` spelling in
    /etc/jasper/jasper.env while the bridge converged to ``outputd_udp``.

    Reads the schema-v4 ``reference_input.source``, NOT
    ``active_capture_plan.mic_reference_identity.ref_source``. The two are
    written from the same resolved value, but where they disagree this
    module's shipped ruling is that the v4 receiver block wins and the
    epoch-based plan is the legacy fallback (see the ``trusted_reference_
    identity`` comment in ``check_aec_bridge_output_health``); reading the
    plan here would have inverted that.

    Fail-soft by design — an absent, malformed, or older snapshot returns
    None and the caller falls back to the env value, which is what keeps a
    rolling deploy (or an unwritten /run snapshot) from changing behaviour.
    No freshness gate here: staleness is the assessor's own contract, which
    fails closed on a stale declared-v4 snapshot rather than skipping it.
    """
    if not isinstance(stats, dict):
        return None
    reference_input = stats.get("reference_input")
    if not isinstance(reference_input, dict):
        return None
    source = reference_input.get("source")
    if not isinstance(source, str) or not source.strip():
        return None
    return source


def _bridge_reference_provenance(
    stats: dict | None,
    now: float,
) -> tuple[str, str | None] | None:
    """Return trusted applied reference source/endpoint from fresh stats."""
    if not isinstance(stats, dict):
        return None
    try:
        updated = float(stats["updated_epoch_sec"])
        identity = stats["active_capture_plan"]["mic_reference_identity"]
        source = identity["ref_source"]
    except (KeyError, OverflowError, TypeError, ValueError):
        return None
    age = now - updated
    if not math.isfinite(updated) or age < 0 or age > _BRIDGE_STATS_FRESH_SEC:
        return None
    # outputd's UDP monitor is the bridge's only reference source since
    # U4 / P7-1 retired the `alsa` (`pcm.jasper_ref`) fallback. Anything
    # else is a source doctor cannot name a producer for, so it reports
    # provenance as unavailable rather than guessing a hop.
    if source != "outputd_udp":
        return None
    endpoint = identity.get("outputd_ref_udp")
    if not _valid_udp_endpoint(endpoint):
        return None
    return source, endpoint


def _valid_udp_endpoint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    host, separator, raw_port = value.rpartition(":")
    try:
        port = int(raw_port)
    except ValueError:
        return False
    return bool(separator and host and 0 < port <= 65535)


def _read_outputd_status_for_aec_reference() -> dict | None:
    """Reuse outputd doctor's bounded STATUS reader with fail-soft policy."""
    from .audio_runtime import _outputd_status_payload

    status = _outputd_status_payload()
    return status if isinstance(status, dict) else None


def _aec_reference_failure_remediation(
    *,
    bridge_stats: dict | None,
    outputd_status: dict | None,
    now: float,
    trusted_reference_identity: tuple[str, str | None] | None = None,
) -> tuple[str, str]:
    """Return (advice text, reason) for the silent-reference FAIL row.

    The reason names which hop the localization could prove broken — each
    points at a different corrective action (restart outputd, reconcile both
    ends, or restart the bridge to rebind its receiver), so it is distinct
    from the generic REASON_BRIDGE_OUTPUT_REF_SILENT fallback used when no
    hop can be named at all.
    """
    provenance = trusted_reference_identity or _bridge_reference_provenance(
        bridge_stats, now,
    )
    if provenance is None:
        return (
            "Runtime reference provenance is unavailable, malformed, stale, "
            "or unknown, so doctor cannot safely name the failed hop. Inspect "
            "a fresh /run/jasper/aec_bridge_stats.json to identify ref_source, "
            "then inspect that producer; run `sudo systemctl start "
            "jasper-aec-reconcile` and restart jasper-aec-bridge before "
            "re-running doctor with program audio.",
            REASON_BRIDGE_OUTPUT_REF_SILENT,
        )

    # Report the source the runtime actually published rather than a
    # hard-coded literal: the caller can also hand us a trusted identity,
    # and a remediation that names a source it did not read is the exact
    # mis-attribution this function exists to avoid.
    source, bridge_endpoint = provenance
    references = (
        outputd_status.get("reference_outputs")
        if isinstance(outputd_status, dict)
        else None
    )
    if not isinstance(references, dict):
        return (
            f"Bridge runtime provenance reports source={source} at "
            f"{bridge_endpoint}, but outputd STATUS/reference_outputs is "
            "unavailable, so the publisher endpoint and health cannot be "
            "compared. Run `sudo systemctl start jasper-aec-reconcile`, "
            "restart jasper-outputd and jasper-aec-bridge, and inspect both "
            "service journals if the reference remains silent.",
            REASON_BRIDGE_OUTPUT_REF_SILENT_TARGET_UNKNOWN,
        )

    outputd_target = references.get("udp_target")
    udp_active = references.get("udp_active")
    udp_error_count = references.get("udp_error_count")
    target_text = outputd_target if isinstance(outputd_target, str) else "unknown"
    active_text = str(udp_active) if isinstance(udp_active, bool) else "unknown"
    error_text = (
        str(udp_error_count)
        if isinstance(udp_error_count, int)
        and not isinstance(udp_error_count, bool)
        and udp_error_count >= 0
        else "unknown"
    )
    observed = (
        f"Bridge runtime provenance reports source={source} at "
        f"{bridge_endpoint}; outputd STATUS reports "
        f"reference_outputs.udp_target={target_text!r}, "
        f"udp_active={active_text}, udp_error_count={error_text}. "
    )
    if not _valid_udp_endpoint(outputd_target):
        return observed + (
            "outputd STATUS has no comparable UDP target, so doctor cannot "
            "declare an endpoint match or mismatch. Run `sudo systemctl start "
            "jasper-aec-reconcile`, restart jasper-outputd and "
            "jasper-aec-bridge, and inspect outputd's STATUS/journal if the "
            "target remains unavailable."
        ), REASON_BRIDGE_OUTPUT_REF_SILENT_TARGET_UNKNOWN
    if outputd_target != bridge_endpoint:
        return observed + (
            "The publisher target and bridge receiver do not match. Run "
            "`sudo systemctl start jasper-aec-reconcile`, then restart "
            "jasper-outputd and jasper-aec-bridge so both ends load the same "
            "endpoint."
        ), REASON_BRIDGE_OUTPUT_REF_SILENT_ENDPOINT_MISMATCH
    if udp_active is not True:
        return observed + (
            "The configured endpoint matches, but outputd does not report its "
            "UDP publisher active. Reconcile the reference route and restart "
            "jasper-outputd; restart jasper-aec-bridge too if the receiver "
            "still sees silence."
        ), REASON_BRIDGE_OUTPUT_REF_SILENT_UNCONFIRMED
    return observed + (
        "The endpoint matches and outputd reports the publisher active; a UDP "
        "send does not prove the receiver consumed it. Restart "
        "jasper-aec-bridge to rebind the receiver, then reconcile/restart "
        "jasper-outputd if silence persists. udp_error_count is cumulative; "
        "inspect the outputd journal if it is increasing."
    ), REASON_BRIDGE_OUTPUT_REF_SILENT_UNCONFIRMED


def _assess_dtln_engine_from_stats(
    stats: dict, now: float,
) -> CheckResult | None:
    """Authoritative DTLN-leg verdict from the bridge's live stats
    snapshot (/run/jasper/aec_bridge_stats.json, `leg_engines.dtln`,
    maintained by jasper/cli/aec_bridge.py across initialization and
    runtime failures). Returns None when the snapshot is stale or predates
    the leg_engines field — caller falls back to journal parsing, which is
    window-limited (a failure ages out of the 10-min journal window; this
    surface doesn't)."""
    try:
        updated = float(stats.get("updated_epoch_sec", 0.0))
        leg = stats["leg_engines"]["dtln"]
        enabled = bool(leg["enabled"])
        loaded = bool(leg["loaded"])
        error = leg.get("error")
    except (KeyError, TypeError, ValueError):
        return None
    if now - updated > _BRIDGE_STATS_FRESH_SEC:
        return None
    if enabled and loaded:
        return CheckResult(
            "DTLN-aec engine", "ok",
            "loaded (per bridge stats snapshot; triple-stream tertiary "
            "leg active)",
            reason=REASON_DTLN_LOADED_FROM_STATS,
        )
    if enabled:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but the running bridge reports "
            f"the engine unavailable: {error or 'unknown error'}. Bridge "
            "degraded to AEC3-only — triple-stream is "
            "dual-stream and voice listens on an unfed :9878 leg. Check "
            "/var/lib/jasper/dtln/ and `journalctl -u jasper-aec-bridge -e`.",
            reason=REASON_DTLN_ENGINE_UNAVAILABLE,
        )
    return CheckResult(
        "DTLN-aec engine", "warn",
        "JASPER_AEC_DTLN_ENABLED=1 but the running bridge was started "
        "without the DTLN leg. If the active input profile is chip-AEC "
        "(xvf_chip_aec, or auto resolving to it), the bridge never "
        "loads DTLN — check the profile via `curl -s "
        "localhost:8780/aec` or http://jts.local/wake/. Otherwise the "
        "bridge may not have restarted since the env changed — try: "
        "sudo systemctl restart jasper-aec-bridge",
        reason=REASON_DTLN_NOT_STARTED_WITH_LEG,
    )

def _assess_dtln_engine(journal_text: str) -> CheckResult:
    """Pure-function parser for the bridge's DTLN-aec engine init
    line. Split out from `check_aec_bridge_dtln_engine` so the
    parsing logic is unit-testable without subprocess mocks.

    Successful load line shape (emitted by jasper/cli/aec_bridge.py):
        DTLN-aec engine enabled: size=256, udp out=...
    Failed load line shape:
        JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: <reason>.
        Continuing with AEC3 only.
    """
    # Search newest-first — we want the most recent engine init,
    # not the first one in the window (which may predate a restart).
    for line in reversed(journal_text.splitlines()):
        if "DTLN-aec engine enabled" in line:
            size = "?"
            if "size=" in line:
                size = line.split("size=", 1)[1].split(",", 1)[0].strip()
            return CheckResult(
                "DTLN-aec engine", "ok",
                f"loaded (size={size}, triple-stream tertiary leg active)",
                reason=REASON_DTLN_LOADED_FROM_JOURNAL,
            )
        if "DTLN couldn't load" in line:
            detail = line.split("couldn't load:", 1)[-1].strip()
            return CheckResult(
                "DTLN-aec engine", "fail",
                f"JASPER_AEC_DTLN_ENABLED=1 but engine couldn't load: "
                f"{detail}. Bridge degraded to AEC3-only — triple-stream "
                f"is silently dual-stream. Check /var/lib/jasper/dtln/ "
                f"and `journalctl -u jasper-aec-bridge -e`.",
                reason=REASON_DTLN_LOAD_FAILED,
            )

    # Neither marker found. Either the bridge has been running long
    # enough that the init line aged out (we use a 10-min window) or
    # JASPER_AEC_DTLN_ENABLED was set after the last bridge start.
    return CheckResult(
        "DTLN-aec engine", "warn",
        "JASPER_AEC_DTLN_ENABLED=1 but no engine-init line in last "
        "10 min — bridge may not have restarted since the env var was "
        "set. Try: sudo systemctl restart jasper-aec-bridge",
        reason=REASON_DTLN_NO_INIT_LINE,
    )

@doctor_check(order=54, group="aec")
def check_aec_bridge_dtln_engine() -> CheckResult:
    """Verify the DTLN-aec engine (triple-stream tertiary leg) is
    actually running when `JASPER_AEC_DTLN_ENABLED=1`.

    Without this check, a silent DTLN load failure would degrade
    triple-stream to dual-stream invisibly. The wake_events DB
    would just always have NULL DTLN scores, the analyzer would
    show "DTLN never fires" (correctly — because it never ran),
    and a week of data would lead to the wrong conclusion.

    Skip cleanly when `JASPER_AEC_DTLN_ENABLED` is unset or 0 —
    that's the legacy dual-stream / single-stream path, working
    as intended. Journal parsing is delegated to
    `_assess_dtln_engine` so it can be unit-tested in isolation."""
    parked = _parked_follower_result("DTLN engine")
    if parked is not None:
        return parked
    enabled = os.environ.get(DTLN_ENABLED_ENV, "0").strip().lower()
    if enabled not in ("1", "true", "yes", "on"):
        return CheckResult(
            "DTLN-aec engine", "ok",
            "JASPER_AEC_DTLN_ENABLED not set (dual-stream mode)",
            reason=REASON_DTLN_DISABLED,
        )

    model_result = _check_dtln_model_assets()
    if model_result is not None:
        return model_result

    # Bridge must be running for the engine to mean anything.
    is_active = _run(
        ["systemctl", "is-active", "jasper-aec-bridge.service"]
    ).stdout.strip()
    if is_active != "active":
        return CheckResult(
            "DTLN-aec engine", "skipped",
            "(bridge not running — see AEC bridge service check above)",
            reason=REASON_DTLN_BRIDGE_NOT_RUNNING,
        )

    # Prefer the bridge's live stats snapshot — authoritative and not
    # journal-window-limited (a load failure at a bridge start >10 min
    # ago is invisible to the journal path below).
    stats = _read_bridge_stats_snapshot()
    if stats is not None:
        result = _assess_dtln_engine_from_stats(stats, time.time())
        if result is not None:
            return result

    # 10-minute window covers a recent install.sh deploy + any
    # post-deploy restarts. The engine init line is logged once at
    # bridge startup, so we just need to look back far enough to
    # find the most recent startup.
    proc = _run(
        ["journalctl", "-u", "jasper-aec-bridge.service",
         "--since", "10 min ago", "--no-pager", "--output", "cat"],
        timeout=8.0,
    )
    if proc.returncode != 0:
        return CheckResult(
            "DTLN-aec engine", "warn",
            f"could not read journal: {proc.stderr.strip() or 'unknown error'}",
            reason=REASON_DTLN_JOURNAL_UNREADABLE,
        )

    return _assess_dtln_engine(proc.stdout)

def _check_dtln_model_assets() -> CheckResult | None:
    from jasper.aec_engines import dtln_models

    raw_size = os.environ.get(
        "JASPER_AEC_DTLN_SIZE", str(dtln_models.DEFAULT_SIZE)
    ).strip()
    try:
        model_size = int(raw_size)
    except ValueError:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but JASPER_AEC_DTLN_SIZE is not "
            f"an integer: {raw_size!r}",
            reason=REASON_DTLN_SIZE_NOT_INTEGER,
        )
    model_entry = dtln_models.by_size(model_size)
    if model_entry is None:
        available = ", ".join(str(entry.size) for entry in dtln_models.REGISTRY)
        if not available:
            available = "none"
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but JASPER_AEC_DTLN_SIZE="
            f"{model_size} is not registered in jasper/aec_engines/"
            f"dtln_models.py (available: {available})",
            reason=REASON_DTLN_SIZE_NOT_REGISTERED,
        )
    model_dir = Path(
        os.environ.get("JASPER_DTLN_MODEL_DIR", dtln_models.DTLN_MODELS_DIR)
    )
    missing: list[str] = []
    mismatched: list[str] = []
    for path, _, expected_sha in model_entry.files(model_dir):
        if not path.is_file() or path.stat().st_size <= 0:
            missing.append(path.name)
            continue
        if _sha256_file(path) != expected_sha:
            mismatched.append(path.name)
    if missing:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but model files are missing: "
            f"{', '.join(sorted(missing))} in {model_dir}; re-run deploy/install.sh",
            reason=REASON_DTLN_MODEL_FILES_MISSING,
        )
    if mismatched:
        return CheckResult(
            "DTLN-aec engine", "fail",
            "JASPER_AEC_DTLN_ENABLED=1 but model file hashes do not match "
            "the registry: "
            f"{', '.join(sorted(mismatched))} in {model_dir}; re-run deploy/install.sh",
            reason=REASON_DTLN_MODEL_HASH_MISMATCH,
        )
    return None

@doctor_check(order=55, group="aec")
def check_xvf_firmware_6ch() -> CheckResult:
    """6-ch firmware exposes raw mics on channels 2-5 of the XVF
    capture endpoint. The bridge depends on the 6-channel endpoint
    shape and reads channel 1 (ASR beam); channel 2 is the optional
    raw0 corpus leg."""
    from ...mics import xvf3800
    capture_ch = xvf3800.capture_channels()
    card = xvf3800.alsa_card_name()
    if capture_ch is None:
        return CheckResult("XVF firmware 6-ch", "warn",
                           f"{card} card not present",
                           reason=REASON_XVF_FIRMWARE_CARD_ABSENT)
    target = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    if capture_ch == target:
        return CheckResult("XVF firmware 6-ch", "ok",
                           f"capture is {target}-channel")
    return CheckResult("XVF firmware 6-ch", "warn",
                       f"capture is {capture_ch}-channel — re-flash for "
                       f"software AEC. {_dfu_flash_remedy()}",
                       reason=REASON_XVF_FIRMWARE_WRONG_CHANNELS)

@doctor_check(order=56, group="aec", exclusive_group="audio-probe")
def check_xvf_mixer_state() -> CheckResult:
    """The XVF chip exposes each capture channel as a kernel ALSA
    mixer slot. When the chip is flashed from 2-ch to 6-ch firmware
    mid-bringup, ALSA assigns new slots for ch2-5 with defaults of
    off / 0 dB, and `alsactl restore` persists that across reboot —
    silently killing raw mics in spite of correct chip state. The
    Bash reconciler self-heals via `ensure_capture_mixer_open`; this
    check flags drift if anything sets them back."""
    from ...mics import xvf3800
    if not xvf3800.is_present():
        return CheckResult("XVF mixer state", "warn",
                           f"{xvf3800.alsa_card_name()} card not present",
                           reason=REASON_XVF_MIXER_CARD_ABSENT)
    card = xvf3800.alsa_card_name()
    # Use cget (not get) — these controls aren't part of any aggregated
    # "simple control" group, so `amixer get` misses them.
    sw = _run(["amixer", "-c", card, "cget",
               f"name={xvf3800.MIXER_CAPTURE_SWITCH}"])
    vol = _run(["amixer", "-c", card, "cget",
                f"name={xvf3800.MIXER_CAPTURE_VOLUME}"])
    if sw.returncode != 0 or vol.returncode != 0:
        return CheckResult("XVF mixer state", "warn", "amixer cget failed",
                           reason=REASON_XVF_MIXER_CGET_FAILED)

    def _extract_values(out: str) -> str | None:
        for line in out.split("\n"):
            if ": values=" in line:
                return line.split("values=", 1)[1].strip()
        return None

    switch = _extract_values(sw.stdout) or ""
    volume = _extract_values(vol.stdout) or ""
    switch_norm = switch.replace(" ", "")
    nch = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    expected_sw = ",".join(["on"] * nch)
    try:
        volume_vals = [int(v.strip()) for v in volume.split(",") if v.strip()]
    except ValueError:
        volume_vals = []
    volume_ok = len(volume_vals) >= nch and all(v >= 50 for v in volume_vals[:nch])

    if switch_norm == expected_sw and volume_ok:
        return CheckResult(
            "XVF mixer state", "ok",
            f"all {nch} capture channels open (switch={switch_norm}, vol={volume})",
        )

    issues = []
    if switch_norm != expected_sw:
        issues.append(f"Capture Switch is {switch_norm or '<empty>'} (expected {expected_sw})")
    if not volume_ok:
        issues.append(f"Capture Volume is {volume or '<empty>'} (expected ≥50 on all {nch})")
    return CheckResult(
        "XVF mixer state", "fail",
        " | ".join(issues)
        + ". Heal: sudo /usr/local/sbin/jasper-aec-reconcile --reason heal "
        "(reconciler will reset switch/volume + alsactl store)",
        reason=REASON_XVF_MIXER_DRIFT,
    )
