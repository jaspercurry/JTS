# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluate runtime readiness for audio-validation reports."""

from __future__ import annotations

import os
import socket
from datetime import datetime
from typing import Any, Mapping

from jasper.audio_resources import audio_validation_artifacts as artifacts
from jasper.runtime_config.audio_profile_state import RuntimeAecEnv
from .chip_aec.policy import (
    APPROVED_DAC_IDS,
    STATUS_APPROVED,
    resolve_chip_aec_dac_gate,
)
from .env_load import parse_env_file
from .service_units import (
    AEC_BRIDGE_SERVICE,
    OUTPUTD_SERVICE,
    JASPER_VOICE_SERVICE,
)
from .install_profile import BUILD_MANIFEST_FILE
from .audio_validation_probes import _env_path


DEFAULT_CHIP_WAKE_LEGS = ("on",)


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
