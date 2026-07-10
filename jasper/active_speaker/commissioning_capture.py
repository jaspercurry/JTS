# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bridge: mic-backed acoustic verdict -> commissioning measurement record.

[`driver_acoustics`](driver_acoustics.py)'s ``analyze_driver_capture`` /
``analyze_summed_crossover`` turn a phone-mic sweep capture into a real acoustic
verdict, but they had no caller (the runtime commissioning loop did not exist
yet). This module is that caller: per driver it derives the expected passband
from the compiled preset's crossover regions, runs the acoustic analysis on a
captured sweep WAV, maps the verdict to a
[`measurement`](measurement.py) outcome, and records it through
``record_driver_measurement`` / ``record_summed_validation`` with the *real*
``observed_mic_dbfs`` plus the acoustic verdict block as new evidence on the
same record (the gap ``driver_acoustics``'s module docstring describes).

It does no audio I/O and opens no hardware. The caller (the runtime
commissioning sequencer and its ``/sound/active-speaker/*`` endpoints) plays the
sweep through the active route under the existing safe-playback machinery,
records the phone mic with the shared browser recorder, and hands the captured
WAV path here. ``analyze`` / ``record`` are injected so the wire is hardware-free
unit-testable; the heavy numpy/scipy work stays lazy inside ``driver_acoustics``.

The acoustic verdict supplements, and never relaxes, the measurement-record
safety gates: a ``present`` verdict maps to ``heard_correct_driver``, which
``record_driver_measurement`` still gates on identity verification + the
operator floor confirmation before it counts as ``captured``. An
``unusable_capture`` (clipped / wrong-rate / too-short) records nothing — the
caller re-captures rather than persisting a fabricated result.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from jasper.output_topology import OutputTopology

from .crossover_alignment import (
    PHASE_AWARE,
    propose_crossover_alignment,
    resolve_measurement_mode,
)
from .driver_acoustics import (
    ANALYSIS_HI_HZ,
    ANALYSIS_LO_HZ,
    DEFAULT_NULL_THRESHOLD_DB,
    SUMMED_BLEND_OK,
    SUMMED_POLARITY_OR_DELAY_PROBLEM,
    VERDICT_OUT_OF_BAND,
    VERDICT_PRESENT,
    VERDICT_SILENT,
    DriverAcousticResult,
    SummedAcousticResult,
    analyze_driver_capture,
    analyze_summed_crossover,
)
from .measurement import record_driver_measurement, record_summed_validation
from .profile import ActiveSpeakerPreset, crossover_edges_for_role

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def driver_crossover_fcs(preset: ActiveSpeakerPreset, role: str) -> tuple[float, ...]:
    """The crossover frequencies ``role`` participates in (for overlap matching).

    A role's crossover edges (from :func:`crossover_edges_for_role`) ARE the Fcs
    where it hands off to an adjacent driver: a woofer has one (its upper
    low-pass edge), a tweeter one (its lower high-pass edge), a 3-way mid two.
    These feed ``analyze_driver_capture(overlap_fcs=...)`` so the measured
    overlap-band level can refine the datasheet sensitivity trim.
    """
    lower_edge, upper_edge = crossover_edges_for_role(preset, role)
    fcs: list[float] = []
    for edge in (lower_edge, upper_edge):
        if edge is not None and edge > 0:
            fcs.append(float(edge))
    return tuple(fcs)

# Acoustic verdict -> measurement outcome, keyed on driver_acoustics' verdict
# constants (one source). ``unusable_capture`` has no entry: DRIVER_OUTCOMES /
# SUMMED_OUTCOMES have no "unusable" member, and recording a pass/fail from a
# capture we could not trust would fabricate evidence, so an unusable capture is
# reported back NOT recorded and the caller re-captures.
DRIVER_VERDICT_TO_OUTCOME = {
    VERDICT_PRESENT: "heard_correct_driver",
    VERDICT_OUT_OF_BAND: "heard_wrong_driver",
    VERDICT_SILENT: "silent",
}
SUMMED_VERDICT_TO_OUTCOME = {
    SUMMED_BLEND_OK: "blend_ok",
    SUMMED_POLARITY_OR_DELAY_PROBLEM: "polarity_or_delay_problem",
}


def driver_passband_hz(preset: ActiveSpeakerPreset, role: str) -> tuple[float, float]:
    """The driver's expected acoustic passband for ``analyze_driver_capture``.

    Derived from the role's crossover edges. Open ends (a woofer's low side, a
    tweeter's high side) clamp to the trusted phone-mic analysis window
    ``[ANALYSIS_LO_HZ, ANALYSIS_HI_HZ]`` so the verdict's band comparison stays
    inside the range the deconvolved magnitude is meaningful in.
    """
    lower_edge, upper_edge = crossover_edges_for_role(preset, role)
    lo = float(lower_edge) if lower_edge and lower_edge > 0 else ANALYSIS_LO_HZ
    hi = float(upper_edge) if upper_edge and upper_edge > lo else ANALYSIS_HI_HZ
    if not 0 < lo < hi:
        # A degenerate/empty crossover set: fall back to the full trusted window
        # rather than raising — the verdict just becomes full-range presence.
        return float(ANALYSIS_LO_HZ), float(ANALYSIS_HI_HZ)
    return lo, hi


def primary_crossover_fc_hz(preset: ActiveSpeakerPreset) -> float | None:
    """The lowest crossover frequency for a group's summed-blend check.

    A 2-way has exactly one crossover; this returns it. A 3-way has two, and the
    lowest (woofer/mid) is the default summed-blend target unless the caller
    passes an explicit ``crossover_fc_hz``.
    """
    fcs = [
        float(region.fc_hz)
        for region in preset.crossover_regions
        if region.fc_hz and region.fc_hz > 0
    ]
    return min(fcs) if fcs else None


def record_driver_acoustic_capture(
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    *,
    speaker_group_id: str,
    role: str,
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    playback_id: str | None = None,
    test_level_dbfs: float | None = None,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    notes: str | None = None,
    calibration_level: Mapping[str, Any] | None = None,
    safe_session: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
    analyze: Callable[..., DriverAcousticResult] = analyze_driver_capture,
    record: Callable[..., dict[str, Any]] = record_driver_measurement,
) -> dict[str, Any]:
    """Analyze one driver's sweep capture and record the result.

    Runs ``analyze_driver_capture`` against the role's expected passband, maps
    the verdict to a measurement outcome, and persists it (with the real
    ``observed_mic_dbfs`` and the full acoustic block) through
    ``record_driver_measurement``. An ``unusable_capture`` records nothing.
    Returns ``{verdict, outcome, recorded, skipped_reason, passband_hz,
    acoustic, measurement}``.

    ``playback_id`` must be the **accepted floor test's** playback id and
    ``safe_session`` its armed session: a ``present`` verdict maps to
    ``heard_correct_driver``, which ``record_driver_measurement`` only counts as
    ``captured`` when that floor confirmation matches this target (see
    ``measurement._floor_confirmation_issues``). A missing or mismatched floor
    confirmation still records the acoustic evidence but leaves ``captured``
    False — the acoustic verdict never bypasses the operator floor gate.
    """
    passband = driver_passband_hz(preset, role)
    result = analyze(
        captured_wav,
        sweep_meta,
        passband_hz=passband,
        overlap_fcs=driver_crossover_fcs(preset, role),
        has_mic_calibration=has_mic_calibration,
        calibration=calibration,
    )
    acoustic = result.to_dict()
    outcome = DRIVER_VERDICT_TO_OUTCOME.get(result.verdict)
    if outcome is None:
        return {
            "verdict": result.verdict,
            "outcome": None,
            "recorded": False,
            "skipped_reason": result.verdict,
            "passband_hz": list(passband),
            "acoustic": acoustic,
            "measurement": None,
        }
    sweep_peak_dbfs = _finite_float(sweep_meta.get("amplitude_dbfs"))
    commissioning_gain_db = _finite_float(test_level_dbfs)
    excitation = None
    if sweep_peak_dbfs is not None and commissioning_gain_db is not None:
        # This is the comparison-critical gain ledger for the isolated-driver
        # capture: the generated sweep peak plus the only role-varying graph
        # gain. Other commissioning gains are common to every driver and cancel.
        # Baseline matching normalizes every capture by this exact ledger, so a
        # per-driver protected level can never masquerade as sensitivity.
        excitation = {
            "schema_version": 1,
            "scope": "sweep_plus_role_varying_commission_gain",
            "sweep_peak_dbfs": sweep_peak_dbfs,
            "commissioning_gain_db": commissioning_gain_db,
            "effective_peak_dbfs": sweep_peak_dbfs + commissioning_gain_db,
        }
    raw = {
        "speaker_group_id": speaker_group_id,
        "role": role,
        "outcome": outcome,
        "observed_mic_dbfs": result.observed_mic_dbfs,
        "mic_clipping": result.mic_clipping,
        "acoustic": acoustic,
        "playback_id": playback_id,
        "test_level_dbfs": test_level_dbfs,
        "excitation": excitation,
        "notes": notes,
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        safe_session=safe_session,
        state_path=state_path,
        now=now,
    )
    return {
        "verdict": result.verdict,
        "outcome": outcome,
        "recorded": True,
        "skipped_reason": None,
        "passband_hz": list(passband),
        "acoustic": acoustic,
        "measurement": measurement,
    }


def record_summed_acoustic_capture(
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    *,
    speaker_group_id: str,
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    crossover_fc_hz: float | None = None,
    null_threshold_db: float = DEFAULT_NULL_THRESHOLD_DB,
    summed_test_id: str | None = None,
    playback_id: str | None = None,
    polarity: str | None = None,
    delay_ms: float | None = None,
    delay_target_role: str | None = None,
    expect_null: bool = False,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    notes: str | None = None,
    calibration_level: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
    analyze: Callable[..., SummedAcousticResult] = analyze_summed_crossover,
    record: Callable[..., dict[str, Any]] = record_summed_validation,
) -> dict[str, Any]:
    """Analyze a summed-driver sweep capture and record the crossover verdict.

    Runs ``analyze_summed_crossover`` at the group's crossover frequency
    (defaulting to the lowest crossover in the preset), maps the verdict to a
    summed outcome, and persists it through ``record_summed_validation``. An
    ``unusable_capture`` — or a preset with no crossover — records nothing.
    """
    fc = (
        float(crossover_fc_hz)
        if crossover_fc_hz and crossover_fc_hz > 0
        else primary_crossover_fc_hz(preset)
    )
    if not fc:
        return {
            "verdict": None,
            "outcome": None,
            "recorded": False,
            "skipped_reason": "no_crossover_region",
            "crossover_fc_hz": None,
            "acoustic": None,
            "measurement": None,
        }
    # Both capture kinds judge "is a null present?" against the same threshold; for
    # a reverse-polarity capture (one driver inverted) a present null is the PASS,
    # for an in-phase one it is the PROBLEM. The cap-independent polarity call
    # (reverse-vs-in-phase margin) is the proposal's job, not this per-capture verdict.
    result = analyze(
        captured_wav,
        sweep_meta,
        crossover_fc_hz=fc,
        null_threshold_db=null_threshold_db,
        expect_null=expect_null,
        has_mic_calibration=has_mic_calibration,
        calibration=calibration,
    )
    acoustic = result.to_dict()
    outcome = SUMMED_VERDICT_TO_OUTCOME.get(result.verdict)
    if outcome is None:
        return {
            "verdict": result.verdict,
            "outcome": None,
            "recorded": False,
            "skipped_reason": result.verdict,
            "crossover_fc_hz": fc,
            "acoustic": acoustic,
            "measurement": None,
        }
    raw = {
        "speaker_group_id": speaker_group_id,
        "outcome": outcome,
        "observed_mic_dbfs": result.observed_mic_dbfs,
        "mic_clipping": result.mic_clipping,
        "acoustic": acoustic,
        "summed_test_id": summed_test_id,
        "playback_id": playback_id,
        "polarity": polarity,
        "delay_ms": delay_ms,
        "delay_target_role": delay_target_role,
        "notes": notes,
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        state_path=state_path,
        now=now,
    )
    return {
        "verdict": result.verdict,
        "outcome": outcome,
        "recorded": True,
        "skipped_reason": None,
        "crossover_fc_hz": fc,
        "acoustic": acoustic,
        "measurement": measurement,
    }


def _acoustic_calibrated(record: Any) -> bool | None:
    """Whether this record's acoustic block was captured with a calibrated mic.

    None when there is no acoustic block (no contribution to the phase_aware
    decision). The proposal grants phase_aware only when EVERY contributing record
    is calibrated — an uncalibrated phone capture can never authorize a phase/delay
    decision, even if phase_aware is requested.
    """
    if not isinstance(record, Mapping):
        return None
    acoustic = record.get("acoustic")
    if not isinstance(acoustic, Mapping):
        return None
    return bool(acoustic.get("calibrated"))


def _summed_null_depths(
    summed_record: Any,
) -> tuple[float | None, float | None]:
    """(in_phase_null_db, reverse_null_db) from a group's latest summed record.

    The state keeps one summed record per group, tagged ``expect_null`` (True for a
    reverse-polarity capture). Route its depth to the matching slot; the other is
    None until that capture is taken.
    """
    if not isinstance(summed_record, Mapping):
        return None, None
    acoustic = summed_record.get("acoustic")
    if not isinstance(acoustic, Mapping):
        return None, None
    raw_depth = acoustic.get("null_depth_db")
    if raw_depth is None:
        return None, None
    try:
        depth = float(raw_depth)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(depth):
        return None, None
    if acoustic.get("expect_null"):
        return None, depth
    return depth, None


def build_crossover_alignment_proposal(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    *,
    requested_mode: str = PHASE_AWARE,
    speaker_group_id: str | None = None,
) -> dict[str, Any]:
    """Propose a SAFE crossover polarity refinement (+ delay status) from state.

    A pure read: it walks the recorded summed-crossover null depths (in-phase and,
    if captured, reverse-polarity) for the PRIMARY (lowest) crossover and asks
    :func:`crossover_alignment.propose_crossover_alignment`.

    The phase_aware gate is enforced AT THE DATA: ``requested_mode`` is granted
    only when the contributing summed capture was taken with a calibrated mic
    (``acoustic.calibrated``); otherwise it downgrades to ``magnitude_only`` and
    the proposal is unauthorized (no polarity/delay decision). So a phone capture
    can never yield a phase decision even if phase_aware is requested. Never raises
    on thin/empty state; returns ``{status, mode, proposal, ...}``.

    Scope: ONE crossover (the primary / lowest). A 3-way's upper crossover needs
    its own summed-null capture and is out of scope for this increment. Multi-group
    (stereo-pair) polarity/delay *emission* is also deferred (see
    ``baseline_profile``'s ``group_specific_delay_not_applied``); the proposal
    computes for one group, so a mono/single-group speaker (jts3's
    active_mono_2way) gets the full L2 polarity refinement.
    """
    regions = sorted(
        (r for r in preset.crossover_regions if r.fc_hz and r.fc_hz > 0),
        key=lambda r: r.fc_hz,
    )
    if not regions:
        return {"status": "no_crossover", "proposal": None}
    region = regions[0]
    lower_role = region.lower_driver
    upper_role = region.upper_driver
    fc = float(region.fc_hz)

    latest = measurements.get("latest_by_target")
    if not isinstance(latest, Mapping):
        summary = measurements.get("summary")
        latest = (
            summary.get("latest_driver_measurements")
            if isinstance(summary, Mapping)
            else None
        )
    by_group_role: dict[tuple[str, str], Mapping[str, Any]] = {}
    for rec in (latest.values() if isinstance(latest, Mapping) else []):
        if not isinstance(rec, Mapping):
            continue
        group_id = rec.get("speaker_group_id")
        role = rec.get("role")
        if isinstance(group_id, str) and group_id and isinstance(role, str):
            by_group_role[(group_id, role)] = rec

    latest_summed = measurements.get("latest_summed_by_group")
    summed_by_group = latest_summed if isinstance(latest_summed, Mapping) else {}

    group = speaker_group_id
    if group is None:
        groups = {g for (g, _r) in by_group_role} | set(summed_by_group.keys())
        if len(groups) == 1:
            group = next(iter(groups))
        elif summed_by_group:
            group = sorted(summed_by_group.keys())[0]
        elif groups:
            group = sorted(groups)[0]
    if group is None:
        return {"status": "no_measurements", "proposal": None}

    lower_rec = by_group_role.get((group, lower_role))
    upper_rec = by_group_role.get((group, upper_role))
    summed_rec = summed_by_group.get(group)
    in_phase_null, reverse_null = _summed_null_depths(summed_rec)

    # The phase_aware gate at the data layer: every contributing capture must be
    # calibrated. A single uncalibrated record blocks phase_aware.
    cal_flags = [
        flag
        for flag in (
            _acoustic_calibrated(lower_rec),
            _acoustic_calibrated(upper_rec),
            _acoustic_calibrated(summed_rec),
        )
        if flag is not None
    ]
    data_calibrated = bool(cal_flags) and all(cal_flags)
    resolved = resolve_measurement_mode(
        requested_mode, has_calibrated_mic=data_calibrated
    )

    proposal = propose_crossover_alignment(
        mode=resolved.mode,
        crossover_fc_hz=fc,
        lower_role=lower_role,
        upper_role=upper_role,
        in_phase_null_depth_db=in_phase_null,
        reverse_null_depth_db=reverse_null,
    )
    return {
        "status": "ok",
        "speaker_group_id": group,
        "mode": resolved.to_dict(),
        "proposal": proposal.to_dict(),
    }
