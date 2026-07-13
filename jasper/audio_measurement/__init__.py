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
  - :mod:`~jasper.audio_measurement.deconv` — regularized FFT deconvolution
    (impulse-response extraction) + magnitude response.
  - :mod:`~jasper.audio_measurement.gating` — impulse-response gating (first-
    reflection detection, the reflection-free window) and the low-frequency
    validity floor it implies.
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
  - :mod:`~jasper.audio_measurement.bundles` — the neutral artifact-manifest
    writer/reader shared by feature-owned Room and Active evidence bundles.
  - :mod:`~jasper.audio_measurement.null_walk` — the geometry-bounded,
    timing-locked delay search contract shared by active-speaker and bass
    alignment; it consumes repeated gated null depths, never browser arrival
    times.
  - :mod:`~jasper.audio_measurement.delay_graph` — the pure, typed graph-content
    proof that a null-walk DSP candidate differs from a graph-proven,
    zero-relative predecessor by only one bounded delay lane; each host supplies
    its exact topology channel set and emitter-owned lane identity, while
    orchestration separately owns read-back freshness and transaction authority.

Layer-specific logic — PEQ design, targets, correction strategy, the
active-speaker verdicts, the web flows — stays in its owning package and
consumes this kernel.
"""
