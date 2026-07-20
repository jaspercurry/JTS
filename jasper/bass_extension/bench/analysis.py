# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Campaign verdicts, composed from the existing measurement kernels.

The runner does not reinvent measurement math. This module composes the
existing kernels — ``assess_capture`` (capture quality), ``thd_curve`` /
``compression_curve`` / ``tracking_error_db`` (signal analysis), and the
``snr_policy`` gate — into the pass/fail verdicts the frozen bundle records, and
adds the three analyses the protocol names but no kernel owns: the isolated
digital-transfer SHA match, the paired sweep-transparency comparison, and the
sustain sag / corner-shift checks. Every threshold comes from the selected
:class:`~jasper.bass_extension.targets.MarginPolicy`; this module invents no
hardware-safety number.

Pure and deterministic: it operates on arrays and metrics, performs no I/O, and
opens no device. Only the *live acoustic capture* upstream of it is mocked in
tests — the math here is real.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from jasper.audio_measurement.analysis import (
    compression_curve,
    thd_curve,
    tracking_error_db,
)
from jasper.audio_measurement.quality import CaptureQuality, assess_capture
from jasper.bass_extension.targets import MarginPolicy

Verdict = str  # "pass" | "fail"

_PASS = "pass"
_FAIL = "fail"


def sample_peak_dbfs(samples: np.ndarray) -> float:
    """Instantaneous float sample-peak dBFS re unity full scale.

    This is the frozen detector reference — the pre/post-limiter tap value.
    A silent buffer floors at -120 dBFS.
    """

    array = np.abs(np.asarray(samples, dtype=np.float64))
    peak = float(np.max(array)) if array.size else 0.0
    if peak <= 0.0:
        return -120.0
    return float(20.0 * np.log10(peak))


def digital_clamp_passed(pre_limiter_peak_dbfs: float, margin: MarginPolicy) -> bool:
    """True iff the pre-limiter peak keeps the policy's digital headroom.

    The arithmetic-headroom eligibility check (the merged Wave-1 digital
    margin): the pre-limiter sample peak must stay at least
    ``margin.digital_margin_db`` below unity full scale.
    """

    return pre_limiter_peak_dbfs <= -float(margin.digital_margin_db)


def transfer_match(
    *,
    deployed_sha256: str,
    deployed_byte_size: int,
    reference_sha256: str,
    reference_byte_size: int,
) -> Verdict:
    """Verdict for the isolated ``digital_transfer_probe``.

    Per the protocol, matching payload SHA and byte size between the deployed
    and reference post-limiter artifacts establishes the deployed transfer
    binding.
    """

    matched = (
        deployed_sha256 == reference_sha256
        and deployed_byte_size == reference_byte_size
    )
    return _PASS if matched else _FAIL


@dataclass(frozen=True, slots=True)
class SweepVerdicts:
    quality_verdict: Verdict
    protection_verdict: Verdict
    thd_max: float
    compression_max_db: float
    tracking_rms_db: float
    tracking_max_db: float
    repeat_spread_db: float
    snr_db: float


def assess_sweep(
    *,
    captured: np.ndarray,
    sample_rate: int,
    sweep_n_samples: int,
    has_mic_calibration: bool,
    fund_freqs: np.ndarray,
    fund_db: np.ndarray,
    harmonics: Mapping[int, tuple[np.ndarray, np.ndarray]],
    compression_rungs: Sequence[tuple[float, tuple[float, ...]]],
    tracking_freqs: np.ndarray,
    tracking_measured_db: np.ndarray,
    tracking_predicted_db: np.ndarray,
    repeat_band_levels_db: Sequence[float],
    snr_db: float,
    band: tuple[float, float],
    margin: MarginPolicy,
    min_snr_db: float,
) -> SweepVerdicts:
    """Compose the kernels + MarginPolicy gates into sweep verdicts."""

    quality: CaptureQuality = assess_capture(
        np.asarray(captured, dtype=np.float64),
        sample_rate=sample_rate,
        expected_sample_rate=sample_rate,
        sweep_n_samples=sweep_n_samples,
        has_mic_calibration=has_mic_calibration,
    )

    _thd_freqs, thd_values = thd_curve(fund_freqs, fund_db, dict(harmonics), band=band)
    thd_max = float(np.max(thd_values)) if len(thd_values) else 0.0

    compression = compression_curve(list(compression_rungs))
    compression_max = (
        max((abs(value) for rung in compression for value in rung), default=0.0)
    )

    tracking_rms, tracking_max = tracking_error_db(
        tracking_freqs, tracking_measured_db, tracking_predicted_db, band
    )

    levels = [float(value) for value in repeat_band_levels_db]
    repeat_spread = (max(levels) - min(levels)) if levels else 0.0

    quality_ok = (
        not quality.failed
        and thd_max <= float(margin.thd_fail_ratio)
        and repeat_spread <= 2.0
        and snr_db >= min_snr_db
    )
    protection_ok = compression_max <= float(margin.compression_fail_db)

    return SweepVerdicts(
        quality_verdict=_PASS if quality_ok else _FAIL,
        protection_verdict=_PASS if protection_ok else _FAIL,
        thd_max=thd_max,
        compression_max_db=float(compression_max),
        tracking_rms_db=float(tracking_rms),
        tracking_max_db=float(tracking_max),
        repeat_spread_db=float(repeat_spread),
        snr_db=float(snr_db),
    )


@dataclass(frozen=True, slots=True)
class SustainVerdicts:
    quality_verdict: Verdict
    protection_verdict: Verdict
    sag_db: float
    fc_shift_pct: float


def assess_sustain(
    *,
    start_level_db: float,
    end_level_db: float,
    start_corner_hz: float,
    end_corner_hz: float,
    snr_db: float,
    margin: MarginPolicy,
    min_snr_db: float,
) -> SustainVerdicts:
    """Sag + corner-shift verdicts for the sustain stress hold.

    Sag is how far the level drooped from the start to the end of the hold;
    corner-shift is the fractional shift of the low corner over the hold. Both
    are gated by the selected margin policy.
    """

    sag_db = float(start_level_db) - float(end_level_db)
    if start_corner_hz <= 0.0:
        fc_shift_pct = float("inf")
    else:
        fc_shift_pct = abs(float(end_corner_hz) - float(start_corner_hz)) / float(
            start_corner_hz
        ) * 100.0

    quality_ok = snr_db >= min_snr_db
    protection_ok = (
        sag_db <= float(margin.sustain_sag_fail_db)
        and fc_shift_pct <= float(margin.sustain_fc_shift_fail_pct)
    )
    return SustainVerdicts(
        quality_verdict=_PASS if quality_ok else _FAIL,
        protection_verdict=_PASS if protection_ok else _FAIL,
        sag_db=sag_db,
        fc_shift_pct=fc_shift_pct,
    )


def assess_transparency(
    *,
    freqs: np.ndarray,
    candidate_response_db: np.ndarray,
    reference_response_db: np.ndarray,
    band: tuple[float, float],
    max_tracking_rms_db: float,
) -> tuple[Verdict, float, float]:
    """Paired candidate-vs-reference transparency verdict.

    The candidate limiter is transparent when the candidate-graph response
    tracks the reference (baseline-limiter) response within the transparency
    policy's RMS bound over the band. Returns ``(verdict, rms_db, max_db)``.
    """

    rms_db, max_db = tracking_error_db(
        freqs, candidate_response_db, reference_response_db, band
    )
    verdict = _PASS if rms_db <= float(max_tracking_rms_db) else _FAIL
    return verdict, float(rms_db), float(max_db)
