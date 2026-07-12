# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Parameterized capture-quality thresholds shared across tuning layers.

Historically the capture-quality thresholds were forked constants living in
whichever module happened to consume them:

* room correction (``quality.py``): ``PEAK_TOO_LOW_DBFS``, ``RMS_TOO_LOW_DBFS``;
  (``acoustic_quality.py``): ``SNR_OK_DB``, ``SNR_WARN_DB``;
* active-crossover driver checks (``active_speaker/driver_acoustics.py``):
  ``SILENT_PEAK_DBFS``, ``DEFAULT_NULL_THRESHOLD_DB``, ``OVERLAP_MIN_BINS``.

Now that all three tuning layers (room correction, active-crossover
commissioning, and the upcoming level ramp) reuse ONE measurement kernel, those
forked constants become *data*: a :class:`QualityModel` holds them and the
shared assess/verdict machinery reads a profile. The values are UNCHANGED from
the pre-extraction constants — this is relocation into data, not retuning.

Three profiles ship:

* :data:`ROOM` — the room-correction listening-position measurement thresholds.
* :data:`DRIVER` — the active-crossover per-driver / summed near-field thresholds.
  Its capture-quality fields (``peak_too_low_dbfs`` / ``rms_too_low_dbfs``) are
  deliberately IDENTICAL to :data:`ROOM`'s, because the driver capture path
  already called room correction's ``assess_capture`` verbatim; only the
  driver-specific *verdict* fields (``silent_peak_dbfs``, ``null_threshold_db``,
  ``overlap_min_bins``) carried distinct driver values.
* :data:`RAMP` — the level-match ramp (P2). It shares :data:`ROOM`'s *structural*
  digital-full-scale facts (``dbfs_floor``, ``clip_abs_threshold``,
  ``clip_fraction_fail``) because those are the same facts the phone's
  ``level-events.js`` and the kernel's clip detection agree on, and it carries a
  ``peak_too_low_dbfs`` / ``rms_too_low_dbfs`` pair for parity with the other
  layers. The ramp's *live* tuning — the safe target window, the
  ``noise_floor + trust_margin`` trust floor, the stop-ahead / settle / confirm
  cadence, and the drift thresholds — deliberately does NOT live here: it lives on
  :class:`~jasper.audio_measurement.ramp.MeasurementRamp` (a validated,
  overshoot-guarded config with its own env knobs), because those are ramp
  control-loop parameters, not capture-quality gates read by ``assess_capture``.
  So ``RAMP`` intentionally equals ``ROOM`` today; the ramp's hardware-gated
  numbers (H1) are tuned on ``MeasurementRamp``, not on this profile.

The structural clip / dBFS-floor knobs (``clip_abs_threshold``,
``clip_fraction_fail``, ``dbfs_floor``) are shared by every profile — they are
digital-full-scale facts, not per-layer tuning — but they live on the model too
so a profile is a complete, self-describing description of one layer's gate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityModel:
    """One tuning layer's capture-quality + verdict thresholds.

    Fields are grouped by which machinery reads them:

    Structural (shared, digital-full-scale facts — not per-layer tuning):
      dbfs_floor: dBFS value substituted for silence / non-finite input so a
        deep null does not log as ``-inf``.
      clip_abs_threshold: absolute sample magnitude counted as "at full scale".
      clip_fraction_fail: fraction of clipped samples that FAILS a capture
        (below it, any clipped sample is a warning, not a failure).

    Capture-quality gate (read by :func:`quality.assess_capture`):
      peak_too_low_dbfs: capture peak below this warns of poor SNR.
      rms_too_low_dbfs: capture RMS below this warns of a noise-dominated room.

    SNR trust (read by the acoustic-quality report builder):
      snr_ok_db: at/above this the capture SNR is "high" confidence.
      snr_warn_db: below this the capture SNR is "low" (a warning); between the
        two it is "medium" / marginal.

    Alignment SNR trust (read by
    :mod:`~jasper.audio_measurement.snr_policy` for null/alignment decisions —
    reverse-polarity null depth, the delay walk — which need far more SNR
    headroom than a magnitude/trim reading; see "Level control and SNR" in
    docs/active-crossover-information-design.md):
      alignment_snr_ok_db: at/above this, the overlap-band SNR is trusted for
        a polarity/delay decision. Deliberately higher than snr_ok_db — a
        null of depth D is provably that deep only with roughly
        D + null_cap_margin_db of SNR in the overlap band.
      null_cap_margin_db: the "+10 dB" in "a null of depth D needs D + 10 dB
        SNR to be measurable". A measured null deeper than
        (overlap SNR - this margin) is reported capped at that ceiling
        rather than at its raw measured depth.

    Driver verdict logic (read by the active-crossover driver analyzers; the
    room and ramp profiles carry values but do not consume them):
      silent_peak_dbfs: a per-driver capture whose peak is at/below this is the
        ``silent`` verdict.
      null_threshold_db: a crossover null at least this deep is "present" (a
        problem for an in-phase capture, the pass signal for a reverse-polarity
        capture).
      overlap_min_bins: an overlap-band level needs at least this many FFT bins
        for a stable mean, else the reading is marked unusable.
    """

    # --- structural (shared) ---
    dbfs_floor: float = -120.0
    clip_abs_threshold: float = 0.999
    clip_fraction_fail: float = 1e-4

    # --- capture-quality gate ---
    peak_too_low_dbfs: float = -45.0
    rms_too_low_dbfs: float = -65.0

    # --- SNR trust ---
    snr_ok_db: float = 25.0
    snr_warn_db: float = 20.0

    # --- alignment SNR trust ---
    alignment_snr_ok_db: float = 35.0
    null_cap_margin_db: float = 10.0

    # --- driver verdict logic ---
    silent_peak_dbfs: float = -45.0
    null_threshold_db: float = 6.0
    overlap_min_bins: int = 4


# Room correction (listening-position). Values verbatim from the pre-extraction
# constants: quality.PEAK_TOO_LOW_DBFS / RMS_TOO_LOW_DBFS and
# acoustic_quality.SNR_OK_DB / SNR_WARN_DB. The driver-verdict fields carry the
# dataclass defaults (unused by the room flow).
ROOM = QualityModel()

# Active-crossover driver checks. Capture-quality + SNR fields match ROOM (the
# driver capture path already used room correction's assess_capture verbatim);
# the verdict fields are the driver_acoustics constants
# (SILENT_PEAK_DBFS / DEFAULT_NULL_THRESHOLD_DB / OVERLAP_MIN_BINS).
DRIVER = QualityModel(
    silent_peak_dbfs=-45.0,
    null_threshold_db=6.0,
    overlap_min_bins=4,
)

# Level ramp (P2 placeholder). Reuses ROOM's values until the ramp's real
# SNR-window / stop-threshold tuning is derived; see the module docstring.
RAMP = QualityModel()
