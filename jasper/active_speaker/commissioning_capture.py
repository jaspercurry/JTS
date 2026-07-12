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
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

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
    noise_floor_dbfs: float | None = None,
    noise_band_report: Sequence[Mapping[str, Any]] | None = None,
    test_level_dbfs: float | None = None,
    excitation: Mapping[str, Any] | None = None,
    placement_proof: Mapping[str, Any] | None = None,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    notes: str | None = None,
    calibration_level: Mapping[str, Any] | None = None,
    safe_session: Mapping[str, Any] | None = None,
    durable_floor_confirmation: Mapping[str, Any] | None = None,
    bundle_ref: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
    capture_geometry: str = "near_field",
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

    ``noise_band_report`` (the correction-shape band-level list — see
    ``jasper.audio_measurement.snr_policy.band_levels_dbfs``) is threaded to
    ``analyze`` so the SC-1 magnitude-class SNR block
    (``DriverAcousticResult.snr``) can be computed; it is independent of the
    pre-existing scalar ``noise_floor_dbfs`` bolt-on below.
    ``capture_geometry`` (``"near_field"`` default, or ``"reference_axis"``)
    passes straight through to ``analyze`` — see
    :func:`jasper.active_speaker.driver_acoustics.analyze_driver_capture`'s
    IR-gating / low-frequency validity-floor behavior.
    ``bundle_ref`` is forwarded verbatim to ``record`` — the durable
    commissioning-bundle join key (``{session_id, artifact_path}`` from
    ``jasper.active_speaker.bundles``) a caller resolved before this call.
    This module does no bundle I/O itself; it only threads the reference
    through to the single measurement write.
    """
    passband = driver_passband_hz(preset, role)
    result = analyze(
        captured_wav,
        sweep_meta,
        passband_hz=passband,
        overlap_fcs=driver_crossover_fcs(preset, role),
        has_mic_calibration=has_mic_calibration,
        calibration=calibration,
        noise_band_report=noise_band_report,
        capture_geometry=capture_geometry,
    )
    acoustic = result.to_dict()
    normalized_noise_floor = _finite_float(noise_floor_dbfs)
    if normalized_noise_floor is not None:
        acoustic["noise_floor_dbfs"] = normalized_noise_floor
        acoustic["signal_over_noise_db"] = (
            float(result.observed_mic_dbfs) - normalized_noise_floor
        )
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
    excitation_ledger = None
    if excitation and (
        sweep_peak_dbfs is None or commissioning_gain_db is None
    ):
        raise ValueError(
            "driver capture excitation has no authoritative played level"
        )
    if sweep_peak_dbfs is not None and commissioning_gain_db is not None:
        supplied_scope = excitation.get("scope") if excitation else None
        locked_main_volume_db = (
            _finite_float(excitation.get("locked_main_volume_db"))
            if excitation else None
        )
        includes_driver_lock = (
            supplied_scope == "sweep_plus_role_gain_and_driver_level_lock"
        )
        effective_peak_dbfs = (
            sweep_peak_dbfs
            + commissioning_gain_db
            + (locked_main_volume_db or 0.0)
        )
        # This is the comparison-critical gain ledger for the isolated-driver
        # capture: the generated sweep peak plus the only role-varying graph
        # gain. Other commissioning gains are common to every driver and cancel.
        # Baseline matching normalizes every capture by this exact ledger, so a
        # per-driver protected level can never masquerade as sensitivity.
        excitation_ledger = {
            "schema_version": 1,
            "scope": supplied_scope or "sweep_plus_role_varying_commission_gain",
            "sweep_peak_dbfs": sweep_peak_dbfs,
            "commissioning_gain_db": commissioning_gain_db,
            "effective_peak_dbfs": effective_peak_dbfs,
        }
        if includes_driver_lock:
            if locked_main_volume_db is None:
                raise ValueError("driver capture excitation has no level lock")
            excitation_ledger["locked_main_volume_db"] = locked_main_volume_db
        if excitation:
            supplied_peak = _finite_float(excitation.get("sweep_peak_dbfs"))
            supplied_gain = _finite_float(excitation.get("commissioning_gain_db"))
            supplied_effective = _finite_float(excitation.get("effective_peak_dbfs"))
            if (
                excitation.get("schema_version") != 1
                or excitation.get("scope") not in {
                    "sweep_plus_role_varying_commission_gain",
                    "sweep_plus_role_gain_and_driver_level_lock",
                }
                or supplied_peak is None
                or supplied_gain is None
                or supplied_effective is None
                or excitation.get("role") not in (None, role)
                or excitation.get("topology_id")
                not in (None, topology.topology_id)
                or abs(supplied_peak - sweep_peak_dbfs) > 1e-6
                or abs(supplied_gain - commissioning_gain_db) > 1e-6
                or abs(
                    supplied_effective
                    - effective_peak_dbfs
                )
                > 1e-6
            ):
                raise ValueError(
                    "driver capture excitation does not match the played sweep"
                )
            for key in ("gain_source", "baseline_id", "topology_id", "role"):
                if excitation.get(key) is not None:
                    excitation_ledger[key] = excitation[key]
    raw = {
        "speaker_group_id": speaker_group_id,
        "role": role,
        "outcome": outcome,
        "observed_mic_dbfs": result.observed_mic_dbfs,
        "mic_clipping": result.mic_clipping,
        "acoustic": acoustic,
        "playback_id": playback_id,
        "test_level_dbfs": test_level_dbfs,
        "excitation": excitation_ledger,
        "placement_proof": dict(placement_proof) if placement_proof else None,
        "notes": notes,
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        safe_session=safe_session,
        durable_floor_confirmation=durable_floor_confirmation,
        bundle_ref=bundle_ref,
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
        "excitation": excitation_ledger,
        "placement_proof": dict(placement_proof) if placement_proof else None,
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
    noise_floor_dbfs: float | None = None,
    noise_band_report: Sequence[Mapping[str, Any]] | None = None,
    excitation: Mapping[str, Any] | None = None,
    placement_proof: Mapping[str, Any] | None = None,
    polarity: str | None = None,
    delay_ms: float | None = None,
    delay_target_role: str | None = None,
    expect_null: bool = False,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    notes: str | None = None,
    calibration_level: Mapping[str, Any] | None = None,
    bundle_ref: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
    capture_geometry: str = "near_field",
    analyze: Callable[..., SummedAcousticResult] = analyze_summed_crossover,
    record: Callable[..., dict[str, Any]] = record_summed_validation,
) -> dict[str, Any]:
    """Analyze a summed-driver sweep capture and record the crossover verdict.

    Runs ``analyze_summed_crossover`` at the group's crossover frequency
    (defaulting to the lowest crossover in the preset), maps the verdict to a
    summed outcome, and persists it through ``record_summed_validation``. An
    ``unusable_capture`` — or a preset with no crossover — records nothing.

    ``noise_band_report`` and ``noise_floor_dbfs`` both feed ``analyze`` (the
    SC-1 alignment-class SNR block on ``SummedAcousticResult.snr`` and the
    null-depth cap); ``noise_floor_dbfs`` is ALSO still bolted onto the
    persisted ``acoustic`` dict's scalar ``noise_floor_dbfs``/
    ``signal_over_noise_db`` keys below, unchanged from before this SNR block
    existed.
    ``unusable_capture`` — or a preset with no crossover — records nothing
    (which includes a reference-axis capture whose crossover Fc or lower
    shoulder sits below the IR-gating validity floor — see
    :func:`jasper.active_speaker.driver_acoustics.analyze_summed_crossover`).

    ``capture_geometry`` (``"near_field"`` default, or ``"reference_axis"``)
    passes straight through to ``analyze``.
    ``bundle_ref`` is forwarded verbatim to ``record`` — see
    :func:`record_driver_acoustic_capture`'s docstring.
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
        noise_band_report=noise_band_report,
        noise_floor_dbfs=noise_floor_dbfs,
        capture_geometry=capture_geometry,
    )
    acoustic = result.to_dict()
    normalized_noise_floor = _finite_float(noise_floor_dbfs)
    if normalized_noise_floor is not None:
        acoustic["noise_floor_dbfs"] = normalized_noise_floor
        acoustic["signal_over_noise_db"] = (
            float(result.observed_mic_dbfs) - normalized_noise_floor
        )
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
    excitation_ledger = None
    if excitation:
        sweep_peak = _finite_float(sweep_meta.get("amplitude_dbfs"))
        declared_peak = _finite_float(excitation.get("sweep_peak_dbfs"))
        corrections = excitation.get("corrections")
        if (
            excitation.get("schema_version") != 1
            or excitation.get("scope")
            != "sweep_plus_applied_full_layer_a_graph"
            or sweep_peak is None
            or declared_peak is None
            or abs(sweep_peak - declared_peak) > 1e-6
            or excitation.get("topology_id")
            not in (None, topology.topology_id)
            or not isinstance(corrections, Mapping)
            or not corrections
        ):
            raise ValueError(
                "summed capture excitation does not match the played graph"
            )
        normalized_corrections: dict[str, dict[str, Any]] = {}
        for role, values in corrections.items():
            gain = (
                _finite_float(values.get("gain_db"))
                if isinstance(values, Mapping)
                else None
            )
            delay = (
                _finite_float(values.get("delay_ms"))
                if isinstance(values, Mapping)
                else None
            )
            effective = (
                _finite_float(values.get("effective_peak_dbfs"))
                if isinstance(values, Mapping)
                else None
            )
            inverted = values.get("inverted") if isinstance(values, Mapping) else None
            if (
                gain is None
                or delay is None
                or effective is None
                or not isinstance(inverted, bool)
                or abs(effective - (sweep_peak + gain)) > 1e-6
            ):
                raise ValueError(
                    "summed capture excitation does not match the played graph"
                )
            normalized_corrections[str(role)] = {
                "gain_db": gain,
                "delay_ms": delay,
                "inverted": inverted,
                "effective_peak_dbfs": effective,
            }
        excitation_ledger = {
            "schema_version": 1,
            "scope": "sweep_plus_applied_full_layer_a_graph",
            "sweep_peak_dbfs": sweep_peak,
            "gain_source": excitation.get("gain_source"),
            "baseline_id": excitation.get("baseline_id"),
            "topology_id": excitation.get("topology_id"),
            "corrections": normalized_corrections,
        }
    raw = {
        "speaker_group_id": speaker_group_id,
        "outcome": outcome,
        "observed_mic_dbfs": result.observed_mic_dbfs,
        "mic_clipping": result.mic_clipping,
        "acoustic": acoustic,
        "summed_test_id": summed_test_id,
        "playback_id": playback_id,
        "excitation": excitation_ledger,
        "placement_proof": dict(placement_proof) if placement_proof else None,
        "polarity": polarity,
        "delay_ms": delay_ms,
        "delay_target_role": delay_target_role,
        "notes": notes,
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        bundle_ref=bundle_ref,
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
        "excitation": excitation_ledger,
        "placement_proof": dict(placement_proof) if placement_proof else None,
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


def _summed_alignment_snr(summed_record: Any) -> tuple[bool | None, bool]:
    """(alignment_snr_ok, null_depth_capped) from a group's latest summed record.

    Reads the SC-1 alignment-class SNR block
    (``acoustic["snr"]``, see
    :func:`jasper.audio_measurement.snr_policy.band_snr_verdicts`) and the
    capped-null flag (``acoustic["null_depth_capped"]``, see
    :func:`jasper.audio_measurement.snr_policy.cap_null_depth_db`) that
    :func:`record_summed_acoustic_capture` persists when noise evidence was
    supplied.

    ``alignment_snr_ok`` is ``None`` (unknown/no evidence — matches
    :func:`~jasper.active_speaker.crossover_alignment.propose_crossover_alignment`'s
    own no-degrade default) when there is no ``snr`` block at all, OR when a
    block IS present but its overall verdict is "unknown" — ``worst_relevant``
    is ``None`` because the only evidence supplied was a scalar noise floor
    (or no relevant band was covered at all). The spec rejects a scalar
    reading as alignment evidence ("Level control and SNR"), so that case is
    indistinguishable from no evidence at all and must not degrade — this
    matters live, not just in theory: today's shipped flow
    (``jasper/web/correction_crossover_flow.py``) bolts a scalar
    ``noise_floor_dbfs`` onto every summed record and never supplies
    ``noise_band_report``, so every real summed capture hits this branch.
    ``alignment_snr_ok`` is ``False`` ONLY when a real per-band reading
    produced a non-"ok" ``worst_relevant`` (i.e. "insufficient" — confirmed
    inadequate overlap-band SNR); ``True`` only on a confident ("ok") overlap
    SNR reading.
    """
    if not isinstance(summed_record, Mapping):
        return None, False
    acoustic = summed_record.get("acoustic")
    if not isinstance(acoustic, Mapping):
        return None, False
    snr = acoustic.get("snr")
    if not isinstance(snr, Mapping):
        alignment_snr_ok = None
    else:
        worst_relevant = snr.get("worst_relevant")
        if not isinstance(worst_relevant, Mapping):
            # Overall 'unknown': scalar-only evidence or no relevant band
            # covered. The spec rejects scalar levels as alignment evidence,
            # so this is indistinguishable from no evidence -> no degrade.
            alignment_snr_ok = None
        else:
            alignment_snr_ok = worst_relevant.get("verdict") == "ok"
    return alignment_snr_ok, bool(acoustic.get("null_depth_capped"))


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
    can never yield a phase decision even if phase_aware is requested. A second,
    independent gate rides the same summed record's SC-1 alignment-class SNR
    block (:func:`_summed_alignment_snr`): a confirmed-insufficient overlap SNR
    or a capped null depth further downgrades keep/invert to review and
    "aligned" to "unknown" inside the proposer, even when phase_aware was
    granted. Never raises on thin/empty state; returns
    ``{status, mode, proposal, ...}``.

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
    alignment_snr_ok, null_depth_capped = _summed_alignment_snr(summed_rec)

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
        alignment_snr_ok=alignment_snr_ok,
        null_depth_capped=null_depth_capped,
    )
    return {
        "status": "ok",
        "speaker_group_id": group,
        "mode": resolved.to_dict(),
        "proposal": proposal.to_dict(),
    }
