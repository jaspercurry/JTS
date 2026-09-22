# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What KIND of feature is that — measured, from a round's own banked captures.

The INSTRUMENT behind :mod:`.feature_classification`'s register: that module
owns the verdict names, the row schema and what ``depth_db`` means, this one
runs the tests that fill them in over captures a round already banked. It
imports every verdict string and never spells one.

**Three tests and a gate.** EXCESS GROUP DELAY — minimum-phase (a driver
defect, so a filter is at least the right kind of tool) or non-minimum-phase
(a cancellation, where a filter lowers the direct sound and its delayed copy
together), read as a fraction of what a genuine same-frequency cancellation
produces through this exact pipeline, which the C4 control measures. GATE
INVARIANCE — the window ladder, run by :mod:`.gate_sweep`; this module owns
no second one. TIMING SCATTER — the sub-sample arrival residual between
captures at the same angle, which needs a repeated angle to run and never
reads its own absence as evidence of tight timing.

**The controls certify the PHASE test, and only it.** Four known answers are
pushed through the identical pipeline on the round's own measured IR, and
none of them touches the magnitude ladder. A run whose controls fail still
writes its artifact and still publishes a real ``gate_verdict``; what it
loses is the phase class, which every row reports as ``ambiguous`` beside
``controls_ok: false`` and the raw reading in ``egd_verdict_raw``.
``defect-*`` is unreachable there — it requires a MIN-PHASE egd verdict — so
no filter can be vouched for by an instrument that failed its own
known-answer check.

It lives here rather than under :mod:`jasper.audio_measurement` because
``tests/test_audio_measurement_boundary_ssot.py``'s
``test_package_boundary_holds`` forbids that
package importing ``jasper.active_speaker``, which this module does for the
verdict register and the phase names. **If you are about to move this
module, read that test first.**
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from jasper.audio_measurement.deconv import regularized_deconvolution_full
from jasper.audio_measurement.excess_phase import (
    COMPLEX_SMOOTH_OCT,
    MAGNITUDE_SMOOTH_FRACTION,
    classification_grid,
    egd_excursion,
    excess_group_delay,
    gate,
    smoothed_curve,
)
from jasper.audio_measurement.gating import (
    SEARCH_T_MAX_MS,
    f_trusted_floor_hz,
)

from ..feature_classification import GATE_MOVED as GATE_MOVED
from ..feature_optics import (
    PHASE_GATE_LEAD_MS as PHASE_GATE_LEAD_MS,
    biquad_peaking as biquad_peaking,
    detrend,
    feature_q,
    read_feature,
)
from ..gate_sweep import (
    CENTRE_SHIFT_OCT,
    DEFAULT_RUNGS_MS,
    GATE_DELTA_SLACK_DB as GATE_DELTA_SLACK_DB,
    SIGMA_GROWTH_MIN_SIGMA_DB as SIGMA_GROWTH_MIN_SIGMA_DB,
    SIGMA_GROWTH_ROOM_RATIO as SIGMA_GROWTH_ROOM_RATIO,
)
from ..journey import PHASE_LATERAL as PHASE_LATERAL
from .captures import (
    ADMISSIBLE_PHASES as ADMISSIBLE_PHASES,
    CAPTURE_ADMISSIBILITY_REASONS as CAPTURE_ADMISSIBILITY_REASONS,
    CAPTURE_OTHER_SESSION as CAPTURE_OTHER_SESSION,
    CAPTURE_PHASE_NOT_ADMISSIBLE as CAPTURE_PHASE_NOT_ADMISSIBLE,
    CAPTURE_PROGRAM_MISSING as CAPTURE_PROGRAM_MISSING,
    CAPTURE_PROGRAM_UNIDENTIFIED as CAPTURE_PROGRAM_UNIDENTIFIED,
    CAPTURE_UNSTAMPED_NAME as CAPTURE_UNSTAMPED_NAME,
    CAPTURE_WAV_MISSING as CAPTURE_WAV_MISSING,
    CAPTURES_UNREADABLE as CAPTURES_UNREADABLE,
    CLASSIFICATION_REFUSAL_REASONS as CLASSIFICATION_REFUSAL_REASONS,
    NO_ADMISSIBLE_CAPTURES as NO_ADMISSIBLE_CAPTURES,
    NO_FEATURES_DETECTED as NO_FEATURES_DETECTED,
    PROGRAM_MISSING as PROGRAM_MISSING,
    ROUND_SHAPE_INADMISSIBLE as ROUND_SHAPE_INADMISSIBLE,
    FeatureClassificationRefused as FeatureClassificationRefused,
    RoundCapture as RoundCapture,
    RoundPoseCurve as RoundPoseCurve,
    _read_wav,
    load_round_captures as load_round_captures,
    load_round_pose_curves as load_round_pose_curves,
)
from .compose import (
    FEATURE_MIN_DEPARTURE_DB,
    FEATURE_MIN_SEPARATION_OCT,
    FEATURE_STABILITY_Z,
    FRAC_NMP_MIN_PHASE as FRAC_NMP_MIN_PHASE,
    FRAC_NMP_NON_MIN_PHASE as FRAC_NMP_NON_MIN_PHASE,
    MAX_FEATURES,
    Z_LOCAL_FLAT as Z_LOCAL_FLAT,
    _compose as _compose,
    _detect_features,
    classifiable_band_hz as classifiable_band_hz,
    summary_lines as summary_lines,
)
from .controls import (
    CONTROL_ALLPASS_RATIO_BAND as CONTROL_ALLPASS_RATIO_BAND,
    CONTROL_COMB_MP_GAIN as CONTROL_COMB_MP_GAIN,
    CONTROL_COMB_NMP_GAIN as CONTROL_COMB_NMP_GAIN,
    CONTROL_ECHO_MS as CONTROL_ECHO_MS,
    CONTROL_MAX_ECHO_FALSE_POSITIVE_US as CONTROL_MAX_ECHO_FALSE_POSITIVE_US,
    CONTROL_MAX_FALSE_POSITIVE_US as CONTROL_MAX_FALSE_POSITIVE_US,
    CONTROLS_FAILED_DISCLOSURE as CONTROLS_FAILED_DISCLOSURE,
    _run_controls as _run_controls,
)
from .ladders import (
    DECAY_TARGET_DROP_DB as DECAY_TARGET_DROP_DB,
    FDW_CYCLES as FDW_CYCLES,
    FDW_TAPER as FDW_TAPER,
    GATE_LADDER_NEEDS_TWO_RUNGS as GATE_LADDER_NEEDS_TWO_RUNGS,
    _decay_bands_hz as _decay_bands_hz,
    _decay_read as _decay_read,
    _DecayHost as _DecayHost,
    _fdw_rungs,
    _gate_call as _gate_call,
    _pose_bank_block,
    _pose_persistence_block,
    _pose_reading,
    _sweep_ladder,
    _timing_scatter,
)

__all__ = [
    "ADMISSIBLE_PHASES",
    "CAPTURE_ADMISSIBILITY_REASONS",
    "CAPTURES_UNREADABLE",
    "classifiable_band_hz",
    "CLASSIFICATION_REFUSAL_REASONS",
    "CLASSIFICATION_SCHEMA_VERSION",
    "NO_ADMISSIBLE_CAPTURES",
    "NO_FEATURES_DETECTED",
    "PROGRAM_MISSING",
    "ROUND_SHAPE_INADMISSIBLE",
    "FeatureClassificationRefused",
    "RoundCapture",
    "RoundPoseCurve",
    "classify_round",
    "load_round_captures",
    "load_round_pose_curves",
    "summary_lines",
]


#: The artifact's own version. Deliberately not a new number: the row shape is
#: the one the register already reads and the 2026-08-19 records already carry.
CLASSIFICATION_SCHEMA_VERSION = 1

GENERATED_BY = "jasper.active_speaker.crossover_v2.feature_classifier"


#: The primary analysis window. ``gating.SEARCH_T_MAX_MS`` is the product's own
#: reflection search ceiling AND the window it falls back to when no reflection
#: is found, so a fixed window of that length is the longest one the product
#: ever calls reflection-free. Overridable per run.
DEFAULT_GATE_MS = SEARCH_T_MAX_MS

#: What :func:`excess_group_delay` actually windows with. Research 03's
#: pitfall is real — a SHORT gate biases the Hilbert min-phase
#: reconstruction — but the window this instrument reads EGD through is
#: ``DEFAULT_GATE_MS`` (``== gating.SEARCH_T_MAX_MS``), the longest window
#: the product ever calls reflection-free, so there is no longer clean
#: window to move to. It is also the exact window the C1/C3 controls are
#: calibrated on, on THIS round's own IR.
EGD_WINDOW_KIND = "fixed_reflection_free_gate"

TRUSTED_CEILING_HZ = 16000.0


def classify_round(
    captures: Sequence[RoundCapture],
    *,
    at: Sequence[float] | None = None,
    gate_ms: float = DEFAULT_GATE_MS,
    gates_ms: Sequence[float] | None = None,
    pose_curves: Sequence[RoundPoseCurve] = (),
) -> dict[str, Any]:
    """Classify one round's features and return the artifact to bank.

    ``at`` pins the frequencies to classify; omitted, they are detected from
    the round's own pooled response (:func:`_detect_features`).

    ``gate_ms`` is the PRIMARY window — the one the phase test, the feature
    detector and the trusted band are read through. ``gates_ms`` is the
    window LADDER, which is :mod:`.gate_sweep`'s. The two are independent.

    ``pose_curves`` is this round's banked lateral-walk curves
    (:func:`load_round_pose_curves`), optional and orthogonal to
    ``captures``: a caller with none still gets every EGD/gate/timing fact,
    plus a ``pose_bank``/``pose_persistence`` NOT-RUN pair rather than a
    refusal.

    A round whose known-answer controls did not pass is REPORTED, not
    refused: every row keeps its real ``gate_verdict`` and gives up only its
    phase class, and ``controls_disclosure`` at the top of the artifact says
    so in words.

    Raises :class:`FeatureClassificationRefused` with
    :data:`NO_FEATURES_DETECTED` when nothing stood above the round's own
    scatter.
    """
    if not captures:
        raise FeatureClassificationRefused(NO_ADMISSIBLE_CAPTURES, {"n_captures": 0})
    ladder = tuple(sorted(float(g) for g in (gates_ms or DEFAULT_RUNGS_MS)))
    primary = float(gate_ms)
    trusted_band_hz = (f_trusted_floor_hz(primary * 1e-3), TRUSTED_CEILING_HZ)
    grid = classification_grid()

    irs: list[np.ndarray] = []
    peaks: list[int] = []
    sample_rate: int | None = None
    for capture in captures:
        signal, rate = _read_wav(capture.wav)
        program, program_rate = _read_wav(capture.program)
        if rate != program_rate:
            raise ValueError(
                f"{capture.wav.name}: capture is {rate} Hz but its program is "
                f"{program_rate} Hz"
            )
        if sample_rate is None:
            sample_rate = rate
        elif rate != sample_rate:
            raise ValueError(
                f"{capture.wav.name}: {rate} Hz among {sample_rate} Hz captures"
            )
        # Unwindowed on purpose: the timing test needs a t=0 defined by the
        # program that was played, not by a per-capture argmax that has already
        # absorbed the offset being measured.
        ir = regularized_deconvolution_full(
            signal.astype(np.float32), program.astype(np.float32), rate
        ).astype(np.float64)
        irs.append(ir)
        peaks.append(int(np.argmax(np.abs(ir))))
    assert sample_rate is not None

    curves = np.array(
        [
            smoothed_curve(
                gate(ir, sample_rate, gate_ms=primary, peak=peak), sample_rate, grid
            )
            for ir, peak in zip(irs, peaks)
        ]
    )
    detrended = np.array([detrend(curve, grid) for curve in curves])
    band_hz = classifiable_band_hz(trusted_band_hz)
    if at is None:
        features = _detect_features(detrended, curves.mean(axis=0), grid, band_hz)
    else:
        features = sorted(
            float(fc) for fc in at if band_hz[0] <= float(fc) <= band_hz[1]
        )
    if not features:
        raise FeatureClassificationRefused(
            NO_FEATURES_DETECTED,
            {
                "n_captures": len(captures),
                "classifiable_band_hz": list(band_hz),
                "trusted_band_hz": list(trusted_band_hz),
                "stability_z": FEATURE_STABILITY_Z,
                "min_departure_db": FEATURE_MIN_DEPARTURE_DB,
                "requested": list(at) if at is not None else None,
            },
        )

    # The controls' host: the round's earliest capture, so the same IR carries
    # every known answer and each inherits this round's own noise floor.
    host_index = 0
    controls = _run_controls(
        irs[host_index],
        sample_rate,
        features,
        gate_ms=primary,
        trusted_band_hz=trusted_band_hz,
    )
    controls_ok = bool(controls["verdict"]["passes"])

    # TEST 1. Two transforms per CAPTURE — the phase gate's lead, and the
    # zero-lead window whose disagreement is reported as
    # `lead_sensitivity_us` — and every feature's excursion read off each.
    lead_by_feature: dict[str, list[dict[str, Any]]] = {
        f"{fc:.0f}": [] for fc in features
    }
    zero_by_feature: dict[str, list[float]] = {f"{fc:.0f}": [] for fc in features}
    for ir, peak in zip(irs, peaks):
        lead_ep = excess_group_delay(
            gate(
                ir, sample_rate, gate_ms=primary, lead_ms=PHASE_GATE_LEAD_MS, peak=peak
            ),
            sample_rate,
            trusted_band_hz=trusted_band_hz,
        )
        zero_ep = excess_group_delay(
            gate(ir, sample_rate, gate_ms=primary, peak=peak),
            sample_rate,
            trusted_band_hz=trusted_band_hz,
        )
        for fc in features:
            key = f"{fc:.0f}"
            lead_by_feature[key].append(egd_excursion(lead_ep, fc, trusted_band_hz))
            zero_by_feature[key].append(
                egd_excursion(zero_ep, fc, trusted_band_hz)["excursion_us"]
            )

    egd_rows: dict[str, dict[str, Any]] = {}
    for fc in features:
        key = f"{fc:.0f}"
        reads = lead_by_feature[key]
        values = np.array([r["excursion_us"] for r in reads])
        pooled = float(values.mean())
        zero_pooled = float(np.mean(zero_by_feature[key]))
        egd_rows[key] = {
            "pooled_excursion_us": pooled,
            "sd_us": float(values.std(ddof=1)) if values.size > 1 else 0.0,
            "p2p_us": float(np.mean([r["p2p_us"] for r in reads])),
            "nbhd_sd_us": float(np.mean([r["nbhd_sd_us"] for r in reads])),
            "zero_lead_pooled_us": zero_pooled,
            "lead_sensitivity_us": pooled - zero_pooled,
            "clean": all(bool(r["clean"]) for r in reads),
            "n": int(values.size),
        }

    # The feature's own pooled size and width, off the PRIMARY window — the
    # curves this function already built. Not the ladder's: the ladder's job is
    # what changes across windows, and the row's depth is what one window read.
    pooled = detrended.mean(axis=0)
    pooled_db = {f"{fc:.0f}": read_feature(pooled, grid, fc) for fc in features}
    measured_q = {f"{fc:.0f}": feature_q(pooled, grid, fc) for fc in features}

    swept, frame, ladder_poses, ladder_refusal = _sweep_ladder(
        captures, irs, peaks, sample_rate, features, ladder
    )
    timing = _timing_scatter(captures, irs, peaks, sample_rate, trusted_band_hz)

    # Feature-independent work, once per round: each pose curve's detrended
    # form, and the host IR's forward FFT — the rows loop only reads them.
    pose_detrended = [
        detrend(curve.magnitude_db, curve.freqs_hz) for curve in pose_curves
    ]
    decay_host = _DecayHost.of(irs[host_index], sample_rate)

    rows = []
    for fc in features:
        key = f"{fc:.0f}"
        gate_call = _gate_call(swept.get(key), ladder_refusal)
        row = _compose(
            fc,
            egd_rows[key],
            gate_call,
            controls["C4_pair"]["at"][key],
            pooled_db[key],
            measured_q[key],
            controls_ok=controls_ok,
            timing_available=bool(timing["available"]),
        )
        row["gate_rungs"] = gate_call["gate_rungs"]
        row["gate_sensitivity"] = gate_call["gate_sensitivity"]
        row["pose_persistence"] = _pose_persistence_block([
            _pose_reading(curve, detrended, fc, pooled_db[key] < 0)
            for curve, detrended in zip(pose_curves, pose_detrended)
        ])
        row["decay"] = {
            name: _decay_read(decay_host, band)
            for name, band in _decay_bands_hz(fc).items()
        }
        row["fdw_rungs"] = _fdw_rungs(
            irs, peaks, sample_rate, fc, pooled_db[key] < 0
        )
        # 6.11a: how many cycles of THIS feature's own frequency fit inside
        # the primary gate -- the grey-zone read research 03 names (2.5/T is
        # prudence, not a law; a feature at 2-3 cycles is where a magnitude
        # estimate is taper-biased and low-resolution). A row field, not a
        # threshold: nothing here refuses or grades on it.
        row["cycles_in_primary_gate"] = fc * primary * 1e-3
        rows.append(row)

    return {
        "schema": CLASSIFICATION_SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "thresholds": {
            "frac_nmp_min_phase": FRAC_NMP_MIN_PHASE,
            "frac_nmp_non_min_phase": FRAC_NMP_NON_MIN_PHASE,
            "z_local_flat": Z_LOCAL_FLAT,
            "sigma_growth_room_ratio": SIGMA_GROWTH_ROOM_RATIO,
            "sigma_growth_min_sigma_db": SIGMA_GROWTH_MIN_SIGMA_DB,
            "gate_delta_slack_db": GATE_DELTA_SLACK_DB,
            "centre_shift_oct": CENTRE_SHIFT_OCT,
            "feature_stability_z": FEATURE_STABILITY_Z,
            "feature_min_departure_db": FEATURE_MIN_DEPARTURE_DB,
            "feature_min_separation_oct": FEATURE_MIN_SEPARATION_OCT,
            "max_features": MAX_FEATURES,
            "decay_target_drop_db": DECAY_TARGET_DROP_DB,
        },
        "measurement": {
            "sample_rate": sample_rate,
            "gate_ms_primary": primary,
            "gate_ladder_ms": list(ladder),
            "gate_ladder_frame": frame,
            # Who each feature's pose rows ARE, once for the round. The engine
            # keys them by `pose_key` and publishes nothing else per feature,
            # because who a pose is does not vary with the bin.
            "gate_ladder_poses": ladder_poses,
            "gate_ladder_refused": ladder_refusal,
            # P1 sec 6 measured one capture's 441.6 Hz feature through both window
            # families and they disagree by 1.72 dB on the same 7->20 ms change (row
            # D, this ladder's shape, against row F, the shape it replaced). Ladder
            # numbers banked before this instrument moved onto the engine are row F
            # and are not comparable with these.
            "gate_ladder_window_changed": (
                "the ladder's window family changed with the move onto "
                "jasper.active_speaker.crossover_v2.gate_sweep; ladder numbers "
                "banked earlier were read through a full-span half-Hann tail "
                "with no lead (P1 sec 6 row F) and are not comparable with "
                "these (row D). gate_ladder_frame states this frame in full"
            ),
            "phase_gate_lead_ms": PHASE_GATE_LEAD_MS,
            "egd_window_source": {
                "kind": EGD_WINDOW_KIND,
                "gate_ms": primary,
                "lead_ms": PHASE_GATE_LEAD_MS,
            },
            "trusted_band_hz": list(trusted_band_hz),
            "classifiable_band_hz": list(band_hz),
            "magnitude_smooth_fraction": MAGNITUDE_SMOOTH_FRACTION,
            "complex_smooth_oct": COMPLEX_SMOOTH_OCT,
            "fdw_cycles": list(FDW_CYCLES),
            "fdw_taper": FDW_TAPER,
            "n_captures": len(captures),
            "phases": sorted({c.phase for c in captures}),
            "captures": [c.wav.name for c in captures],
            "features_requested": list(at) if at is not None else None,
        },
        "controls_ok": controls_ok,
        "controls_disclosure": None if controls_ok else CONTROLS_FAILED_DISCLOSURE,
        "controls": controls,
        "timing_scatter": timing,
        "pose_bank": _pose_bank_block(pose_curves),
        "rows": rows,
    }
