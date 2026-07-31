# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared acoustic-measurement kernel.

The pure measurement primitives that every JTS tuning layer reuses — room
correction, active-crossover commissioning, and the level ramp — live
here rather than under any one layer's package. Extracted verbatim from
``jasper.correction`` (which used to be their home and still consumes them); the
DSP math is unchanged.

Modules:
  - :mod:`~jasper.audio_measurement.sweep` — synchronized swept-sine (ESS)
    generation + WAV I/O.
  - :mod:`~jasper.audio_measurement.program` — the crossover conductor's
    excitation-program model (pure-data schedule dataclasses), the CHECK /
    MEASURE / VERIFY composers, and deterministic PCM rendering / WAV writing.
  - :mod:`~jasper.audio_measurement.program_analysis` — the pure
    ``(program, capture) → ProgramAnalysis`` analysis (locate + integrity +
    clock-drift + per-driver response + GCC-PHAT alignment + candidate) for the
    conductor flow.
  - :mod:`~jasper.audio_measurement.deconv` — regularized FFT deconvolution
    (impulse-response extraction) + magnitude response.
  - :mod:`~jasper.audio_measurement.gating` — impulse-response gating (first-
    reflection detection, the reflection-free window) and the low-frequency
    validity floor it implies, plus the classification ledger of early
    features that are structurally un-gateable.
  - :mod:`~jasper.audio_measurement.gate_disclosure` — the gating contract's
    other half: what the gate actually bought, priced over the intersection
    of its trusted validity range with the band the DUT radiates, and the
    single writer of the sentence that distinguishes "a reflection was found
    and removed" from "nothing was found; the window was capped".
  - :mod:`~jasper.audio_measurement.analysis` — fractional-octave smoothing,
    log resampling, spatial averaging, deviation metrics.
  - :mod:`~jasper.audio_measurement.calibration` — measurement-mic calibration
    registry, parser, and vendor lookup.
  - :mod:`~jasper.audio_measurement.quality` — capture-quality assessment
    (``assess_capture``) driven by a threshold profile.
  - :mod:`~jasper.audio_measurement.quality_model` — the parameterized
    :class:`QualityModel` profiles (``ROOM`` / ``DRIVER`` / ``RAMP``) that
    replace the previously-forked capture-quality constants.
  - :mod:`~jasper.audio_measurement.snr_policy` — the band-specific,
    decision-class-split SNR gate (``band_levels_dbfs`` FFT band power +
    ``band_snr_verdicts`` magnitude/alignment verdicts) shared by room
    correction and active-crossover commissioning.
  - :mod:`~jasper.audio_measurement.ramp` — the settle-based level-match
    ``RampController`` (muted-at-floor → audible gain ramp → audible-evidence
    confirmation) that drives main-volume-affecting playback.
  - :mod:`~jasper.audio_measurement.excitation_admission` — the pure,
    identity-bound allow/refuse contract for requested frequency band,
    effective peak, duration, repeats, and current protection evidence.
  - :mod:`~jasper.audio_measurement.excitation_artifacts` — fail-closed,
    canonical admission persistence plus independent playback re-admission;
    existing and historical evidence cannot be promoted into authority.
  - :mod:`~jasper.audio_measurement.admitted_playback` — guarded composition of
    a caller-issued fresh protection readback, playback-role re-admission, and
    the policy-free WAV emitter.
  - :mod:`~jasper.audio_measurement.bundles` — the neutral artifact-manifest
    writer/reader shared by feature-owned Room and Active evidence bundles.
  - :mod:`~jasper.audio_measurement.playback` — production WAV/tone emission,
    cancellation, and process cleanup after feature-owned admission.
  - :mod:`~jasper.audio_measurement.null_walk` — the geometry-bounded,
    timing-locked delay search contract and bounded coarse-plus-refinement
    schedule shared by active-speaker and bass alignment; it consumes repeated
    gated null depths, never browser arrival times.
  - :mod:`~jasper.audio_measurement.delay_graph` — the pure, typed graph-content
    proof that a null-walk DSP candidate differs from a graph-proven,
    zero-relative predecessor by only one bounded delay lane; each host supplies
    its exact topology channel set and emitter-owned lane identity, while
    orchestration separately owns read-back freshness and transaction authority.
  - :mod:`~jasper.audio_measurement.spatial_combine` — the spatial
    multi-capture combiner (power-mean cloud average), the
    power-mean-vs-median interference honesty screen, the per-capture
    cepstral echo detector, and the ``geometry.locked`` verdict.
  - :mod:`~jasper.audio_measurement.interference_nulls` — the orthogonal
    honesty gate the screen above is structurally blind to: identifying
    position-invariant interference nulls by fitting a single-delay null
    ladder and corroborating it against a measured arrival, so an
    uncorrectable comb is excluded with its delay and reflection ratio
    recorded as the reason.
  - :mod:`~jasper.audio_measurement.frame_fit` — the FRAME between two
    magnitude curves the flow is about to difference: a least-squares
    ``offset + tilt·log2(f)``, two scalars, fitted and DISCLOSED so a
    cross-instrument comparison can no longer report instrument tilt as model
    error. Serves the **diagnose** stage; owns the frame model and the typed
    disclosure record, and owns no band, no threshold, and no verdict.

Layer-specific logic — PEQ design, targets, correction strategy, the
active-speaker verdicts, the web flows — stays in its owning package and
consumes this kernel.
"""
