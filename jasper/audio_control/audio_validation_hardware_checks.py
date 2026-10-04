# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluate passive hardware observations for audio-validation reports."""

from __future__ import annotations

import os
from typing import Any, Mapping

from jasper.audio_resources import audio_validation_artifacts as artifacts
from jasper.platform.service_units import (
    CAMILLA_SERVICE,
    FANIN_SERVICE,
    OUTPUTD_SERVICE,
)
from jasper.audio_control.audio_validation_readiness import (
    _check,
    _readiness_recommendation,
)


CHIP_AEC_PROFILE_READBACK_COMMANDS = (
    "SHF_BYPASS",
    "AUDIO_MGR_SYS_DELAY",
    "AEC_ASROUTONOFF",
    "AEC_FIXEDBEAMSONOFF",
    "AEC_FIXEDBEAMSGATING",
)
CHIP_AEC_CONVERGENCE_COMMAND = "AEC_AECCONVERGED"


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
        "clipped_samples_delta": clipped_delta,
        "last_progress_age_ms": progress_age_ms,
    }
    if (dac_xrun_delta or 0) > 0:
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
