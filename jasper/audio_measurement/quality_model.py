# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Parameterized capture-quality thresholds shared across tuning layers.

A :class:`QualityModel` holds one layer's gate as data; three profiles ship —
:data:`ROOM` (listening-position), :data:`DRIVER` (per-driver / summed
near-field) and :data:`RAMP` (level match). All three carry equal values today:
the driver capture path already used room correction's ``assess_capture``
verbatim, and the driver-verdict fields coincide with the dataclass defaults.
The ramp's live control-loop tuning is NOT here — it lives on
:class:`~jasper.audio_measurement.ramp.MeasurementRamp`, because those are not
gates ``assess_capture`` reads.

This module also owns the WORDS those thresholds are reported in, so the
numbers and the vocabulary answering one question keep one owner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# --------------------------------------------------------------------------
# Verdict vocabulary — one set of words per question.
# --------------------------------------------------------------------------
#
# Three aliases because they answer three questions; a surface picks the one
# whose question it answers:
#
#   Severity    — "how bad is this ONE finding?"        info < warn < fail
#   ReportLevel — "how does the WHOLE report roll up?"    ok < warn < fail
#   TrustLevel  — "how much do I trust this READING?"    low < medium < high
#
# Severity and ReportLevel share two of three words and are still distinct: a
# report with no findings rolls up ``ok``, which no single finding can carry,
# and a finding can be ``info``, which no report rolls up to.
#
# NOT in this vocabulary, deliberately: ``snr_policy``'s per-band
# ``ok``/``reduced``/``insufficient``/``unknown`` rank. That is a REFUSAL — it
# carries a ``shortfall_db`` and is scoped per decision class, so the same
# capture is magnitude-``ok`` and alignment-``insufficient``. Do not unify it
# into :data:`TrustLevel`.

#: How bad ONE finding is. Widest of the three: a surface that never emits
#: ``info`` still speaks this vocabulary.
Severity = Literal["info", "warn", "fail"]

#: How a WHOLE report rolls up, once its findings are reduced.
ReportLevel = Literal["ok", "warn", "fail"]

#: How much a reported READING can be trusted — a measured number, or a
#: classifier's verdict about one. ``medium`` is spelled in full.
TrustLevel = Literal["high", "medium", "low"]

#: The no-evidence slot beside :data:`TrustLevel`. Deliberately not a member of
#: it: "we did not measure this" is not a low reading.
TRUST_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class QualityModel:
    """One tuning layer's capture-quality + verdict thresholds.

    Structural (shared digital-full-scale facts, not per-layer tuning):
      dbfs_floor: dBFS substituted for silence / non-finite input so a deep
        null does not log as ``-inf``.
      clip_abs_threshold: absolute sample magnitude counted as "at full scale".
      clip_fraction_fail: fraction of clipped samples that FAILS a capture;
        below it, any clipped sample is a warning.

    Capture-quality gate (read by :func:`quality.assess_capture`):
      peak_too_low_dbfs: capture peak below this warns of poor SNR.
      rms_too_low_dbfs: capture RMS below this warns of a noise-dominated room.

    SNR trust (read by the acoustic-quality report builder):
      snr_ok_db: at/above this the capture SNR is "high" confidence.
      snr_warn_db: below this it is "low"; between the two, "medium".

    Alignment SNR trust (read by :mod:`~jasper.audio_measurement.snr_policy`
    for null/alignment decisions; see "Level control and SNR" in
    docs/active-crossover-information-design.md):
      alignment_snr_ok_db: overlap-band SNR at/above which a polarity/delay
        decision is trusted. Higher than snr_ok_db because a null of depth D is
        provably that deep only with roughly D + null_cap_margin_db of SNR.
      null_cap_margin_db: that margin. A measured null deeper than
        (overlap SNR - this) is reported capped at that ceiling.

    Driver verdict logic (read by the active-crossover driver analyzers; the
    room and ramp profiles carry values but do not consume them):
      silent_peak_dbfs: a per-driver capture peaking at/below this is
        ``silent``.
      null_threshold_db: a crossover null at least this deep is "present".
      overlap_min_bins: minimum FFT bins for a stable overlap-band mean; below
        it the reading is marked unusable.
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


# Room correction (listening-position). The driver-verdict fields carry the
# dataclass defaults and are unused by the room flow.
ROOM = QualityModel()

# Active-crossover driver checks. The three verdict fields are spelled out
# rather than defaulted because they are the driver_acoustics values this
# profile owns; they happen to equal the dataclass defaults today.
DRIVER = QualityModel(
    silent_peak_dbfs=-45.0,
    null_threshold_db=6.0,
    overlap_min_bins=4,
)

# Level ramp. Reuses ROOM's values; the ramp's own tuning lives on
# MeasurementRamp — see the module docstring.
RAMP = QualityModel()
