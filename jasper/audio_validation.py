# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Live audio readiness checks, evidence builders, and snapshot CLI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import audio_validation_artifacts as artifacts
from .audio_profile_state import (
    AEC_MODE_ENV,
    AEC_MODE_FILE_ENV,
    DEFAULT_AEC_MODE_PATH,
    MicProbe,
    RuntimeAecEnv,
    build_audio_profile_status,
    intent_from_env,
    normalize_aec_mode,
    probe_xvf_mic as _probe_xvf_mic,
    runtime_env_from_mapping,
)
from .aec.bridge_telemetry import read_bridge_stats
from .audio_hardware.dac import HIFIBERRY_DAC8X_ID
from .chip_aec.policy import (
    APPROVED_DAC_IDS,
    STATUS_APPROVED,
    resolve_chip_aec_dac_gate,
)
from .platform import control_client as control
from .env_load import env_file_path, parse_env_file
from .service_units import (
    AEC_BRIDGE_SERVICE,
    CAMILLA_SERVICE,
    FANIN_SERVICE,
    OUTPUTD_SERVICE,
    JASPER_VOICE_SERVICE,
)
from .install_profile import BUILD_MANIFEST_FILE
from .log_event import log_event
from .systemd_probe import UNKNOWN as UNKNOWN_STATE, unit_states
from .output_hardware import published_dac_id
from .platform.status_socket import (
    OUTPUTD_STATUS_SOCKET,
    read_status_socket_or_none,
)
from .logging_setup import configure_logging


CHIP_AEC_PROFILE = "xvf_chip_aec"
DAC8X_OUTPUTD_STABILITY_PROFILE = "hifiberry_dac8x_outputd_stability"
READINESS_SNAPSHOT_KIND = "readiness_snapshot"
HARDWARE_VALIDATION_KIND = "hardware_validation_passive"
DEFAULT_HARDWARE_OBSERVE_SECONDS = 10.0
DEFAULT_CHIP_WAKE_LEGS = ("on",)
CHIP_AEC_PROFILE_READBACK_COMMANDS = (
    "SHF_BYPASS",
    "AUDIO_MGR_SYS_DELAY",
    "AEC_ASROUTONOFF",
    "AEC_FIXEDBEAMSONOFF",
    "AEC_FIXEDBEAMSGATING",
)
CHIP_AEC_CONVERGENCE_COMMAND = "AEC_AECCONVERGED"

logger = logging.getLogger("jasper.audio_validation")

# Bound on the is-active probe in the validation report.
_SERVICE_PROBE_TIMEOUT_SEC = 2.0


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


def read_mode_env(path: Path | None = None) -> dict[str, str]:
    return parse_env_file(
        str(path or _env_path(AEC_MODE_FILE_ENV, DEFAULT_AEC_MODE_PATH))
    )


def read_system_env(path: Path | None = None) -> dict[str, str]:
    return parse_env_file(str(path) if path else env_file_path())


def _mic_details(mic: MicProbe) -> dict[str, artifacts.JsonValue]:
    return {
        "id": "xvf3800" if mic.xvf_present else "unknown",
        "family": "xvf3800",
        "display_name": mic.display_name,
        "present": mic.xvf_present,
        "capture_channels": mic.capture_channels,
        "recommended_channels": mic.recommended_channels,
        "alsa_card_name": mic.alsa_card_name,
        "variant_id": mic.variant_id,
        "geometry": mic.geometry,
        "chip_beam_plan": mic.chip_beam_plan,
        "chip_aec_supported": mic.chip_aec_supported,
        "probe_error": mic.probe_error,
    }


def outputd_socket_path(system_env: Mapping[str, str]) -> Path:
    raw = (
        system_env.get("JASPER_OUTPUTD_CONTROL_SOCKET")
        or os.environ.get("JASPER_OUTPUTD_CONTROL_SOCKET")
        or OUTPUTD_STATUS_SOCKET
    )
    return Path(raw)


def query_outputd_status(
    socket_path: Path, timeout: float = 1.0
) -> dict[str, Any] | None:
    return read_status_socket_or_none(
        str(socket_path),
        timeout=timeout,
        event="audio_validation.outputd_status_unavailable",
    )


def service_state(unit: str) -> str:
    state = unit_states([unit], timeout=_SERVICE_PROBE_TIMEOUT_SEC)[unit]
    if state == UNKNOWN_STATE:
        log_event(
            logger,
            "audio_validation.service_probe_failed",
            unit=unit,
            level=logging.DEBUG,
        )
    return state


def read_voice_wake_legs(timeout: float = 1.0) -> set[str] | None:
    try:
        data = control.get_state(timeout=timeout)
    except (control.ControlError, ValueError) as e:
        log_event(
            logger,
            "audio_validation.voice_state_unavailable",
            error=str(e),
            level=logging.DEBUG,
        )
        return None
    voice = data.get("voice") if isinstance(data, dict) else None
    if not isinstance(voice, dict):
        return None
    legs = voice.get("wake_legs")
    if not isinstance(legs, list):
        return None
    return {str(leg) for leg in legs}


def _check(
    status: str,
    *,
    summary: str,
    required: bool = True,
    observed: artifacts.JsonValue = None,
    expected: artifacts.JsonValue = None,
) -> dict[str, artifacts.JsonValue]:
    out: dict[str, artifacts.JsonValue] = {
        "status": status,
        "required": required,
        "summary": summary,
    }
    if observed is not None:
        out["observed"] = observed
    if expected is not None:
        out["expected"] = expected
    return out


def _rollup_status(checks: Mapping[str, Mapping[str, Any]]) -> str:
    required = [check for check in checks.values() if check.get("required", True)]
    if any(check.get("status") == "fail" for check in required):
        return "fail"
    if any(check.get("status") != "pass" for check in required):
        return "warn"
    return "pass"


def _readiness_recommendation(checks: Mapping[str, Mapping[str, Any]]) -> str:
    failed = [name for name, check in checks.items() if check.get("status") == "fail"]
    if "mic_detected" in failed:
        return "use_software_aec3_until_xvf_6ch_available"
    if "dac_support" in failed:
        return "calibrate_output_dac_before_chip_aec"
    if "runtime_profile" in failed or "runtime_env" in failed:
        return "run_reconciler_or_select_chip_aec_before_validating"
    if "dac_reference" in failed:
        return "fix_outputd_chip_reference_before_chip_aec"
    if "service_state" in failed:
        return "fix_audio_services_before_chip_aec"
    runtime_unknown = [
        name
        for name, check in checks.items()
        if check.get("required", True) and check.get("status") in {"unknown", "not_run"}
    ]
    if runtime_unknown:
        return "fix_runtime_observability_before_hardware_validation"
    return "run_hardware_validation"


def _dac_details(
    system_env: Mapping[str, str],
    outputd_status: Mapping[str, Any] | None,
) -> dict[str, artifacts.JsonValue]:
    outputd_dac = (
        outputd_status.get("dac") if isinstance(outputd_status, dict) else None
    )
    dac_pcm = ""
    dac_card = ""
    sample_rate: artifacts.JsonValue = None
    if isinstance(outputd_dac, dict):
        dac_pcm = str(outputd_dac.get("pcm") or "")
        dac_card = str(outputd_dac.get("card") or "")
        raw_sample_rate = outputd_dac.get("sample_rate")
        if (
            isinstance(raw_sample_rate, (str, int, float, bool))
            or raw_sample_rate is None
        ):
            sample_rate = raw_sample_rate
    if not dac_pcm:
        dac_pcm = (
            system_env.get("JASPER_OUTPUTD_DAC_PCM")
            or os.environ.get("JASPER_OUTPUTD_DAC_PCM")
            or "outputd_dac"
        )
    if not dac_card:
        dac_card = (
            system_env.get("JASPER_AUDIO_DAC_CARD")
            or os.environ.get("JASPER_AUDIO_DAC_CARD")
            or ""
        )
    # The id is the reconciler's publication and nothing else — no process-env
    # fallback on purpose, unlike outputd's pcm/card/backend facts above.
    dac_id = published_dac_id(system_env)
    return {
        "id": dac_id,
        "pcm": dac_pcm,
        "card": dac_card,
        "backend": str(
            (outputd_status or {}).get("backend")
            or system_env.get("JASPER_OUTPUTD_BACKEND")
            or os.environ.get("JASPER_OUTPUTD_BACKEND")
            or "unknown"
        ),
        "sample_rate": sample_rate,
    }


def current_artifact_filter_kwargs(
    *,
    requested_profile: str | None = None,
    system_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
) -> dict[str, str | None]:
    """Build hardware-bound filters for status-surface artifact reads.

    Always include mic/dac identity, even when detection is unavailable.
    Passing ``unknown`` is intentional: it prevents a previous pass from a
    real mic/DAC from being accepted when the current hardware identity
    cannot be established.
    """

    env = dict(system_env) if system_env is not None else read_system_env()
    mic = _mic_details(mic_probe if mic_probe is not None else _probe_xvf_mic())
    dac = _dac_details(env, None)
    return {
        "requested_profile": requested_profile,
        "mic_id": str(mic.get("id") or "unknown"),
        "dac_id": str(dac.get("id") or "unknown"),
    }


def _runtime_identity_check(
    system_env: Mapping[str, str],
) -> dict[str, artifacts.JsonValue]:
    build_env = parse_env_file(
        str(_env_path("JASPER_BUILD_MANIFEST", BUILD_MANIFEST_FILE)),
    )
    observed = {
        "system_hostname": socket.gethostname(),
        "jasper_hostname": (
            system_env.get("JASPER_HOSTNAME") or os.environ.get("JASPER_HOSTNAME") or ""
        ),
        "build_sha": build_env.get("JASPER_GIT_SHA", ""),
        "build_branch": build_env.get("JASPER_GIT_BRANCH", ""),
        "installed_at": build_env.get("JASPER_INSTALL_AT", ""),
    }
    return _check(
        "pass",
        required=False,
        summary="Pi/runtime identity captured for artifact attribution.",
        observed=observed,
    )


def _dac_identity_check(
    dac: Mapping[str, artifacts.JsonValue],
    *,
    expected_id: str,
) -> dict[str, artifacts.JsonValue]:
    dac_card = str(dac.get("card") or "").strip()
    observed = {
        "id": dac.get("id"),
        "card": dac_card,
        "pcm": dac.get("pcm"),
        "backend": dac.get("backend"),
        "sample_rate": dac.get("sample_rate"),
    }
    expected = {
        "id": expected_id,
        "card": "recognized non-fallback ALSA card",
    }
    card_ok = bool(dac_card) and dac_card != "A"
    if dac.get("id") == expected_id and card_ok:
        return _check(
            "pass",
            summary=f"Expected output DAC identity {expected_id} is active.",
            observed=observed,
            expected=expected,
        )
    if dac.get("id") == expected_id:
        summary = (
            f"Expected output DAC identity {expected_id} is active, "
            "but ALSA card identity is missing or fallback-like."
        )
    else:
        summary = f"This validation profile must run on {expected_id}."
    return _check(
        "fail",
        summary=summary,
        observed=observed,
        expected=expected,
    )


def _chip_aec_dac_support_check(
    dac: Mapping[str, artifacts.JsonValue],
) -> dict[str, artifacts.JsonValue]:
    gate = resolve_chip_aec_dac_gate(dac.get("id"))
    dac_id = gate.dac_id
    observed = {
        "id": dac_id,
        "status": gate.status,
        "source": gate.source,
        # This check asks about PRODUCTION approval, which is what it passes or
        # fails on — an uncodified DAC still arms on an explicit testing
        # request (ADR-0101), and recording THAT here would contradict the
        # verdict beside it.
        "permitted": gate.permits(testing_requested=False),
        "recommended_action": gate.recommended_action,
        "card": dac.get("card"),
        "pcm": dac.get("pcm"),
    }
    expected = {
        "status": STATUS_APPROVED,
        "supported_dac_ids": sorted(APPROVED_DAC_IDS),
    }
    if gate.status == STATUS_APPROVED:
        return _check(
            "pass",
            summary=f"Output DAC {dac_id} is approved for chip-AEC.",
            observed=observed,
            expected=expected,
        )
    return _check("fail", summary=gate.detail, observed=observed, expected=expected)


def _runtime_profile_check(
    profile_status: Mapping[str, Any], profile: str
) -> dict[str, artifacts.JsonValue]:
    audio_profile = profile_status.get("audio_profile") or {}
    observed = {
        "requested": audio_profile.get("requested"),
        "active": audio_profile.get("active"),
        "state": audio_profile.get("state"),
        "reason": audio_profile.get("reason"),
    }
    if (
        audio_profile.get("requested") == profile
        and audio_profile.get("active") == profile
    ):
        return _check(
            "pass", summary="Requested chip-AEC profile is active.", observed=observed
        )
    return _check(
        "fail",
        summary="Chip-AEC profile is not the active runtime profile.",
        observed=observed,
        expected={"requested": profile, "active": profile},
    )


def _runtime_env_check(runtime: Any) -> dict[str, artifacts.JsonValue]:
    observed = {
        "primary_device": getattr(runtime, "primary_device", ""),
        "aec_device": getattr(runtime, "aec_device", ""),
        "chip_enabled": getattr(runtime, "chip_enabled", False),
        "chip_aec_150_device": getattr(runtime, "chip_aec_150_device", ""),
        "chip_aec_210_device": getattr(runtime, "chip_aec_210_device", ""),
        "chip_primary_leg": getattr(runtime, "chip_primary_leg", ""),
    }
    if observed["chip_enabled"] and str(observed["primary_device"]).startswith("udp:"):
        return _check(
            "pass",
            summary="Reconciler-applied chip-AEC env is present.",
            observed=observed,
        )
    return _check(
        "fail",
        summary="Reconciler-applied chip-AEC env is incomplete.",
        observed=observed,
    )


def _service_state_check(
    service_states: Mapping[str, str],
) -> dict[str, artifacts.JsonValue]:
    required_units = (
        OUTPUTD_SERVICE,
        AEC_BRIDGE_SERVICE,
        "jasper-aec-init.service",
        JASPER_VOICE_SERVICE,
    )
    missing = {
        unit: service_states.get(unit, "unknown")
        for unit in required_units
        if service_states.get(unit) != "active"
    }
    if not missing:
        return _check(
            "pass",
            summary="Required chip-AEC services are active.",
            observed=dict(service_states),
        )
    return _check(
        "fail",
        summary="One or more required chip-AEC services are not active.",
        observed=dict(service_states),
        expected={unit: "active" for unit in required_units},
    )


def _outputd_pipeline_service_state_check(
    service_states: Mapping[str, str],
) -> dict[str, artifacts.JsonValue]:
    required_units = (
        OUTPUTD_SERVICE,
        CAMILLA_SERVICE,
        FANIN_SERVICE,
    )
    missing = {
        unit: service_states.get(unit, "unknown")
        for unit in required_units
        if service_states.get(unit) != "active"
    }
    if not missing:
        return _check(
            "pass",
            summary="Required outputd/content-pipeline services are active.",
            observed=dict(service_states),
        )
    return _check(
        "fail",
        summary="One or more outputd/content-pipeline services are not active.",
        observed=dict(service_states),
        expected={unit: "active" for unit in required_units},
    )


def _outputd_dac_status_check(
    outputd_status: Mapping[str, Any] | None,
) -> dict[str, artifacts.JsonValue]:
    if not isinstance(outputd_status, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS was unavailable; DAC state could not be read.",
        )
    dac = outputd_status.get("dac")
    if not isinstance(dac, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS does not expose DAC state.",
        )
    raw_sample_rate = dac.get("sample_rate")
    sample_rate = _as_int(raw_sample_rate)
    observed = {
        "pcm": dac.get("pcm"),
        "sample_rate": sample_rate if sample_rate is not None else raw_sample_rate,
        "period_frames": dac.get("period_frames"),
        "buffer_frames": dac.get("buffer_frames"),
        "frames_written": dac.get("frames_written"),
        "xrun_count": dac.get("xrun_count"),
    }
    if observed["pcm"] and sample_rate == 48000:
        return _check(
            "pass",
            summary="outputd exposes active 48 kHz DAC state.",
            observed=observed,
        )
    return _check(
        "fail",
        summary="outputd DAC state is incomplete or not at the expected rate.",
        observed=observed,
        expected={"pcm": "non-empty", "sample_rate": 48000},
    )


def _dac_reference_check(
    outputd_status: Mapping[str, Any] | None,
) -> dict[str, artifacts.JsonValue]:
    if not isinstance(outputd_status, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS was unavailable; speaker-reference state could not be read.",
        )
    refs = outputd_status.get("reference_outputs")
    if not isinstance(refs, Mapping):
        return _check(
            "unknown",
            summary="outputd STATUS does not expose reference_outputs.",
        )
    observed = {
        "speaker_reference_source": refs.get("speaker_reference_source"),
        "speaker_reference_active": refs.get("speaker_reference_active"),
        "speaker_reference_channels": refs.get("speaker_reference_channels"),
        "chip_ref_pcm": refs.get("chip_ref_pcm"),
        "chip_ref_sample_rate": refs.get("chip_ref_sample_rate"),
        "chip_ref_period_frames": refs.get("chip_ref_period_frames"),
        "chip_ref_buffer_frames": refs.get("chip_ref_buffer_frames"),
        "udp_target": refs.get("udp_target"),
    }
    if (
        observed["speaker_reference_source"] == "outputd_final_electrical"
        and observed["speaker_reference_channels"] == 2
        and observed["chip_ref_pcm"]
        and observed["udp_target"]
        and observed["chip_ref_sample_rate"] == 16000
    ):
        return _check(
            "pass",
            summary="outputd exposes the speaker monitor plus chip PCM reference outputs.",
            observed=observed,
        )
    return _check(
        "fail",
        summary="outputd speaker/chip reference outputs are not fully configured.",
        observed=observed,
        expected={
            "speaker_reference_source": "outputd_final_electrical",
            "speaker_reference_channels": 2,
            "chip_ref_pcm": "non-empty",
            "udp_target": "non-empty",
            "chip_ref_sample_rate": 16000,
        },
    )


def _expected_chip_wake_legs(runtime: RuntimeAecEnv) -> set[str]:
    expected = set(DEFAULT_CHIP_WAKE_LEGS)
    if runtime.chip_aec_150_device:
        expected.add("chip_aec_150")
    if runtime.chip_aec_210_device:
        expected.add("chip_aec_210")
    return expected


def _wake_legs_check(
    runtime: RuntimeAecEnv,
    voice_wake_legs: set[str] | None,
) -> dict[str, artifacts.JsonValue]:
    expected = _expected_chip_wake_legs(runtime)
    if voice_wake_legs is None:
        return _check(
            "unknown",
            summary="jasper-voice wake-leg runtime state was unavailable.",
            expected=sorted(expected),
        )
    missing = expected - voice_wake_legs
    unexpected = voice_wake_legs - expected
    if not missing and not unexpected:
        return _check(
            "pass",
            summary="jasper-voice has armed the expected chip-AEC wake legs.",
            observed=sorted(voice_wake_legs),
            expected=sorted(expected),
        )
    if unexpected:
        return _check(
            "fail",
            summary="jasper-voice has armed unexpected chip-AEC wake legs.",
            observed=sorted(voice_wake_legs),
            expected=sorted(expected),
        )
    return _check(
        "fail",
        summary="jasper-voice has not armed every expected chip-AEC wake leg.",
        observed=sorted(voice_wake_legs),
        expected=sorted(expected),
    )


def _bridge_stats_check(
    stats: Mapping[str, Any] | None, now: datetime
) -> dict[str, artifacts.JsonValue]:
    if not isinstance(stats, Mapping):
        return _check("unknown", summary="AEC bridge stats snapshot is unavailable.")
    counters = stats.get("counters")
    if not isinstance(counters, Mapping):
        return _check("unknown", summary="AEC bridge stats snapshot has no counters.")
    updated = stats.get("updated_epoch_sec")
    age_sec = None
    if isinstance(updated, (int, float)):
        age_sec = max(0.0, now.timestamp() - float(updated))
    queue_drops = counters.get("queue_drops")
    udp_drops = counters.get("udp_send_drops_by_leg")
    ref_starved = int(counters.get("ref_starved_frames", 0) or 0)
    observed = {
        "age_seconds": round(age_sec, 3) if age_sec is not None else None,
        "frames_processed": counters.get("frames_processed"),
        "ref_starved_frames": ref_starved,
        "queue_drops": queue_drops if isinstance(queue_drops, dict) else None,
        "udp_send_drops_by_leg": udp_drops if isinstance(udp_drops, dict) else None,
        "packets_sent_by_leg": counters.get("packets_sent_by_leg"),
    }
    if age_sec is not None and age_sec > 10:
        return _check("warn", summary="AEC bridge stats are stale.", observed=observed)
    drop_total = ref_starved
    for group in (queue_drops, udp_drops):
        if isinstance(group, Mapping):
            drop_total += sum(int(v or 0) for v in group.values())
    if drop_total:
        return _check(
            "warn",
            summary="AEC bridge counters show drops or reference starvation since process start.",
            observed=observed,
        )
    return _check("pass", summary="AEC bridge counters are clean.", observed=observed)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            try:
                return int(float(stripped))
            except (OverflowError, ValueError):
                return None
    return None


def _nested_int(mapping: Mapping[str, Any] | None, *keys: str) -> int | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _as_int(current)


def _nested_mapping(
    mapping: Mapping[str, Any] | None, *keys: str
) -> Mapping[str, Any] | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current if isinstance(current, Mapping) else None


def _counter_delta(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *keys: str,
) -> int | None:
    start = _nested_int(before, *keys)
    end = _nested_int(after, *keys)
    if start is None or end is None:
        return None
    return max(0, end - start)


def _mapping_delta_total(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *keys: str,
) -> int | None:
    start_map = _nested_mapping(before, *keys)
    end_map = _nested_mapping(after, *keys)
    if start_map is None or end_map is None:
        return None
    total = 0
    for key in set(start_map) | set(end_map):
        start = _as_int(start_map.get(key)) or 0
        end = _as_int(end_map.get(key)) or 0
        total += max(0, end - start)
    return total


def profile_runtime_ready(checks: Mapping[str, artifacts.JsonValue]) -> bool:
    required = (
        "runtime_profile",
        "mic_detected",
        "dac_support",
        "runtime_env",
        "service_state",
        "dac_reference",
    )
    for name in required:
        check = checks.get(name)
        if not isinstance(check, Mapping) or check.get("status") != "pass":
            return False
    return True


def _outputd_reference_health_check(
    samples: list[Mapping[str, Any]],
    *,
    duration_seconds: float,
    report_only: bool,
) -> dict[str, artifacts.JsonValue]:
    if report_only:
        return _check(
            "not_run",
            summary="Report-only mode did not observe outputd reference movement.",
            required=True,
            observed={
                "duration_seconds": duration_seconds,
                "sample_count": len(samples),
            },
        )
    if len(samples) < 2:
        return _check(
            "unknown",
            summary="outputd STATUS could not be sampled across the validation window.",
            observed={
                "duration_seconds": duration_seconds,
                "sample_count": len(samples),
            },
        )
    before = samples[0]
    after = samples[-1]
    sequence_delta = _counter_delta(before, after, "mix", "reference_sequence")
    dac_frames_delta = _counter_delta(before, after, "dac", "frames_written")
    dac_xrun_delta = _counter_delta(before, after, "dac", "xrun_count")
    content_xrun_delta = _counter_delta(before, after, "content", "xrun_count")
    clipped_delta = _counter_delta(before, after, "mix", "clipped_samples")
    progress_age_ms = _nested_int(after, "watchdog", "last_progress_age_ms")
    observed = {
        "duration_seconds": round(duration_seconds, 3),
        "sample_count": len(samples),
        "reference_sequence_start": _nested_int(before, "mix", "reference_sequence"),
        "reference_sequence_end": _nested_int(after, "mix", "reference_sequence"),
        "reference_sequence_delta": sequence_delta,
        "dac_frames_written_delta": dac_frames_delta,
        "dac_xrun_delta": dac_xrun_delta,
        "content_xrun_delta": content_xrun_delta,
        "clipped_samples_delta": clipped_delta,
        "last_progress_age_ms": progress_age_ms,
    }
    if (dac_xrun_delta or 0) > 0 or (content_xrun_delta or 0) > 0:
        return _check(
            "fail",
            summary="outputd reported xruns during the validation window.",
            observed=observed,
            expected={"xrun_delta": 0},
        )
    if (clipped_delta or 0) > 0:
        return _check(
            "fail",
            summary="outputd reported clipped samples during the validation window.",
            observed=observed,
            expected={"clipped_samples_delta": 0},
        )
    if sequence_delta is None or sequence_delta <= 0:
        return _check(
            "warn",
            summary="outputd reference sequence did not advance during the validation window.",
            observed=observed,
            expected={"reference_sequence_delta": ">0"},
        )
    if progress_age_ms is not None and progress_age_ms > 2500:
        return _check(
            "warn",
            summary="outputd watchdog progress is stale at the end of the validation window.",
            observed=observed,
            expected={"last_progress_age_ms": "<=2500"},
        )
    return _check(
        "pass",
        summary="outputd reference state advanced without xruns or clipping.",
        observed=observed,
    )


def _bridge_counter_window_check(
    samples: list[Mapping[str, Any]],
    *,
    duration_seconds: float,
    report_only: bool,
) -> dict[str, artifacts.JsonValue]:
    if report_only:
        return _check(
            "not_run",
            summary="Report-only mode did not observe bridge counter movement.",
            observed={
                "duration_seconds": duration_seconds,
                "sample_count": len(samples),
            },
        )
    if len(samples) < 2:
        return _check(
            "unknown",
            summary="AEC bridge stats could not be sampled across the validation window.",
            observed={
                "duration_seconds": duration_seconds,
                "sample_count": len(samples),
            },
        )
    before = samples[0]
    after = samples[-1]
    frames_delta = _counter_delta(before, after, "counters", "frames_processed")
    ref_starved_delta = _counter_delta(before, after, "counters", "ref_starved_frames")
    queue_drop_delta = _mapping_delta_total(before, after, "counters", "queue_drops")
    udp_drop_delta = _mapping_delta_total(
        before, after, "counters", "udp_send_drops_by_leg"
    )
    observed = {
        "duration_seconds": round(duration_seconds, 3),
        "sample_count": len(samples),
        "frames_processed_delta": frames_delta,
        "ref_starved_frames_delta": ref_starved_delta,
        "queue_drop_delta": queue_drop_delta,
        "udp_send_drop_delta": udp_drop_delta,
    }
    drop_delta = (queue_drop_delta or 0) + (udp_drop_delta or 0)
    if drop_delta > 0:
        return _check(
            "fail",
            summary="AEC bridge dropped queued or UDP frames during the validation window.",
            observed=observed,
            expected={"queue_drop_delta": 0, "udp_send_drop_delta": 0},
        )
    if (ref_starved_delta or 0) > 0:
        return _check(
            "warn",
            summary="AEC bridge reused stale reference frames during the validation window.",
            observed=observed,
            expected={"ref_starved_frames_delta": 0},
        )
    if frames_delta is None or frames_delta <= 0:
        return _check(
            "warn",
            summary="AEC bridge did not process mic frames during the validation window.",
            observed=observed,
            expected={"frames_processed_delta": ">0"},
        )
    return _check(
        "pass",
        summary="AEC bridge counters advanced without drops or reference starvation.",
        observed=observed,
    )


def _expected_chip_readback(system_env: Mapping[str, str]) -> dict[str, list[int]]:
    raw_delay = (
        system_env.get("JASPER_AEC_CHIP_SYS_DELAY")
        or os.environ.get("JASPER_AEC_CHIP_SYS_DELAY")
        or "12"
    )
    try:
        sys_delay = int(raw_delay)
    except ValueError:
        sys_delay = 12
    return {
        "SHF_BYPASS": [0],
        "AUDIO_MGR_SYS_DELAY": [sys_delay],
        "AEC_ASROUTONOFF": [1],
        "AEC_FIXEDBEAMSONOFF": [1],
        "AEC_FIXEDBEAMSGATING": [1],
    }


def _normalize_xvf_values(values: Any) -> list[int | float | str]:
    if values is None:
        return []
    if isinstance(values, list):
        raw = values
    elif isinstance(values, tuple):
        raw = list(values)
    else:
        raw = [values]
    out: list[int | float | str] = []
    for value in raw:
        if isinstance(value, bool):
            out.append(int(value))
        elif isinstance(value, (int, float, str)):
            out.append(value)
    return out


def _values_equal(
    expected: list[int | float], observed: list[int | float | str]
) -> bool:
    if len(expected) != len(observed):
        return False
    for want, got in zip(expected, observed, strict=True):
        try:
            if isinstance(want, float):
                if abs(float(got) - want) > 1e-4:
                    return False
            elif int(got) != want:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _chip_profile_readback_check(
    readback: Mapping[str, Any] | None,
    *,
    system_env: Mapping[str, str],
    skipped: bool,
    skip_reason: str = "",
) -> dict[str, artifacts.JsonValue]:
    expected = _expected_chip_readback(system_env)
    if skipped:
        return _check(
            "not_run",
            summary=skip_reason or "Chip readback was skipped.",
            expected=expected,
        )
    if not isinstance(readback, Mapping) or not readback:
        return _check(
            "unknown",
            summary="XVF3800 profile readback was unavailable.",
            expected=expected,
        )
    observed = {
        key: _normalize_xvf_values(readback.get(key))
        for key in CHIP_AEC_PROFILE_READBACK_COMMANDS
    }
    mismatches = {
        key: {"expected": value, "observed": observed.get(key, [])}
        for key, value in expected.items()
        if not _values_equal(value, observed.get(key, []))
    }
    if mismatches:
        return _check(
            "fail",
            summary="XVF3800 chip-AEC profile readback does not match expected volatile settings.",
            observed={"values": observed, "mismatches": mismatches},
            expected=expected,
        )
    return _check(
        "pass",
        summary="XVF3800 chip-AEC volatile profile readback matches expected settings.",
        observed=observed,
        expected=expected,
    )


def _chip_convergence_check(
    polls: list[Mapping[str, Any]],
    *,
    skipped: bool,
    skip_reason: str = "",
) -> dict[str, artifacts.JsonValue]:
    if skipped:
        return _check(
            "not_run",
            summary=skip_reason or "Chip convergence polling was skipped.",
            expected={
                CHIP_AEC_CONVERGENCE_COMMAND: (
                    "read-only poll after runtime/ref health passes"
                ),
            },
        )
    if not polls:
        return _check(
            "unknown",
            summary="XVF3800 convergence polling produced no samples.",
            expected={CHIP_AEC_CONVERGENCE_COMMAND: "0 or 1"},
        )
    values: list[int] = []
    errors: list[str] = []
    for poll in polls:
        if "error" in poll:
            errors.append(str(poll["error"]))
            continue
        value = _normalize_xvf_values(poll.get(CHIP_AEC_CONVERGENCE_COMMAND))
        if value:
            try:
                values.append(int(value[0]))
            except (TypeError, ValueError):
                errors.append(f"invalid value {value[0]!r}")
    observed = {
        "poll_count": len(polls),
        "values": values,
        "errors": errors,
        "converged_count": sum(1 for value in values if value == 1),
    }
    if not values:
        return _check(
            "unknown",
            summary="XVF3800 convergence readback was unavailable.",
            observed=observed,
            expected={CHIP_AEC_CONVERGENCE_COMMAND: "0 or 1"},
        )
    if 1 in values:
        first_converged_index = values.index(1)
        nonconverged_after_first = [
            value for value in values[first_converged_index + 1 :] if value != 1
        ]
        observed["first_converged_sample_index"] = first_converged_index
        observed["nonconverged_after_first_count"] = len(nonconverged_after_first)
        if nonconverged_after_first:
            return _check(
                "warn",
                summary=(
                    "XVF3800 reported AEC convergence but did not remain "
                    "converged for the full validation window."
                ),
                observed=observed,
                expected={
                    CHIP_AEC_CONVERGENCE_COMMAND: "1, with no later 0 once convergence is observed",
                },
            )
        return _check(
            "pass",
            summary="XVF3800 reported stable AEC convergence during the validation window.",
            observed=observed,
            expected={CHIP_AEC_CONVERGENCE_COMMAND: 1},
        )
    return _check(
        "not_observed",
        summary=(
            "XVF3800 did not report AEC convergence during the passive window. "
            "Without an explicit far-end stimulus, this may mean there was "
            "nothing meaningful for the chip to converge on."
        ),
        observed=observed,
        expected={
            CHIP_AEC_CONVERGENCE_COMMAND: "1 when meaningful far-end audio is present",
        },
    )


def _hardware_recommendation(checks: Mapping[str, Mapping[str, Any]]) -> str:
    readiness_names = {
        "runtime_identity",
        "runtime_profile",
        "mic_detected",
        "dac_support",
        "runtime_env",
        "service_state",
        "dac_reference",
        "wake_legs",
        "bridge_counters",
    }
    readiness_checks = {
        name: check for name, check in checks.items() if name in readiness_names
    }
    readiness = _readiness_recommendation(readiness_checks)
    if readiness != "run_hardware_validation":
        return readiness
    if checks.get("outputd_reference_health", {}).get("status") == "fail":
        return "fix_outputd_reference_health_before_chip_validation"
    if checks.get("bridge_counter_window", {}).get("status") == "fail":
        return "fix_aec_bridge_stability_before_chip_validation"
    if checks.get("chip_profile_readback", {}).get("status") == "fail":
        return "rerun_aec_init_or_reconciler_before_chip_validation"
    if checks.get("outputd_reference_health", {}).get("status") in {
        "unknown",
        "not_run",
        "warn",
    }:
        return "review_outputd_reference_health_before_chip_validation"
    if checks.get("bridge_counter_window", {}).get("status") in {
        "unknown",
        "not_run",
        "warn",
    }:
        return "review_aec_bridge_reference_stability"
    return "run_drift_delay_validation"


def _outputd_stability_recommendation(
    status: str,
    checks: Mapping[str, Mapping[str, Any]],
) -> str:
    if checks.get("service_state", {}).get("status") == "fail":
        return "fix_outputd_pipeline_services_before_validation"
    if checks.get("dac_identity", {}).get("status") == "fail":
        return "run_on_hifiberry_dac8x_target_before_validation"
    if checks.get("dac_output", {}).get("status") in {"fail", "unknown", "not_run"}:
        return "fix_outputd_runtime_observability_before_validation"
    if checks.get("outputd_reference_health", {}).get("status") == "fail":
        return "fix_outputd_stability_before_dac_validation"
    if checks.get("outputd_reference_health", {}).get("status") in {
        "unknown",
        "not_run",
        "warn",
    }:
        return "review_outputd_reference_health_before_dac_validation"
    if status == "pass":
        return "outputd_dac_stability_validated"
    return "review_audio_validation_warnings"


def build_chip_aec_readiness_artifact(
    *,
    now: datetime | None = None,
    profile: str = CHIP_AEC_PROFILE,
    system_env: Mapping[str, str] | None = None,
    mode_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    bridge_stats: Mapping[str, Any] | None = None,
    voice_wake_legs: set[str] | None = None,
) -> artifacts.ValidationArtifact:
    """Build a bounded schema-v1 chip-AEC readiness snapshot.

    This producer reads runtime state that is already exposed by JTS. It does
    not play audio, capture audio, mutate audio daemons, or persist XVF chip
    settings, so it can only produce readiness evidence. Full validation stays
    a separate operator-controlled hardware run.
    """

    now = datetime.now(timezone.utc) if now is None else now
    mode_env = dict(mode_env) if mode_env is not None else read_mode_env()
    system_env = dict(system_env) if system_env is not None else read_system_env()
    mic_probe = mic_probe or _probe_xvf_mic()
    service_states = (
        dict(service_states)
        if service_states is not None
        else {
            unit: service_state(unit)
            for unit in (
                OUTPUTD_SERVICE,
                AEC_BRIDGE_SERVICE,
                "jasper-aec-init.service",
                JASPER_VOICE_SERVICE,
            )
        }
    )
    if outputd_status is None:
        outputd_status = query_outputd_status(outputd_socket_path(system_env))
    if bridge_stats is None:
        bridge_stats = read_bridge_stats()
    if voice_wake_legs is None:
        voice_wake_legs = read_voice_wake_legs()

    intent = replace(
        intent_from_env(mode_env),
        mode=normalize_aec_mode(mode_env.get(AEC_MODE_ENV, "")),
        profile_selection=mode_env.get("JASPER_AUDIO_INPUT_PROFILE", ""),
    )
    runtime = runtime_env_from_mapping(system_env, process_env=os.environ)
    chip_available = mic_probe.chip_aec_supported
    dac = _dac_details(system_env, outputd_status)
    chip_gate = resolve_chip_aec_dac_gate(dac.get("id"), outputd_status=outputd_status)
    profile_status = build_audio_profile_status(
        intent,
        runtime,
        mic_probe,
        bridge_active=service_states.get(AEC_BRIDGE_SERVICE) == "active",
        chip_available=chip_available,
        chip_gate=chip_gate.to_dict(),
    )
    mic = _mic_details(mic_probe)
    checks = {
        "runtime_identity": _runtime_identity_check(system_env),
        "runtime_profile": _runtime_profile_check(profile_status, profile),
        "mic_detected": _check(
            "pass" if chip_available else "fail",
            summary=(
                "XVF3800 mic profile has a validated chip beam plan."
                if chip_available
                else (
                    "Chip-AEC requires a validated XVF3800 chip beam plan "
                    "for the detected mic geometry."
                )
            ),
            observed=mic,
            expected={
                "family": "xvf3800",
                "chip_beam_plan": "validated",
            },
        ),
        "dac_support": _chip_aec_dac_support_check(dac),
        "runtime_env": _runtime_env_check(runtime),
        "service_state": _service_state_check(service_states),
        "dac_reference": _dac_reference_check(outputd_status),
        "wake_legs": _wake_legs_check(runtime, voice_wake_legs),
        "bridge_counters": _bridge_stats_check(bridge_stats, now),
    }
    status = _rollup_status(checks)
    return artifacts.make_artifact(
        validated_at=now,
        mic_id=str(mic["id"] or "unknown"),
        dac_id=str(dac["id"] or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_readiness_recommendation(checks),
        notes=(
            f"{READINESS_SNAPSHOT_KIND}: runtime readiness only",
            "No playback stimulus was generated.",
            "No capture loop was opened.",
            "No XVF chip settings were written or persisted.",
            "Long-window drift and fixed-delay stability require hardware validation.",
        ),
    )


def build_outputd_stability_hardware_validation_artifact(
    *,
    now: datetime | None = None,
    profile: str = DAC8X_OUTPUTD_STABILITY_PROFILE,
    system_env: Mapping[str, str] | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    outputd_status_samples: list[Mapping[str, Any]] | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    report_only: bool = False,
    forced: bool = False,
) -> artifacts.ValidationArtifact:
    """Build a measured outputd/DAC stability artifact.

    This profile intentionally excludes chip-AEC and voice prerequisites so
    DAC/content-loop stability can be validated while chip-AEC is disabled or
    the voice daemon is parked for first-time provider setup.
    """

    now = datetime.now(timezone.utc) if now is None else now
    system_env = dict(system_env) if system_env is not None else read_system_env()
    service_states = (
        dict(service_states)
        if service_states is not None
        else {
            unit: service_state(unit)
            for unit in (
                OUTPUTD_SERVICE,
                CAMILLA_SERVICE,
                FANIN_SERVICE,
            )
        }
    )
    outputd_status_samples = list(outputd_status_samples or [])
    if outputd_status is None and outputd_status_samples:
        outputd_status = outputd_status_samples[0]
    if outputd_status is None:
        outputd_status = query_outputd_status(outputd_socket_path(system_env))

    dac = _dac_details(system_env, outputd_status)
    checks: dict[str, Mapping[str, Any]] = {
        "runtime_identity": _runtime_identity_check(system_env),
        "service_state": _outputd_pipeline_service_state_check(service_states),
        "dac_identity": _dac_identity_check(dac, expected_id=HIFIBERRY_DAC8X_ID),
        "dac_output": _outputd_dac_status_check(outputd_status),
        "outputd_reference_health": _outputd_reference_health_check(
            outputd_status_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
        ),
        "operator_control": _check(
            "pass",
            required=False,
            summary="Validation was explicitly operator-invoked and bounded.",
            observed={
                "duration_seconds": round(duration_seconds, 3),
                "report_only": report_only,
                "forced": forced,
                "playback_generated": False,
                "capture_loop_opened": False,
                "xvf_reads": False,
                "xvf_persistent_writes": False,
            },
        ),
    }
    status = _rollup_status(checks)
    errors: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "fail":
            errors.append(f"{name}: {check.get('summary', 'failed')}")
    notes = [
        f"{HARDWARE_VALIDATION_KIND}: passive outputd/DAC stability evidence",
        "No playback stimulus was generated.",
        "No capture loop was opened.",
        (
            "Chip-AEC, AEC bridge, XVF readback, and jasper-voice state are "
            "not prerequisites for this profile."
        ),
    ]
    return artifacts.make_artifact(
        validated_at=now,
        mic_id="not_applicable",
        dac_id=str(dac["id"] or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_outputd_stability_recommendation(status, checks),
        notes=tuple(notes),
        errors=tuple(errors),
    )


def build_chip_aec_hardware_validation_artifact(
    *,
    now: datetime | None = None,
    profile: str = CHIP_AEC_PROFILE,
    system_env: Mapping[str, str] | None = None,
    mode_env: Mapping[str, str] | None = None,
    mic_probe: MicProbe | None = None,
    service_states: Mapping[str, str] | None = None,
    outputd_status: Mapping[str, Any] | None = None,
    bridge_stats: Mapping[str, Any] | None = None,
    voice_wake_legs: set[str] | None = None,
    outputd_status_samples: list[Mapping[str, Any]] | None = None,
    bridge_stats_samples: list[Mapping[str, Any]] | None = None,
    chip_readback: Mapping[str, Any] | None = None,
    chip_convergence_polls: list[Mapping[str, Any]] | None = None,
    duration_seconds: float = DEFAULT_HARDWARE_OBSERVE_SECONDS,
    report_only: bool = False,
    forced: bool = False,
    chip_probe_skipped: bool = False,
    chip_probe_skip_reason: str = "",
) -> artifacts.ValidationArtifact:
    """Build a schema-v1 measured chip-AEC validation artifact.

    The default hardware runner is passive: it samples already-running
    outputd/bridge state and read-only XVF parameters. It never generates
    speaker output, opens capture streams, or writes/persists chip settings.
    """

    if profile == DAC8X_OUTPUTD_STABILITY_PROFILE:
        return build_outputd_stability_hardware_validation_artifact(
            now=now,
            profile=profile,
            system_env=system_env,
            service_states=service_states,
            outputd_status=outputd_status,
            outputd_status_samples=outputd_status_samples,
            duration_seconds=duration_seconds,
            report_only=report_only,
            forced=forced,
        )

    now = datetime.now(timezone.utc) if now is None else now
    mode_env = dict(mode_env) if mode_env is not None else read_mode_env()
    system_env = dict(system_env) if system_env is not None else read_system_env()
    outputd_status_samples = list(outputd_status_samples or [])
    bridge_stats_samples = list(bridge_stats_samples or [])
    if outputd_status is None and outputd_status_samples:
        outputd_status = outputd_status_samples[0]
    if bridge_stats is None and bridge_stats_samples:
        bridge_stats = bridge_stats_samples[0]

    readiness = build_chip_aec_readiness_artifact(
        now=now,
        profile=profile,
        system_env=system_env,
        mode_env=mode_env,
        mic_probe=mic_probe,
        service_states=service_states,
        outputd_status=outputd_status,
        bridge_stats=bridge_stats,
        voice_wake_legs=voice_wake_legs,
    )
    checks: dict[str, Mapping[str, Any]] = {
        key: value
        for key, value in readiness.checks.items()
        if isinstance(value, Mapping)
    }
    outputd_health = _outputd_reference_health_check(
        outputd_status_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
    )
    bridge_window = _bridge_counter_window_check(
        bridge_stats_samples,
        duration_seconds=duration_seconds,
        report_only=report_only,
    )
    checks["outputd_reference_health"] = outputd_health
    checks["bridge_counter_window"] = bridge_window
    skip_chip = chip_probe_skipped or not (
        profile_runtime_ready(checks) and outputd_health.get("status") == "pass"
    )
    skip_reason = chip_probe_skip_reason
    if skip_chip and not skip_reason:
        skip_reason = (
            "Chip readback/convergence polling waits for passing runtime "
            "and outputd reference health."
        )
    checks["chip_profile_readback"] = _chip_profile_readback_check(
        chip_readback,
        system_env=system_env,
        skipped=skip_chip,
        skip_reason=skip_reason,
    )
    checks["chip_convergence"] = _chip_convergence_check(
        list(chip_convergence_polls or []),
        skipped=skip_chip,
        skip_reason=skip_reason,
    )
    checks["operator_control"] = _check(
        "pass",
        required=False,
        summary="Validation was explicitly operator-invoked and bounded.",
        observed={
            "duration_seconds": round(duration_seconds, 3),
            "report_only": report_only,
            "forced": forced,
            "playback_generated": False,
            "capture_loop_opened": False,
            "xvf_persistent_writes": False,
        },
    )
    status = _rollup_status(checks)
    dac = _dac_details(system_env, outputd_status)
    notes = (
        f"{HARDWARE_VALIDATION_KIND}: passive operator-controlled hardware evidence",
        "No playback stimulus was generated.",
        "No capture loop was opened.",
        "Only read-only XVF parameters were polled.",
        "No XVF chip settings were written or persisted.",
        (
            "Fixed delay and long-window drift still require an explicit "
            "playback/capture validation mode."
        ),
    )
    errors: list[str] = []
    for name, check in checks.items():
        if check.get("status") == "fail":
            errors.append(f"{name}: {check.get('summary', 'failed')}")
    return artifacts.make_artifact(
        validated_at=now,
        mic_id=readiness.mic_id,
        dac_id=str(dac["id"] or readiness.dac_id or "unknown"),
        profile=profile,
        status=status,
        checks=checks,
        recommendation=_hardware_recommendation(checks),
        notes=notes,
        errors=tuple(errors),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a bounded audio readiness snapshot artifact.",
    )
    parser.add_argument(
        "--profile",
        default=CHIP_AEC_PROFILE,
        choices=(CHIP_AEC_PROFILE,),
        help="Audio profile to snapshot.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Artifact directory (default: /var/lib/jasper/audio-validation).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the full artifact JSON to stdout.",
    )
    args = parser.parse_args(argv)

    configure_logging(fmt="%(message)s")
    artifact = build_chip_aec_readiness_artifact(profile=args.profile)
    directory = args.directory or artifacts.artifact_directory()
    try:
        path = artifacts.write_artifact(artifact, directory=directory)
        latest_path = artifacts.write_latest_pointer(artifact, directory=directory)
    except OSError as e:
        log_event(
            logger,
            "audio_validation.write_failed",
            profile=artifact.profile,
            status=artifact.status,
            error=str(e),
            level=logging.ERROR,
        )
        return 1
    log_event(
        logger,
        "audio_validation.snapshot",
        profile=artifact.profile,
        status=artifact.status,
        recommendation=artifact.recommendation,
        path=path,
        latest=latest_path,
    )
    if args.stdout:
        json.dump(artifact.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if artifact.status != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
