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

import logging
import math
import statistics
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from ._common import REGION_FC_MATCH_TOLERANCE_HZ, region_key as _region_key
from .crossover_alignment import (
    PHASE_AWARE,
    ResolvedMode,
    propose_crossover_alignment,
    resolve_measurement_mode,
)
from .crossover_contract import summed_decision_evidence_state
from .driver_acoustics import (
    ANALYSIS_HI_HZ,
    ANALYSIS_LO_HZ,
    DEFAULT_NULL_THRESHOLD_DB,
    SUMMED_BLEND_OK,
    SUMMED_POLARITY_OR_DELAY_PROBLEM,
    VERDICT_OUT_OF_BAND,
    VERDICT_PRESENT,
    VERDICT_SILENT,
    VERDICT_UNUSABLE_CAPTURE,
    DriverAcousticResult,
    SummedAcousticResult,
    analyze_driver_capture,
    analyze_summed_crossover,
)
from .measurement import record_driver_measurement, record_summed_validation
from .profile import ActiveSpeakerPreset, crossover_edges_for_role

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

logger = logging.getLogger(__name__)

# Crossover lifecycle events reserved for later slices/phases -- declared here
# (docs/active-crossover-information-design.md "Structured events") so a grep
# for the event name finds a documented reason it is silent, not a missing
# call site. No code path in this lane emits any of these; a unit test in
# tests/test_active_speaker_commissioning_capture.py pins that.
RESERVED_CROSSOVER_EVENTS = (
    # Slice 3 (measured candidate selection) produces the proposal this fires
    # for; Slice 1 wires only the lifecycle events themselves.
    "correction.crossover_proposal_ready",
    # Phase 2 post-apply acoustic verification; not built in Phase 1.
    "correction.crossover_verification_passed",
    "correction.crossover_verification_failed",
    # Level locking already ships under correction.crossover_driver_level_*
    # names in jasper/web/correction_crossover_backend.py. Renaming a shipped
    # event onto this namespace is a deliberate future migration, not
    # something this lane does silently.
    "correction.crossover_level_locked",
    "correction.crossover_level_failed",
)


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


# Tolerance for matching an analyzed fc back to the preset region it came
# from. The fc always originates FROM a region's own fc_hz (either
# `primary_crossover_fc_hz`'s default or an explicit caller value that
# should match one), so an exact-ish float comparison is enough; this only
# guards against float round-trip noise, not a real mismatch.
def region_for_fc(
    preset: ActiveSpeakerPreset, fc_hz: float,
) -> dict[str, Any] | None:
    """The preset crossover region whose ``fc_hz`` matches ``fc_hz``, or None.

    Stamped onto a summed capture at record time (see
    ``record_summed_acoustic_capture``) so paired in-phase/reverse evidence
    can be grouped per region downstream (``measurement.
    record_summed_validation`` / ``_latest_current_summed_records``). A
    caller-supplied ``crossover_fc_hz`` that does not match any region's
    ``fc_hz`` resolves to ``None`` — an honest "couldn't identify the
    region" rather than a guess.
    """
    for region in preset.crossover_regions:
        if (
            region.fc_hz
            and abs(float(region.fc_hz) - fc_hz) < REGION_FC_MATCH_TOLERANCE_HZ
        ):
            return {
                "lower_role": region.lower_driver,
                "upper_role": region.upper_driver,
                "fc_hz": float(region.fc_hz),
            }
    return None


def _bundle_session_id(measurement: Any) -> str | None:
    """Best-effort bundle session id from a just-recorded measurement.

    ``record_driver_measurement`` returns the whole persisted state. The
    durable session is owned by its active comparison set; a direct key remains
    a legacy fallback for older/fabricated callers.
    """
    if not isinstance(measurement, Mapping):
        return None
    comparison_set = measurement.get("active_comparison_set")
    session_id = (
        comparison_set.get("bundle_session_id")
        if isinstance(comparison_set, Mapping)
        else None
    ) or measurement.get("bundle_session_id")
    return str(session_id) if session_id else None


def _log_capture_verdict_event(
    result: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str | None = None,
) -> None:
    """Emit ``correction.crossover_capture_accepted``/``_rejected`` once.

    ``record_driver_acoustic_capture`` and ``record_summed_acoustic_capture``
    are "the shared chokepoint both the relay flow and web_measurement call"
    (docs/active-crossover-information-design.md "Structured events"), so
    every one of their return points funnels through here exactly once.
    """
    acoustic = result.get("acoustic")
    acoustic = acoustic if isinstance(acoustic, Mapping) else {}
    fields: dict[str, Any] = {}
    session = _bundle_session_id(result.get("measurement"))
    if session:
        fields["session"] = session
    fields["group"] = speaker_group_id
    if role:
        fields["role"] = role
    capture_geometry = acoustic.get("capture_geometry")
    if capture_geometry in {"near_field", "reference_axis"}:
        fields["capture_geometry"] = capture_geometry
    if capture_geometry == "reference_axis":
        gating = acoustic.get("gating")
        floor_hz = (
            _finite_float(gating.get("f_valid_floor_hz"))
            if isinstance(gating, Mapping)
            else None
        )
        fields["validity_floor_status"] = (
            "known"
            if (
                isinstance(gating, Mapping)
                and gating.get("applied") is True
                and floor_hz is not None
                and floor_hz > 0
            )
            else "unknown"
        )
    verdict = result.get("verdict")
    if verdict is not None:
        fields["verdict"] = verdict
    if result.get("recorded"):
        fields["outcome"] = result.get("outcome")
        snr_block = acoustic.get("snr")
        worst_relevant = (
            snr_block.get("worst_relevant") if isinstance(snr_block, Mapping) else None
        )
        snr_db = (
            _finite_float(worst_relevant.get("estimated_snr_db"))
            if isinstance(worst_relevant, Mapping)
            else None
        )
        if snr_db is not None:
            fields["snr_db"] = snr_db
        gating_block = acoustic.get("gating")
        floor_hz = (
            _finite_float(gating_block.get("f_valid_floor_hz"))
            if isinstance(gating_block, Mapping)
            else None
        )
        if floor_hz is not None:
            fields["floor_hz"] = floor_hz
        log_event(logger, "correction.crossover_capture_accepted", **fields)
    else:
        reason = result.get("skipped_reason")
        if reason is not None:
            fields["reason"] = reason
        log_event(logger, "correction.crossover_capture_rejected", **fields)


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
    noise_band_report: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    ambient_report: Mapping[str, Any] | None = None,
    ambient_duration_s: float | None = None,
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
    repeats: Mapping[str, Any] | None = None,
    emit_lifecycle_event: bool = True,
    analyze: Callable[..., DriverAcousticResult] = analyze_driver_capture,
    record: Callable[..., dict[str, Any] | None] = record_driver_measurement,
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

    ``noise_band_report`` (the legacy correction-shape band-level list or the
    domain-tagged stored-ambient report) is threaded to
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
    analyze_kwargs: dict[str, Any] = {
        "passband_hz": passband,
        "overlap_fcs": driver_crossover_fcs(preset, role),
        "has_mic_calibration": has_mic_calibration,
        "calibration": calibration,
        "noise_band_report": noise_band_report,
        "capture_geometry": capture_geometry,
    }
    if ambient_duration_s is not None:
        analyze_kwargs["ambient_duration_s"] = ambient_duration_s
    result = analyze(captured_wav, sweep_meta, **analyze_kwargs)
    acoustic = result.to_dict()
    if isinstance(ambient_report, Mapping) and not isinstance(
        acoustic.get("ambient"), Mapping
    ):
        acoustic["ambient"] = dict(ambient_report)
    normalized_noise_floor = _finite_float(noise_floor_dbfs)
    if normalized_noise_floor is not None:
        acoustic["noise_floor_dbfs"] = normalized_noise_floor
        acoustic["signal_over_noise_db"] = (
            float(result.observed_mic_dbfs) - normalized_noise_floor
        )
    outcome = DRIVER_VERDICT_TO_OUTCOME.get(result.verdict)
    if outcome is None:
        rejected: dict[str, Any] = {
            "verdict": result.verdict,
            "outcome": None,
            "recorded": False,
            "skipped_reason": result.verdict,
            "passband_hz": list(passband),
            "acoustic": acoustic,
            "measurement": None,
        }
        if emit_lifecycle_event:
            _log_capture_verdict_event(
                rejected, speaker_group_id=speaker_group_id, role=role,
            )
        return rejected
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
        "repeats": dict(repeats) if isinstance(repeats, Mapping) else None,
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
    accepted: dict[str, Any] = {
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
    if emit_lifecycle_event:
        _log_capture_verdict_event(
            accepted, speaker_group_id=speaker_group_id, role=role
        )
    return accepted


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
    noise_band_report: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    ambient_report: Mapping[str, Any] | None = None,
    ambient_duration_s: float | None = None,
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
    The analyzed ``fc`` is resolved back to its preset crossover region
    (:func:`region_for_fc`) and stamped onto the persisted record — this is
    what lets :func:`build_crossover_alignment_proposal` retain BOTH this
    capture's polarity (``expect_null``) and its sibling opposite-polarity
    capture as distinct, region-keyed evidence instead of one overwriting
    the other.
    """
    fc = (
        float(crossover_fc_hz)
        if crossover_fc_hz and crossover_fc_hz > 0
        else primary_crossover_fc_hz(preset)
    )
    if not fc:
        no_crossover_rejected: dict[str, Any] = {
            "verdict": None,
            "outcome": None,
            "recorded": False,
            "skipped_reason": "no_crossover_region",
            "crossover_fc_hz": None,
            "acoustic": None,
            "measurement": None,
        }
        _log_capture_verdict_event(
            no_crossover_rejected, speaker_group_id=speaker_group_id,
        )
        return no_crossover_rejected
    # Both capture kinds judge "is a null present?" against the same threshold; for
    # a reverse-polarity capture (one driver inverted) a present null is the PASS,
    # for an in-phase one it is the PROBLEM. The cap-independent polarity call
    # (reverse-vs-in-phase margin) is the proposal's job, not this per-capture verdict.
    analyze_kwargs: dict[str, Any] = {
        "crossover_fc_hz": fc,
        "null_threshold_db": null_threshold_db,
        "expect_null": expect_null,
        "has_mic_calibration": has_mic_calibration,
        "calibration": calibration,
        "noise_band_report": noise_band_report,
        "noise_floor_dbfs": noise_floor_dbfs,
        "capture_geometry": capture_geometry,
    }
    if ambient_duration_s is not None:
        analyze_kwargs["ambient_duration_s"] = ambient_duration_s
    result = analyze(captured_wav, sweep_meta, **analyze_kwargs)
    acoustic = result.to_dict()
    if isinstance(ambient_report, Mapping) and not isinstance(
        acoustic.get("ambient"), Mapping
    ):
        acoustic["ambient"] = dict(ambient_report)
    normalized_noise_floor = _finite_float(noise_floor_dbfs)
    if normalized_noise_floor is not None:
        acoustic["noise_floor_dbfs"] = normalized_noise_floor
        acoustic["signal_over_noise_db"] = (
            float(result.observed_mic_dbfs) - normalized_noise_floor
        )
    outcome = SUMMED_VERDICT_TO_OUTCOME.get(result.verdict)
    if outcome is None:
        rejected: dict[str, Any] = {
            "verdict": result.verdict,
            "outcome": None,
            "recorded": False,
            "skipped_reason": result.verdict,
            "crossover_fc_hz": fc,
            "acoustic": acoustic,
            "measurement": None,
        }
        _log_capture_verdict_event(rejected, speaker_group_id=speaker_group_id)
        return rejected
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
        # Which crossover region this fc belongs to -- lets paired in-phase/
        # reverse evidence be grouped per region downstream (measurement.py's
        # latest_summed_pairs_by_group). None when fc matches no preset
        # region (should not happen: fc always came FROM one above).
        "region": region_for_fc(preset, fc),
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        bundle_ref=bundle_ref,
        state_path=state_path,
        now=now,
    )
    accepted: dict[str, Any] = {
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
    _log_capture_verdict_event(accepted, speaker_group_id=speaker_group_id)
    return accepted


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


def _pair_null_depths(
    in_phase_record: Any,
    reverse_record: Any,
) -> tuple[float | None, float | None]:
    """(in_phase_null_db, reverse_null_db) read directly from a resolved pair.

    Each side's null depth comes straight from ITS OWN record — no more
    XOR-on-``expect_null`` routing through one latest-record-per-group slot.
    ``in_phase_record`` / ``reverse_record`` are the two sides of one
    ``measurement.py`` ``latest_summed_pairs_by_group[group][region_key]``
    entry (either may be ``None`` — that capture hasn't been taken yet, or
    fell off the ``MAX_SUMMED_RECORDS`` ring).
    """

    def _depth(record: Any) -> float | None:
        if not isinstance(record, Mapping):
            return None
        acoustic = record.get("acoustic")
        if not isinstance(acoustic, Mapping):
            return None
        raw_depth = acoustic.get("null_depth_db")
        if raw_depth is None:
            return None
        try:
            depth = float(raw_depth)
        except (TypeError, ValueError):
            return None
        return depth if math.isfinite(depth) else None

    return _depth(in_phase_record), _depth(reverse_record)


def _record_alignment_snr(summed_record: Any) -> tuple[bool | None, bool]:
    """(alignment_snr_ok, null_depth_capped) from ONE summed record.

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


def _summed_alignment_snr(
    in_phase_record: Any,
    reverse_record: Any = None,
) -> tuple[bool | None, bool]:
    """(alignment_snr_ok, null_depth_capped) — the conservative combination
    across up to two paired summed records (in-phase + reverse-polarity).

    Evaluates each side independently through :func:`_record_alignment_snr`
    and combines them conservatively, never optimistically. A single capture
    still surfaces the same tentative preview, but its pair-quality verdict is
    ``None`` because the absent side cannot authorize automatic application.

    * ``null_depth_capped`` is ``True`` if EITHER side capped its null depth
      — a capped depth on either polarity's capture means that side's
      number wasn't fully provable, and the proposer's alignment gate must
      see that regardless of which side actually holds the deep null.
    * ``alignment_snr_ok`` is ``False`` if EITHER side confirmed insufficient
      overlap-band SNR (a confirmed problem on one side is not erased by a
      clean reading on the other); ``None`` when EITHER contributing side is
      unknown; and ``True`` only when BOTH sides affirmatively cleared the
      alignment threshold. A pair margin consumes both null depths, so one
      clean capture cannot authorize the other capture's unknown depth.
    """
    ok_in_phase, capped_in_phase = _record_alignment_snr(in_phase_record)
    ok_reverse, capped_reverse = _record_alignment_snr(reverse_record)
    if ok_in_phase is False or ok_reverse is False:
        alignment_snr_ok: bool | None = False
    elif ok_in_phase is None or ok_reverse is None:
        alignment_snr_ok = None
    else:
        alignment_snr_ok = True
    return alignment_snr_ok, (capped_in_phase or capped_reverse)


def _resolve_region_pair(
    *,
    pairs_by_group: Mapping[str, Any],
    flat_by_group: Mapping[str, Any],
    group: str,
    region_key: str,
    allow_flat_fallback: bool,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """(in_phase_record, reverse_record) for one (group, region_key).

    Prefers ``measurement.py``'s region-keyed ``latest_summed_pairs_by_group``
    — the real production shape, since ``load_measurement_state`` always
    produces it alongside the flat map. Falls back to the flat, region-
    unaware ``latest_summed_by_group`` (latest in-phase record per group)
    routed by its own ``expect_null`` flag ONLY when ``allow_flat_fallback``
    (the caller passes this true only for a group's SOLE region — a 2-way,
    or a hand-built ``measurements`` mapping that predates the paired shape
    entirely): a 3-way's second region has no way to claim a flat,
    region-unaware record without risking misfiling another region's
    evidence as its own, so it stays unpaired instead. This resolver preserves
    historical visibility only; :func:`summed_decision_evidence_state` is the
    later authorization boundary and rejects every legacy/flat fallback.
    """
    group_pairs = (
        pairs_by_group.get(group) if isinstance(pairs_by_group, Mapping) else None
    )
    pair = group_pairs.get(region_key) if isinstance(group_pairs, Mapping) else None
    if isinstance(pair, Mapping):
        in_phase = pair.get("in_phase")
        reverse = pair.get("reverse")
        return (
            in_phase if isinstance(in_phase, Mapping) else None,
            reverse if isinstance(reverse, Mapping) else None,
        )
    if not allow_flat_fallback:
        return None, None
    record = flat_by_group.get(group) if isinstance(flat_by_group, Mapping) else None
    if not isinstance(record, Mapping):
        return None, None
    acoustic = record.get("acoustic")
    if not isinstance(acoustic, Mapping):
        return None, None
    if acoustic.get("expect_null"):
        return None, record
    return record, None


def build_crossover_alignment_proposal(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    *,
    requested_mode: str = PHASE_AWARE,
    speaker_group_id: str | None = None,
    expected_profile_context_id: str | None = None,
    expected_applied_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a SAFE crossover polarity refinement (+ delay status) from state.

    A pure read: for EVERY crossover region (sorted by fc — every region in a
    3-way, not only the lowest) it walks the recorded summed-crossover null
    depths (in-phase and, if captured, reverse-polarity) and asks
    :func:`crossover_alignment.propose_crossover_alignment`.

    The phase_aware gate is enforced AT THE DATA and is PER REGION: every
    record contributing to a region's proposal (both drivers' captures, both
    summed captures of that region's pair) must be calibrated
    (``acoustic.calibrated``); otherwise that region's proposal downgrades to
    ``magnitude_only`` and is unauthorized (no polarity/delay decision). So a
    phone capture can never yield a phase decision even if phase_aware is
    requested, and one region's uncalibrated evidence never blocks a sibling
    region whose own evidence IS calibrated. A second, independent gate rides
    each region's own pair's SC-1 alignment-class SNR block
    (:func:`_summed_alignment_snr`, the conservative combination across both
    polarities of that region): a confirmed-insufficient overlap SNR or a
    capped null depth further downgrades keep/invert to review and "aligned"
    to "unknown" inside the proposer, even when phase_aware was granted.
    Never raises on thin/empty state; returns ``{status, speaker_group_id,
    mode, proposals, proposal}`` — ``proposals`` is one ``{region, proposal}``
    entry per crossover region sorted by fc; ``mode``/``proposal`` are the
    lowest region's, kept for backward compatibility with callers that only
    know about a single crossover.

    Multi-group (stereo-pair) polarity/delay *emission* is deferred (see
    ``baseline_profile``'s ``group_specific_alignment_not_applied``); the
    proposal computes for one group, so a mono/single-group speaker (jts3's
    active_mono_2way) gets the full L2 polarity refinement.

    A paired summary is not authority by itself. Every contributing summed
    record passes :func:`crossover_contract.summed_decision_evidence_state`,
    which binds it to the current full comparison/profile fingerprint, audible
    playback artifact, fixed reference-axis placement proof, blocker-free
    analyzer result, and this exact region/Fc. Rejected records remain visible
    through the measurement history and the returned per-region ``evidence``
    verdict, but cannot supply a null or grant ``phase_aware``.
    """
    regions = sorted(
        (r for r in preset.crossover_regions if r.fc_hz and r.fc_hz > 0),
        key=lambda r: r.fc_hz,
    )
    if not regions:
        return {"status": "no_crossover", "proposal": None, "proposals": []}

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

    pairs_by_group = measurements.get("latest_summed_pairs_by_group")
    if not isinstance(pairs_by_group, Mapping):
        summary = measurements.get("summary")
        pairs_by_group = (
            summary.get("latest_summed_pairs_by_group")
            if isinstance(summary, Mapping)
            else None
        )
    pairs_by_group = pairs_by_group if isinstance(pairs_by_group, Mapping) else {}
    active_comparison_set = measurements.get("active_comparison_set")
    active_comparison_set = (
        active_comparison_set
        if isinstance(active_comparison_set, Mapping)
        else None
    )

    group = speaker_group_id
    if group is None:
        groups = (
            {g for (g, _r) in by_group_role}
            | set(summed_by_group.keys())
            | set(pairs_by_group.keys())
        )
        if len(groups) == 1:
            group = next(iter(groups))
        elif summed_by_group:
            group = sorted(summed_by_group.keys())[0]
        elif pairs_by_group:
            group = sorted(pairs_by_group.keys())[0]
        elif groups:
            group = sorted(groups)[0]
    if group is None:
        return {"status": "no_measurements", "proposal": None, "proposals": []}

    allow_flat_fallback = len(regions) == 1
    proposals: list[dict[str, Any]] = []
    resolved_modes: list[ResolvedMode] = []
    for region in regions:
        lower_role = region.lower_driver
        upper_role = region.upper_driver
        fc = float(region.fc_hz)
        region_key = _region_key(lower_role, upper_role)

        lower_rec = by_group_role.get((group, lower_role))
        upper_rec = by_group_role.get((group, upper_role))
        raw_in_phase_rec, raw_reverse_rec = _resolve_region_pair(
            pairs_by_group=pairs_by_group,
            flat_by_group=summed_by_group,
            group=group,
            region_key=region_key,
            allow_flat_fallback=allow_flat_fallback,
        )
        in_phase_evidence = summed_decision_evidence_state(
            raw_in_phase_rec,
            active_comparison_set=active_comparison_set,
            expected_applied_profile=expected_applied_profile,
            speaker_group_id=group,
            lower_role=lower_role,
            upper_role=upper_role,
            crossover_fc_hz=fc,
            expected_expect_null=False,
            expected_profile_context_id=expected_profile_context_id,
        )
        reverse_evidence = summed_decision_evidence_state(
            raw_reverse_rec,
            active_comparison_set=active_comparison_set,
            expected_applied_profile=expected_applied_profile,
            speaker_group_id=group,
            lower_role=lower_role,
            upper_role=upper_role,
            crossover_fc_hz=fc,
            expected_expect_null=True,
            expected_profile_context_id=expected_profile_context_id,
        )
        in_phase_rec = (
            raw_in_phase_rec if in_phase_evidence["valid"] is True else None
        )
        reverse_rec = raw_reverse_rec if reverse_evidence["valid"] is True else None
        in_phase_null, reverse_null = _pair_null_depths(in_phase_rec, reverse_rec)
        alignment_snr_ok, null_depth_capped = _summed_alignment_snr(
            in_phase_rec, reverse_rec
        )

        # The phase_aware gate at the data layer, per region: every
        # contributing capture must be calibrated. A single uncalibrated
        # record blocks phase_aware for THIS region only.
        cal_flags = [
            flag
            for flag in (
                _acoustic_calibrated(lower_rec),
                _acoustic_calibrated(upper_rec),
                _acoustic_calibrated(in_phase_rec),
                _acoustic_calibrated(reverse_rec),
            )
            if flag is not None
        ]
        has_summed_decision_evidence = bool(in_phase_rec or reverse_rec)
        data_calibrated = (
            has_summed_decision_evidence and bool(cal_flags) and all(cal_flags)
        )
        resolved = resolve_measurement_mode(
            requested_mode, has_calibrated_mic=data_calibrated
        )
        resolved_modes.append(resolved)

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
        proposal_payload = proposal.to_dict()
        rejected_evidence = [
            kind
            for kind, raw_record, state in (
                ("in_phase", raw_in_phase_rec, in_phase_evidence),
                ("reverse", raw_reverse_rec, reverse_evidence),
            )
            if raw_record is not None and state["valid"] is not True
        ]
        for kind in rejected_evidence:
            proposal_payload["issues"].append({
                "severity": "warning",
                "code": "summed_decision_evidence_rejected",
                "message": (
                    f"{kind.replace('_', '-')} summed evidence is historical only; "
                    "capture it again at the fixed reference axis in the current "
                    "automatic measurement run"
                ),
            })
        proposals.append({
            "region": {
                "lower_role": lower_role,
                "upper_role": upper_role,
                "fc_hz": fc,
            },
            "evidence": {
                "in_phase": in_phase_evidence,
                "reverse": reverse_evidence,
            },
            "decision_quality": {
                "alignment_snr_ok": alignment_snr_ok,
                "null_depth_capped": null_depth_capped,
            },
            "proposal": proposal_payload,
        })

    return {
        "status": "ok",
        "speaker_group_id": group,
        "mode": resolved_modes[0].to_dict(),
        "proposals": proposals,
        "proposal": proposals[0]["proposal"],
    }


# --------------------------------------------------------------------------
# Three-repeat capture — outlier rejection, never noise-floor reduction
# (design doc "Slice 0: measurement-validity substrate").
# --------------------------------------------------------------------------

DEFAULT_REPEAT_TARGET = 3
# |level_dbfs - running_median| beyond this rejects a repeat as an outlier.
REPEAT_OUTLIER_DB = 3.0
# spread_db_p90 above this downgrades confidence even at a full target.
REPEAT_CONFIDENCE_SPREAD_DB = 2.0

# The lane A/B SNR-verdict vocabulary ("ok"/"reduced"/"insufficient"/
# "unknown" — see snr_policy.py's band_snr_verdicts). Only the two
# non-informative ends reject a repeat outright.
_REPEAT_REJECT_SNR_VERDICTS = frozenset({"insufficient", "unknown"})


def _repeat_level_dbfs(acoustic: Mapping[str, Any]) -> float | None:
    """Broadband magnitude summary used for the median/spread computation.

    ``observed_mic_dbfs`` (the capture RMS) is the one scalar every
    analyzer result already carries, driver or summed — the natural
    "shared grid" comparison quantity across repeats taken under the same
    excitation/gain ledger.
    """

    return _finite_float(acoustic.get("observed_mic_dbfs"))


_REFERENCE_BINDING_KEYS = (
    "policy_id",
    "comparison_set_id",
    "comparison_set_fingerprint",
    "target_fingerprint",
    "speaker_group_id",
    "role",
)


def _reference_axis_repeat_binding(
    item: Mapping[str, Any], acoustic: Mapping[str, Any]
) -> tuple[str, ...] | None:
    """Return the immutable fixed-axis placement binding for one repeat.

    ``None`` means the repeat is not reference-axis. An empty tuple marks an
    explicitly reference-axis repeat without a complete server proof; callers
    reject it before it can influence the aggregate median.
    """

    if acoustic.get("capture_geometry") != "reference_axis":
        return None
    proof = item.get("placement_proof")
    if not isinstance(proof, Mapping):
        return ()
    from .capture_geometry import (
        REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
        placement_proof_shape_valid,
    )

    values = tuple(str(proof.get(key) or "") for key in _REFERENCE_BINDING_KEYS)
    if (
        not placement_proof_shape_valid(
            proof,
            policy_id=REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
            speaker_group_id=str(proof.get("speaker_group_id") or ""),
            role=str(proof.get("role") or ""),
            target_fingerprint=str(proof.get("target_fingerprint") or ""),
        )
        or any(not value for value in values)
    ):
        return ()
    return values


def _repeat_reject_reason(
    *,
    verdict: Any,
    acoustic: Mapping[str, Any],
    level_dbfs: float | None,
    running_median: float | None,
) -> str | None:
    """The outlier-rejection rule for one repeat.

    Reads the lane A/B SNR and validity-floor evidence. Legacy/near-field
    records without those sibling blocks retain level-outlier-only behavior.
    A record explicitly marked ``capture_geometry=reference_axis`` is held to
    the stronger Lane B contract: missing/ungateable validity evidence rejects
    as ``validity_floor_unknown``. Known-below-floor and insufficient SNR
    evidence also reject before the level check.
    """

    gating = acoustic.get("gating")
    overlap_entries = [
        entry
        for entry in acoustic.get("overlap_levels") or ()
        if isinstance(entry, Mapping)
    ]
    if acoustic.get("capture_geometry") == "reference_axis":
        floor_value = gating.get("f_valid_floor_hz") if isinstance(gating, Mapping) else None
        floor_hz = (
            None
            if isinstance(floor_value, bool)
            else _finite_float(floor_value)
            if isinstance(gating, Mapping)
            else None
        )
        floor_states = [
            entry.get("above_validity_floor") for entry in overlap_entries
        ]
        if (
            not isinstance(gating, Mapping)
            or gating.get("applied") is not True
            or floor_hz is None
            or floor_hz <= 0
            or any(state is None for state in floor_states)
        ):
            return "validity_floor_unknown"
        if not overlap_entries:
            return "no_usable_overlap"
    if verdict in (None, VERDICT_UNUSABLE_CAPTURE):
        return "unusable_capture"
    if bool(acoustic.get("mic_clipping")):
        return "clipping"
    if isinstance(gating, Mapping) and gating.get("above_validity_floor") is False:
        return "below_validity_floor"
    if overlap_entries:
        floor_states = [
            entry.get("above_validity_floor") for entry in overlap_entries
        ]
        if floor_states and all(state is False for state in floor_states):
            return "below_validity_floor"
        # A woofer's unusable bottom octave or a 3-way mid's unusable lower
        # handoff must not veto a clean required overlap at the other edge.
        # ``usable`` is the analyzer-owned conjunction of bins, local SNR,
        # clipping and validity floor. Reject only when no topology overlap can
        # support the trim decision.
        if not any(entry.get("usable") is True for entry in overlap_entries):
            return "no_usable_overlap"
    else:
        # Legacy/fabricated analyzers without topology overlap entries retain
        # the coarse whole-passband SNR behavior.
        snr = acoustic.get("snr")
        snr_verdict = snr.get("verdict") if isinstance(snr, Mapping) else None
        if snr_verdict in _REPEAT_REJECT_SNR_VERDICTS:
            return f"snr_{snr_verdict}"
    if level_dbfs is None:
        return "level_unavailable"
    if (
        running_median is not None
        and abs(level_dbfs - running_median) > REPEAT_OUTLIER_DB
    ):
        return "level_outlier"
    return None


def aggregate_driver_repeats(
    repeats: Sequence[Mapping[str, Any]],
    *,
    target: int = DEFAULT_REPEAT_TARGET,
) -> dict[str, Any]:
    """Aggregate N per-driver (or summed) repeat captures via outlier rejection.

    Each item in ``repeats`` carries at minimum ``verdict`` and ``acoustic``
    (the ``DriverAcousticResult``/``SummedAcousticResult.to_dict()`` block a
    single ``analyze_driver_capture``/``analyze_summed_crossover`` call
    already produces) plus whatever else the caller wants preserved
    (``artifact_path``, ``excitation``, ``placement_proof``, ...) — the
    whole item is echoed back verbatim as ``aggregate_repeat`` when it wins.

    Processes repeats IN ORDER, comparing each against the running median of
    already-*accepted* levels (:func:`_repeat_reject_reason`) — never
    against the group's final median, so an early anchor is not
    retroactively second-guessed once later repeats arrive. Magnitude only:
    there is no complex/IR input path here, and the aggregate reuses the
    ACCEPTED repeat closest to the final median's full ``acoustic`` block
    verbatim — never a synthesized average across curves, and never a
    claimed SNR improvement (repeats reject outliers; they do not reduce
    the noise floor).

    ``needed_recapture`` signals "short of target and the ONE bounded extra
    attempt hasn't been used yet" (``len(repeats) <= target``); a caller
    reads it to decide whether to capture one more attempt (append it and
    call again) or finalize with whatever is accepted so far (confidence
    degrades to ``"reduced"`` below the full ``target`` accepted or above
    the spread floor). ``recaptured`` records whether that extra attempt
    actually happened (``len(repeats) > target``).
    """

    per_repeat: list[dict[str, Any]] = []
    accepted_levels: list[float] = []
    accepted_indices: list[int] = []
    reference_axis_binding: tuple[str, ...] | None = None
    sequence_geometry: str | None = None

    for index, item in enumerate(repeats):
        acoustic = item.get("acoustic")
        acoustic = acoustic if isinstance(acoustic, Mapping) else {}
        verdict = item.get("verdict")
        level_dbfs = _repeat_level_dbfs(acoustic)
        running_median = (
            statistics.median(accepted_levels) if accepted_levels else None
        )
        binding = _reference_axis_repeat_binding(item, acoustic)
        geometry = acoustic.get("capture_geometry")
        reason: str | None
        if geometry in {"near_field", "reference_axis"} and sequence_geometry is None:
            sequence_geometry = str(geometry)
        if geometry in {"near_field", "reference_axis"} and geometry != sequence_geometry:
            reason = "capture_context_mismatch"
        elif binding == ():
            reason = "reference_axis_placement_unbound"
        elif binding is not None and reference_axis_binding not in (None, binding):
            reason = "capture_context_mismatch"
        else:
            reason = _repeat_reject_reason(
                verdict=verdict,
                acoustic=acoustic,
                level_dbfs=level_dbfs,
                running_median=running_median,
            )
        if binding and reference_axis_binding is None:
            reference_axis_binding = binding
        accepted = reason is None
        snr = acoustic.get("snr")
        worst_relevant_raw = snr.get("worst_relevant") if isinstance(snr, Mapping) else None
        worst_relevant: Mapping[str, Any] = (
            worst_relevant_raw if isinstance(worst_relevant_raw, Mapping) else {}
        )
        gating_raw = acoustic.get("gating")
        gating: Mapping[str, Any] = (
            gating_raw if isinstance(gating_raw, Mapping) else {}
        )
        overlap_floor_evidence = [
            entry.get("above_validity_floor")
            for entry in acoustic.get("overlap_levels") or ()
            if isinstance(entry, Mapping)
            and entry.get("above_validity_floor") in (True, False, None)
        ]
        if isinstance(gating.get("above_validity_floor"), bool):
            above_validity_floor: bool | None = bool(
                gating["above_validity_floor"]
            )
        elif any(value is True for value in overlap_floor_evidence):
            above_validity_floor = True
        elif overlap_floor_evidence and all(
            value is False for value in overlap_floor_evidence
        ):
            above_validity_floor = False
        elif acoustic.get("capture_geometry") == "reference_axis":
            above_validity_floor = None
        else:
            above_validity_floor = True
        per_repeat.append({
            "index": index,
            "attempt": int(item.get("attempt") or index + 1),
            "verdict": verdict,
            "accepted": accepted,
            "reject_reason": reason,
            "artifact_path": item.get("artifact_path"),
            "estimated_snr_db": _finite_float(worst_relevant.get("estimated_snr_db")),
            "clipping": bool(acoustic.get("mic_clipping")),
            "above_validity_floor": above_validity_floor,
            "level_dbfs": level_dbfs,
        })
        if accepted and level_dbfs is not None:
            accepted_levels.append(level_dbfs)
            accepted_indices.append(index)
        if len(accepted_levels) >= target:
            break  # bounded: stop once the target accepted count is reached

    accepted_count = len(accepted_levels)
    rejected_count = sum(1 for entry in per_repeat if not entry["accepted"])
    attempts = len(per_repeat)

    spread_db_p90: float | None = None
    aggregate_repeat: dict[str, Any] | None = None
    if accepted_count >= 1:
        median_level = statistics.median(accepted_levels)
        if accepted_count >= 2:
            deviations = sorted(
                abs(level - median_level) for level in accepted_levels
            )
            rank = min(
                len(deviations) - 1, max(0, math.ceil(0.9 * len(deviations)) - 1)
            )
            spread_db_p90 = deviations[rank]
        winner_local = min(
            range(accepted_count),
            key=lambda i: abs(accepted_levels[i] - median_level),
        )
        aggregate_repeat = dict(repeats[accepted_indices[winner_local]])

    # DEVIATION (flagged in the PR body): the STEP 1 CONTRACT §9 text reads
    # "confidence = normal when accepted >= 2 AND spread_db_p90 <= 2.0" —
    # taken literally, that can never actually gate anything given
    # REPEAT_OUTLIER_DB=3.0: with exactly 2 accepted, the second is always
    # checked directly against the running median built from the first, so
    # their spread is mathematically bounded to <= REPEAT_OUTLIER_DB / 2 ==
    # 1.5, always under the 2.0 floor — "two accepted" would ALWAYS read
    # "normal", indistinguishable from a full three, contradicting both the
    # required test ("refusing the re-capture -> proceeds with two,
    # confidence reduced") and the product intent of the field (fewer
    # repeats than the protocol calls for is honestly lower-confidence
    # evidence). Reading "2" as shorthand for "enough to take a median at
    # all" and gating "normal" on reaching the full `target` instead
    # resolves the contradiction and keeps the spread check meaningful as
    # an ADDITIONAL floor once target is reached.
    confidence = (
        "normal"
        if (
            accepted_count >= target
            and spread_db_p90 is not None
            and spread_db_p90 <= REPEAT_CONFIDENCE_SPREAD_DB
        )
        else "reduced"
    )
    recaptured = attempts > target
    needed_recapture = accepted_count < target and attempts <= target

    return {
        "repeat_group_id": uuid.uuid4().hex[:12],
        "target": target,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "recaptured": recaptured,
        "needed_recapture": needed_recapture,
        "aggregate": "median_magnitude",
        "spread_db_p90": spread_db_p90,
        "confidence": confidence,
        "per_repeat": per_repeat,
        "aggregate_repeat": aggregate_repeat,
    }


def record_driver_repeat_aggregate(
    *,
    speaker_group_id: str,
    role: str,
    repeats: Sequence[Mapping[str, Any]],
    target: int = DEFAULT_REPEAT_TARGET,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate driver repeats and emit the repeats-aggregated lifecycle event.

    Logs ``correction.crossover_repeats_aggregated`` (SC-5) via
    :func:`jasper.log_event.log_event`. A pure evidence step: it does not
    itself call ``record_driver_measurement`` — the web orchestration layer
    uses the returned ``aggregate_repeat`` exactly to re-analyze and record
    the winning capture.  Durable measurement state receives only the compact
    counters, spread, and ``per_repeat`` projection; the process-local winner
    object and full repeat artifacts remain in the commissioning bundle.
    """

    aggregate = aggregate_driver_repeats(repeats, target=target)
    log_event(
        logger,
        "correction.crossover_repeats_aggregated",
        session=session_id,
        group=speaker_group_id,
        role=role,
        accepted=aggregate["accepted"],
        rejected=aggregate["rejected"],
        spread_db=aggregate["spread_db_p90"],
        confidence=aggregate["confidence"],
    )
    return aggregate
