# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision-class + band-specific SNR gate — the split SNR policy.

docs/active-crossover-information-design.md ("Level control and SNR") splits
SNR trust by what the number is used FOR, not by one blanket threshold:

* **Magnitude / trim decisions** (a driver's level, its overlap-band trim) are
  usable well before an alignment decision is: 25 dB SNR is the confident
  floor, 20-25 dB is a reduced-confidence result, and below 20 dB the capture
  is refused with a report of how many dB are missing.
* **Null / alignment decisions** (reverse-polarity depth, the delay walk) need
  far more: a null of depth D cannot be measured with less than about D + 10
  dB of SNR in the overlap band, so alignment evidence needs roughly 35 dB
  there — and a plain scalar noise-floor reading (e.g. a 1 kHz tone level) is
  explicitly NOT sufficient evidence for that call; only a real per-band
  noise measurement is.

This module is the single place that turns raw per-band signal/noise levels
into that split, per-band verdict. It has two halves:

* :func:`band_levels_dbfs` — the FFT band-power estimator, relocated verbatim
  from ``jasper.correction.session._band_levels_dbfs`` (which now delegates
  here with its own band table) so room correction and active-crossover
  commissioning share one implementation instead of two forks.
* :func:`band_snr_verdicts` — the decision-class-aware verdict builder.
  ``jasper.active_speaker.driver_acoustics`` (per-driver and summed-crossover
  analysis) is the first consumer; room correction does not call this yet.

Pure-data / pure-function: no I/O, no product policy, no CamillaDSP or
playback awareness — mirrors the "one measurement-quality model with
consumer-specific policy values" DRY invariant in the design doc. numpy is a
module-level import here (``band_levels_dbfs`` needs it for the FFT); callers
that stay numpy/scipy-free until a measurement actually runs (e.g. the
socket-activated ``/sound/`` wizard via
``jasper.active_speaker.driver_acoustics``) import this module LAZILY inside a
function, not at their own module top.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import deconv
from jasper.audio_measurement.quality_model import QualityModel

# Six bands spanning the trusted phone-mic analysis window. The first four are
# byte-identical to jasper.correction.session.SNR_BANDS_HZ (room correction's
# shipped table, pinned by test_audio_measurement_snr_policy.py so the two
# never drift apart). "mid" and "treble" extend the table up through a
# tweeter's crossover range, which room correction (a sub-1 kHz PEQ concern)
# never needed.
CROSSOVER_SNR_BANDS_HZ: tuple[tuple[str, float, float], ...] = (
    ("sub_bass", 20.0, 80.0),
    ("bass", 80.0, 160.0),
    ("upper_bass", 160.0, 350.0),
    ("transition", 350.0, 1000.0),
    ("mid", 1000.0, 4000.0),
    ("treble", 4000.0, 12000.0),
)

DBFS_FLOOR = -120.0

# Decision-class vocabulary for band_snr_verdicts.
DECISION_CLASS_MAGNITUDE = "magnitude"
DECISION_CLASS_ALIGNMENT = "alignment"
DECISION_CLASSES = frozenset({DECISION_CLASS_MAGNITUDE, DECISION_CLASS_ALIGNMENT})

_ALIGNMENT_BAND_METHODS = frozenset({
    "fft_band_power_difference",
    "deconvolved_band_difference",
})

# Per-band verdict severity, worst last. Used to reduce a list of per-band
# verdicts to a single "worst" verdict for a frequency window. "unknown" is
# deliberately absent — it carries no evidence, so it never outranks a real
# verdict (see worst_band_verdict).
_VERDICT_RANK: dict[str, int] = {"ok": 0, "reduced": 1, "insufficient": 2}


def _dbfs(value: float) -> float:
    if value <= 0 or not np.isfinite(value):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(value))


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def band_levels_dbfs(
    samples: np.ndarray,
    sample_rate: int,
    bands: Sequence[tuple[str, float, float]],
) -> list[dict[str, Any]]:
    """FFT band-power levels of ``samples``, one entry per band that has bins.

    Moved verbatim from ``jasper.correction.session._band_levels_dbfs``
    (which now delegates here, passing its own ``SNR_BANDS_HZ``) — same
    Hanning window, same power average, same rounding — so room correction
    and active-crossover commissioning read one implementation instead of two
    forks. Bounds the FFT input the same way
    :func:`~jasper.audio_measurement.deconv.deconvolve` does
    (``deconv.cap_capture_length``), since callers pass uploaded WAVs
    (ambient noise, capture band levels) limited only by the HTTP body cap —
    unbounded would otherwise drive this rfft + hanning to OOM on the 1 GB Pi.
    """
    if samples.ndim != 1 or sample_rate <= 0 or samples.size < 8:
        return []
    samples = deconv.cap_capture_length(samples, sweep_len=0, sample_rate=sample_rate)
    x = np.asarray(samples, dtype=np.float64)
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x * window)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    out: list[dict[str, Any]] = []
    for band_id, low, high in bands:
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            continue
        rms_like = math.sqrt(float(np.mean(power[mask]))) / max(1, x.size)
        out.append({
            "band_id": band_id,
            "band_hz": [low, high],
            "level_dbfs": round(_dbfs(rms_like), 2),
        })
    return out


def ambient_band_report(
    samples: np.ndarray,
    sample_rate: int,
    bands: Sequence[tuple[str, float, float]] = CROSSOVER_SNR_BANDS_HZ,
) -> dict[str, Any]:
    """Return a non-stationary-robust ambient report.

    The stored ambient window is split into one-second frames and each band's
    95th percentile is retained.  This deliberately does not select one lucky
    quiet instant: a fan, furnace, or traffic burst that is present during the
    commissioning window remains part of the noise evidence.
    """

    x = np.asarray(samples, dtype=np.float64)
    if sample_rate <= 0 or x.size < 8:
        return {"schema_version": 1, "duration_s": 0.0, "bands": []}
    frame_len = sample_rate
    frames = [
        x[start:start + frame_len]
        for start in range(0, x.size - frame_len + 1, frame_len)
    ] or [x]
    per_frame = [band_levels_dbfs(frame, sample_rate, bands) for frame in frames]
    out: list[dict[str, Any]] = []
    for band_id, low, high in bands:
        levels = [
            float(entry["level_dbfs"])
            for frame in per_frame
            for entry in frame
            if entry.get("band_id") == band_id
        ]
        if levels:
            out.append({
                "band_id": band_id,
                "band_hz": [low, high],
                "level_dbfs": round(float(np.percentile(levels, 95)), 2),
            })
    return {
        "schema_version": 1,
        "duration_s": round(x.size / sample_rate, 3),
        "method": "one_second_p95",
        "bands": out,
    }


def magnitude_band_levels(
    frequencies_hz: np.ndarray,
    magnitude_db: np.ndarray,
    bands: Sequence[tuple[str, float, float]] = CROSSOVER_SNR_BANDS_HZ,
) -> list[dict[str, Any]]:
    """Power-mean levels for a deconvolved magnitude response."""

    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    mag = np.asarray(magnitude_db, dtype=np.float64)
    out: list[dict[str, Any]] = []
    for band_id, low, high in bands:
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            continue
        power = np.power(10.0, mag[mask] / 10.0)
        level = 10.0 * math.log10(max(float(np.mean(power)), 1e-12))
        out.append({
            "band_id": band_id,
            "band_hz": [low, high],
            "level_dbfs": round(level, 2),
        })
    return out


def deconvolved_ambient_report(
    ambient_samples: np.ndarray,
    sample_rate: int,
    sweep_meta: Mapping[str, Any],
    *,
    calibration: Any = None,
    bands: Sequence[tuple[str, float, float]] = CROSSOVER_SNR_BANDS_HZ,
    capture_length_samples: int | None = None,
) -> dict[str, Any]:
    """Transform stored ambient through the played sweep's deconvolution.

    Signal and noise must be compared in the same domain.  The reference is
    regenerated from the exact played metadata, and the ambient window must be
    at least as long as that reference.  When the full recording length is
    supplied, the ambient input is zero-padded to that length so signal and
    noise use the same FFT grid. Per-band p95-vs-mean deltas from the full
    ambient window are then applied to the deconvolved floor so the result
    retains non-stationary noise rather than an optimistic average.
    """

    from jasper.audio_measurement import calibration as calibration_mod
    from jasper.audio_measurement import sweep as sweep_mod

    expected_rate = int(sweep_meta["sample_rate"])
    if sample_rate != expected_rate:
        raise ValueError("ambient and sweep sample rates differ")
    reference, _ = sweep_mod.synchronized_swept_sine(
        f1=float(sweep_meta["f1"]),
        f2=float(sweep_meta["f2"]),
        duration_approx_s=float(sweep_meta["duration_s"]),
        sample_rate=expected_rate,
        amplitude_dbfs=float(sweep_meta["amplitude_dbfs"]),
    )
    ambient = np.asarray(ambient_samples, dtype=np.float64)
    if ambient.size < reference.size:
        raise ValueError("stored ambient window is shorter than the played sweep")
    matched = ambient[:reference.size]
    deconvolution_input = matched
    if capture_length_samples is not None:
        target_length = max(reference.size, int(capture_length_samples))
        if target_length > matched.size:
            deconvolution_input = np.pad(
                matched, (0, target_length - matched.size)
            )
    ir = deconv.deconvolve(
        deconvolution_input, reference.astype(np.float64), expected_rate
    )
    freqs, magnitude = deconv.magnitude_response(ir, expected_rate, normalize=False)
    if calibration is not None:
        magnitude = calibration_mod.apply_calibration_curve(
            freqs, magnitude, calibration
        )
    deconvolved = magnitude_band_levels(freqs, magnitude, bands)

    robust = ambient_band_report(ambient, expected_rate, bands)["bands"]
    mean_raw = band_levels_dbfs(matched, expected_rate, bands)
    robust_by_id = {entry["band_id"]: entry for entry in robust}
    mean_by_id = {entry["band_id"]: entry for entry in mean_raw}
    adjusted: list[dict[str, Any]] = []
    for entry in deconvolved:
        band_id = entry["band_id"]
        robust_entry = robust_by_id.get(band_id)
        mean_entry = mean_by_id.get(band_id)
        adjustment = 0.0
        if robust_entry is not None and mean_entry is not None:
            adjustment = float(robust_entry["level_dbfs"]) - float(
                mean_entry["level_dbfs"]
            )
        adjusted.append({
            **entry,
            "level_dbfs": round(float(entry["level_dbfs"]) + adjustment, 2),
        })
    return {
        "schema_version": 1,
        "domain": "deconvolved",
        "method": "deconvolved_band_difference",
        "ambient_duration_s": round(ambient.size / expected_rate, 3),
        "bands": adjusted,
    }


def unwrap_noise_report(
    report: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None,
) -> tuple[str, Sequence[Mapping[str, Any]] | None]:
    """Normalize legacy bare-band and domain-tagged ambient reports."""

    if isinstance(report, Mapping):
        rows = report.get("bands")
        return str(report.get("domain") or "raw"), (
            rows if isinstance(rows, (list, tuple)) else None
        )
    return "raw", report


def _band_overlaps(band_hz: Any, lo_hz: float, hi_hz: float) -> bool:
    if not isinstance(band_hz, (list, tuple)) or len(band_hz) != 2:
        return False
    try:
        b_lo, b_hi = float(band_hz[0]), float(band_hz[1])
    except (TypeError, ValueError):
        return False
    return b_hi > lo_hz and b_lo < hi_hz


def worst_band_verdict(
    bands: Sequence[Mapping[str, Any]] | None,
    lo_hz: float,
    hi_hz: float,
) -> dict[str, Any] | None:
    """The single worst-verdict entry in ``bands`` overlapping ``[lo_hz, hi_hz]``.

    "Worst" ranks insufficient > reduced > ok; an entry whose ``verdict`` is
    "unknown" (or anything unrecognized) never wins — it carries no evidence,
    so it can neither veto nor clear the window. Returns ``None`` when no
    *evidenced* band overlaps the window (nothing overlaps, or everything
    that does is "unknown") — callers read that as "unknown" for the whole
    window: a partial-pass rule shared by :func:`band_snr_verdicts` (reducing
    over its own ``relevant_hz``) and
    ``jasper.active_speaker.driver_acoustics`` (reducing over one overlap-band
    Fc window) — one rule, not two.
    """
    worst: dict[str, Any] | None = None
    for band in bands or ():
        if not isinstance(band, Mapping):
            continue
        if not _band_overlaps(band.get("band_hz"), lo_hz, hi_hz):
            continue
        verdict = band.get("verdict")
        if verdict not in _VERDICT_RANK:
            continue
        if worst is None or _VERDICT_RANK[verdict] > _VERDICT_RANK[worst["verdict"]]:
            worst = dict(band)
    return worst


def _band_verdict(
    *,
    decision_class: str,
    method: str,
    estimated_snr_db: float | None,
    model: QualityModel,
) -> tuple[str, float | None]:
    """(verdict, raw shortfall_db) for one band's estimated SNR.

    ``shortfall_db`` is unrounded here; :func:`band_snr_verdicts` rounds it
    (matching ``estimated_snr_db``'s rounding) at the point it builds the
    band entry.
    """
    if estimated_snr_db is None:
        return "unknown", None
    if decision_class == DECISION_CLASS_ALIGNMENT:
        # A scalar (or missing) noise floor is not sufficient evidence for a
        # null/alignment call, even when a number was computable — degrade to
        # "unknown" rather than gate on an untrustworthy figure ("Level
        # control and SNR": "a 1 kHz scalar level is not sufficient evidence
        # that a broadband room or driver sweep has 20 dB SNR").
        if method not in _ALIGNMENT_BAND_METHODS:
            return "unknown", None
        if estimated_snr_db >= model.alignment_snr_ok_db:
            return "ok", None
        return "insufficient", model.alignment_snr_ok_db - estimated_snr_db
    # Magnitude / trim decision class: scalar evidence is acceptable.
    if estimated_snr_db >= model.snr_ok_db:
        return "ok", None
    if estimated_snr_db >= model.snr_warn_db:
        return "reduced", model.snr_ok_db - estimated_snr_db
    return "insufficient", model.snr_warn_db - estimated_snr_db


def band_snr_verdicts(
    *,
    decision_class: str,
    capture_bands: Sequence[Mapping[str, Any]],
    noise_bands: Sequence[Mapping[str, Any]] | None,
    noise_floor_dbfs_scalar: float | None,
    relevant_hz: tuple[float, float],
    model: QualityModel,
    band_method: str = "fft_band_power_difference",
) -> dict[str, Any]:
    """The SC-1 per-band SNR verdict block for one decision.

    ``capture_bands`` is the signal side (e.g. :func:`band_levels_dbfs` on the
    accepted sweep capture); ``noise_bands`` is the matching band-specific
    noise-floor report (same shape, matched to ``capture_bands`` by
    ``band_id``) when available. ``noise_floor_dbfs_scalar`` is a
    single-number noise-floor fallback — usable evidence for a
    ``"magnitude"`` decision, but never sufficient on its own for an
    ``"alignment"`` decision (see :func:`_band_verdict`).

    ``estimated_snr_db`` is populated whenever a number is computable
    (real per-band evidence OR the scalar fallback), even for a band whose
    ``verdict`` reads "unknown" because the decision class rejects that
    evidence type — the number stays visible for diagnostics; ``verdict`` (not
    the presence of a number) is the trust signal callers must gate on.

    ``relevant_hz`` scopes which bands can veto the OVERALL verdict: every
    band in ``capture_bands`` gets its own entry (useful for diagnostics even
    outside the window), but ``worst_relevant``/``verdict`` are computed only
    from bands overlapping ``relevant_hz`` — a bad octave outside the window a
    decision actually depends on must not refuse the whole capture (the
    partial-pass rule in "Level control and SNR").
    """
    if decision_class not in DECISION_CLASSES:
        raise ValueError(f"unknown decision_class: {decision_class!r}")

    noise_by_band: dict[Any, Mapping[str, Any]] = {
        band.get("band_id"): band
        for band in (noise_bands or ())
        if isinstance(band, Mapping) and band.get("band_id") is not None
    }

    bands_out: list[dict[str, Any]] = []
    for capture_band in capture_bands or ():
        if not isinstance(capture_band, Mapping):
            continue
        band_id = capture_band.get("band_id")
        band_hz = capture_band.get("band_hz")
        capture_level = _to_float(capture_band.get("level_dbfs"))
        if capture_level is None:
            continue

        estimated_snr_db: float | None = None
        method = "none"
        noise_band = noise_by_band.get(band_id)
        if noise_band is not None:
            noise_level = _to_float(noise_band.get("level_dbfs"))
            if noise_level is not None:
                estimated_snr_db = capture_level - noise_level
                method = band_method
        if method == "none" and noise_floor_dbfs_scalar is not None:
            estimated_snr_db = capture_level - float(noise_floor_dbfs_scalar)
            method = "scalar_fallback"

        verdict, shortfall_db = _band_verdict(
            decision_class=decision_class,
            method=method,
            estimated_snr_db=estimated_snr_db,
            model=model,
        )
        bands_out.append({
            "band_id": band_id,
            "band_hz": (
                [float(band_hz[0]), float(band_hz[1])]
                if isinstance(band_hz, (list, tuple)) and len(band_hz) == 2
                else None
            ),
            "estimated_snr_db": (
                round(estimated_snr_db, 2) if estimated_snr_db is not None else None
            ),
            "verdict": verdict,
            "shortfall_db": (
                round(shortfall_db, 2) if shortfall_db is not None else None
            ),
            "method": method,
        })

    relevant_lo, relevant_hi = float(relevant_hz[0]), float(relevant_hz[1])
    worst_entry = worst_band_verdict(bands_out, relevant_lo, relevant_hi)
    if worst_entry is None:
        worst_relevant = None
        overall_verdict = "unknown"
    else:
        worst_relevant = {
            "band_id": worst_entry["band_id"],
            "estimated_snr_db": worst_entry["estimated_snr_db"],
            "verdict": worst_entry["verdict"],
        }
        overall_verdict = worst_entry["verdict"]

    return {
        "schema_version": 1,
        "decision_class": decision_class,
        "relevant_hz": [relevant_lo, relevant_hi],
        "bands": bands_out,
        "worst_relevant": worst_relevant,
        "verdict": overall_verdict,
    }


def cap_null_depth_db(
    measured_db: float,
    worst_relevant: Mapping[str, Any] | None,
    margin_db: float,
) -> tuple[float, bool]:
    """Cap a measured null depth to what the overlap-band SNR can prove.

    A null of depth D needs at least D + ``margin_db`` of SNR in the overlap
    band to be trustworthy ("Level control and SNR": "a null of depth D
    cannot be measured with less than about D + 10 dB of SNR in the overlap
    band"). Reporting a deeper number than the noise floor can support would
    overstate confidence, so this returns the (possibly capped) depth to
    REPORT and whether capping occurred.

    ``worst_relevant`` is the ``worst_relevant`` entry from
    :func:`band_snr_verdicts` for the overlap band (``relevant_hz=[fc/2,
    fc*2]`` for a summed-crossover decision). When it is ``None`` or carries
    no numeric evidence — including the alignment-class "unknown" case, where
    :func:`band_snr_verdicts` already nulls out ``estimated_snr_db`` for
    scalar-only/no evidence — the measured depth is returned unchanged,
    uncapped: there is no SNR figure to cap against.

    The capped value floors at 0 dB (never negative — "a null shallower than
    nothing" is not a meaningful report) but the comparison against
    ``measured_db`` uses the UNCLAMPED cap, so a very low overlap SNR still
    reports "capped at 0 dB", not silently "uncapped because 0 > cap".

    The pass/fail verdict for a summed-crossover capture must be computed
    from the UNCAPPED ``measured_db`` BEFORE calling this — a capped-but-
    still-deep null is safely "at least that deep" (see
    ``jasper.active_speaker.driver_acoustics.analyze_summed_crossover``,
    which this function does not itself decide).
    """
    if worst_relevant is None:
        return measured_db, False
    snr = worst_relevant.get("estimated_snr_db")
    if snr is None:
        return measured_db, False
    cap = float(snr) - margin_db
    if measured_db > cap:
        return max(cap, 0.0), True
    return measured_db, False
