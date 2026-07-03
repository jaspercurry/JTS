# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared acoustic-measurement kernel.

The pure measurement primitives that every JTS tuning layer reuses — room
correction, active-crossover commissioning, and the upcoming level ramp — live
here rather than under any one layer's package. Extracted verbatim from
``jasper.correction`` (which used to be their home and still consumes them); the
DSP math is unchanged.

Modules:
  - :mod:`~jasper.audio_measurement.sweep` — synchronized swept-sine (ESS)
    generation + WAV I/O.
  - :mod:`~jasper.audio_measurement.deconv` — regularized FFT deconvolution
    (impulse-response extraction) + magnitude response.
  - :mod:`~jasper.audio_measurement.analysis` — fractional-octave smoothing,
    log resampling, spatial averaging, deviation metrics.
  - :mod:`~jasper.audio_measurement.calibration` — measurement-mic calibration
    registry, parser, and vendor lookup.
  - :mod:`~jasper.audio_measurement.quality` — capture-quality assessment
    (``assess_capture``) driven by a threshold profile.
  - :mod:`~jasper.audio_measurement.quality_model` — the parameterized
    :class:`QualityModel` profiles (``ROOM`` / ``DRIVER`` / ``RAMP``) that
    replace the previously-forked capture-quality constants.

Layer-specific logic — PEQ design, targets, correction strategy, the
active-speaker verdicts, the web flows — stays in its owning package and
consumes this kernel.
"""
