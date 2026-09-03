# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Decision-class + band-specific SNR gate — the split SNR policy.

docs/active-crossover-information-design.md ("Level control and SNR") splits SNR trust by what
the number is used FOR: magnitude/trim decisions need 25 dB SNR (confident), 20-25 dB (reduced
confidence), refused below 20 dB; null/alignment decisions need roughly 35 dB in the overlap
band (a null of depth D needs about D + 10 dB), and a scalar noise-floor reading is NOT
sufficient evidence there — only a real per-band measurement is.

Two halves: :func:`band_levels_dbfs` (the FFT band-power estimator, shared with room correction
via ``jasper.correction.acoustic_quality``) and :func:`band_snr_verdicts` (the decision-class
verdict builder; ``jasper.active_speaker.driver_acoustics`` is the first consumer).

Pure-data/pure-function: no I/O, no product policy, no CamillaDSP or playback awareness. numpy
is module-level (the FFT needs it); callers that must stay numpy/scipy-free until a measurement
runs import this module LAZILY inside a function instead.
"""
from __future__ import annotations

import math
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import deconv
from jasper.audio_measurement.quality_model import QualityModel

# Six bands spanning the trusted phone-mic analysis window. First four are byte-identical to
# jasper.correction.acoustic_quality.SNR_BANDS_HZ (pinned by test_audio_measurement_snr_policy.py);
# "mid"/"treble" extend the table up through a tweeter's crossover range.
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
    "paired_signal_window_deconvolution",
})

# Per-band verdict severity, worst last; "unknown" is deliberately absent (no evidence, never
# outranks a real verdict). NOT quality_model's TrustLevel, despite reading the same two
# magnitude thresholds: a TrustLevel LABELS a number, this REFUSES a decision and is scoped per
# decision class, so one capture is legitimately magnitude-"ok" and alignment-"insufficient".
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
    *,
    window: Literal["hann", "rectangular"] = "hann",
) -> list[dict[str, Any]]:
    """Band-INTEGRATED level of ``samples``, in true dBFS, per band with bins.

    Each entry's ``level_dbfs`` is ``20*log10`` of the band's RMS amplitude — what a band-pass
    filter followed by an RMS meter would read.

    Fixed in #1838: the previous per-BIN-mean (PSD-like) implementation read low by
    ``7.27 + 10*log10(n_bins)`` dB and was not even a stable statistic — ``n_bins`` scales with
    input length, so the SAME stationary noise over 1/2/4 s read -111.4/-114.4/-117.1 dBFS. That
    was benign while every consumer used it in a ratio (SNR verdicts cancel it), until #1829's
    absolute per-driver level solve read the room 18-39 dB too quiet and killed a field session.

    Parseval-exact: one-sided ``rfft`` bins weighted to two-sided energy, Hann window-energy
    loss divided out by its own ``sum(w**2)``. UNBIASED against closed-form white-noise band
    power; residual spread (chi-square, up to ~0.8 dB on a 60-bin band) is not an accuracy
    budget — a full-band total matches true RMS to <0.05 dB.

    ``window="rectangular"`` is a non-stationary-input escape hatch, not free choice: Hann
    re-weights a swept sine's energy by WHEN it occurs, reading a 4 s sweep's band split wrong
    by tens of dB and varying with capture length (#1847). Pass ``rectangular`` for a
    sweep/chirp capture — ``capture_band_snr`` does; every other caller keeps the Hann default,
    including ``driver_acoustics._capture_band_levels``'s sweep-capture gate, whose own bias is
    measured but unreached in production (see that docstring before reviving it).

    Bounds the FFT input via ``deconv.cap_capture_length``, since callers pass uploaded WAVs
    limited only by the HTTP body cap — unbounded would drive rfft+hanning to OOM on the 1 GB Pi.
    """
    if window not in ("hann", "rectangular"):
        raise ValueError(f"band_levels_dbfs: unknown window {window!r}")
    if samples.ndim != 1 or sample_rate <= 0 or samples.size < 8:
        return []
    samples = deconv.cap_capture_length(samples, sweep_len=0, sample_rate=sample_rate)
    x = np.asarray(samples, dtype=np.float64)
    if window == "hann":
        win = np.hanning(x.size)
        windowed = x * win
        window_energy = float(np.sum(win ** 2))
    else:
        # rectangular: x * ones(N) == x and sum(ones(N)**2) == N, so both are skipped outright.
        windowed = x
        window_energy = float(x.size)
    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    # One-sided -> two-sided: every bin but DC (and Nyquist, even-length only) is a conjugate pair.
    power = power * 2.0
    power[0] = power[0] / 2.0
    if x.size % 2 == 0:
        power[-1] = power[-1] / 2.0
    # Parseval + window-energy normalization.
    denom = float(x.size) * window_energy
    out: list[dict[str, Any]] = []
    for band_id, low, high in bands:
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            continue
        mean_square = float(np.sum(power[mask])) / denom if denom > 0 else 0.0
        out.append({
            "band_id": band_id,
            "band_hz": [low, high],
            "level_dbfs": round(_dbfs(math.sqrt(max(mean_square, 0.0))), 2),
        })
    return out


def framed_ambient_band_report(
    samples: np.ndarray,
    sample_rate: int,
    bands: Sequence[tuple[str, float, float]] = CROSSOVER_SNR_BANDS_HZ,
    *,
    percentile: float,
) -> dict[str, Any]:
    """One-second-frame ambient PSD statistic, independent of total duration."""

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
                "level_dbfs": round(float(np.percentile(levels, percentile)), 2),
            })
    return {
        "schema_version": 1,
        "duration_s": round(x.size / sample_rate, 3),
        "method": f"one_second_p{percentile:g}",
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


def excitation_covered_bands(
    bands: Sequence[tuple[str, float, float]],
    *,
    f1_hz: float,
    f2_hz: float,
) -> dict[str, bool]:
    """Which bands lie ENTIRELY inside the swept-sine reference's excited range.

    A regularized deconvolution divides by the reference sweep's own spectrum, clamped by a
    fixed Tikhonov epsilon. Outside ``[f1_hz, f2_hz]`` the reference carries no deliberate
    energy, so the division is dominated by epsilon and its regularized inverse resonates
    right at the knee, amplifying whatever is on the OTHER side (room noise for an ambient
    capture) well beyond its true level — reporting a noise floor overstated by tens of dB.

    An uncovered band is not safe to read from the deconvolved domain; callers should fall back
    to a raw measurement instead. Deliberately exact (no margin): widening it would also flag
    bands that empirically read fine today.
    """

    lo_hz, hi_hz = float(f1_hz), float(f2_hz)
    return {
        band_id: (float(low) >= lo_hz and float(high) <= hi_hz)
        for band_id, low, high in bands
    }


def apply_noise_band_fallback(
    noise_bands: Sequence[Mapping[str, Any]],
    *,
    robust_bands: Sequence[Mapping[str, Any]],
    baseline_bands: Sequence[Mapping[str, Any]],
    covered: Mapping[str, bool],
) -> list[dict[str, Any]]:
    """Robust-delta adjustment, with a raw-ambient fallback for uncovered bands.

    For a COVERED band: the deconvolved level plus the small robust-minus-baseline delta (a
    non-stationarity correction). For an UNCOVERED band: the deconvolved level is a Tikhonov
    regularization artifact (see :func:`excitation_covered_bands`), so this reports the raw
    robust (p95) ambient level instead — UNLESS that reading is itself floor-clamped at
    :data:`DBFS_FLOOR`, in which case the deconvolved+delta value is kept as least-bad. Each
    band carries a diagnostic ``"basis"`` key recording which path was taken.

    **The fallback changes the band's UNITS** — a ``"deconvolved"`` band is a gated
    transfer-function level (``20*log10|Y/X|``, per-bin power MEAN); a
    ``"raw_ambient_fallback"`` band is a band-INTEGRATED RMS dBFS over ungated one-second
    frames. The substitution is not a constant offset, nor stable in sweep length (SC-1 SNR
    units defect, 2026-08-01: error ran -22.08 to +11.11 dB at 8 s and -13.32 to +27.44 dB at
    1 s on the summed-crossover capture).

    Correct for what it was built for (#1563: a WIDE per-driver near-field sweep where the
    uncovered bands are ones the gate doesn't read) — not a licence to mix domains inside a
    gated band; a consumer whose gate reads uncovered bands should narrow its band table instead.
    """

    robust_by_id = {item["band_id"]: item for item in robust_bands}
    baseline_by_id = {item["band_id"]: item for item in baseline_bands}
    adjusted: list[dict[str, Any]] = []
    for item in noise_bands:
        band_id = item["band_id"]
        robust_item = robust_by_id.get(band_id)
        baseline_item = baseline_by_id.get(band_id)
        delta = (
            float(robust_item["level_dbfs"]) - float(baseline_item["level_dbfs"])
            if robust_item is not None and baseline_item is not None
            else 0.0
        )
        raw_robust_level = (
            float(robust_item["level_dbfs"]) if robust_item is not None else None
        )
        if (
            not covered.get(band_id, True)
            and raw_robust_level is not None
            and raw_robust_level > DBFS_FLOOR
        ):
            adjusted.append({
                **item,
                "level_dbfs": round(raw_robust_level, 2),
                "basis": "raw_ambient_fallback",
            })
        else:
            adjusted.append({
                **item,
                "level_dbfs": round(float(item["level_dbfs"]) + delta, 2),
                "basis": "deconvolved",
            })
    return adjusted


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


def _worst_snr_key(band: Mapping[str, Any]) -> float:
    """Lower sorts worse. A missing/unparseable number sorts ``+inf`` (most PERMISSIVE) so it
    never displaces a real number — the safe direction, since electing a numberless band would
    silently remove the cap. ``-inf`` shares that bucket too: it's a degenerate sentinel, not a
    measurement, and unreachable anyway (:func:`band_snr_verdicts` always verdicts it
    "insufficient", so verdict RANK selects it before this key is consulted)."""
    snr = _to_float(band.get("estimated_snr_db"))
    if snr is None or not math.isfinite(snr):
        return math.inf
    return snr


def worst_band_verdict(
    bands: Sequence[Mapping[str, Any]] | None,
    lo_hz: float,
    hi_hz: float,
) -> dict[str, Any] | None:
    """The single worst entry in ``bands`` overlapping ``[lo_hz, hi_hz]``.

    ``verdict`` (insufficient > reduced > ok) dominates the selection — one ``insufficient``
    band vetoes its ``ok`` siblings regardless of SNR. Among entries of EQUAL verdict rank the
    LOWEST ``estimated_snr_db`` wins (the minimum over the window, not table order — #2026: a
    positional pick graded against a band up to 17 dB too permissive). See :func:`_worst_snr_key`
    for the ``+inf``/``-inf`` tie-break.

    An entry whose ``verdict`` is "unknown" never wins. Returns ``None`` when no evidenced band
    overlaps the window — callers read that as "unknown" for the whole window.
    """
    worst: dict[str, Any] | None = None
    worst_rank = -1
    worst_snr = math.inf
    for band in bands or ():
        if not isinstance(band, Mapping):
            continue
        if not _band_overlaps(band.get("band_hz"), lo_hz, hi_hz):
            continue
        verdict = band.get("verdict")
        if verdict not in _VERDICT_RANK:
            continue
        rank = _VERDICT_RANK[verdict]
        snr = _worst_snr_key(band)
        if worst is None or rank > worst_rank or (
            rank == worst_rank and snr < worst_snr
        ):
            worst, worst_rank, worst_snr = dict(band), rank, snr
    return worst


def _band_verdict(
    *,
    decision_class: str,
    method: str,
    estimated_snr_db: float | None,
    model: QualityModel,
) -> tuple[str, float | None]:
    """(verdict, raw shortfall_db); ``shortfall_db`` is unrounded here, rounded by the caller."""
    if estimated_snr_db is None:
        return "unknown", None
    if decision_class == DECISION_CLASS_ALIGNMENT:
        # A scalar noise floor is not sufficient evidence for a null/alignment call, even when
        # computable — degrade to "unknown" rather than gate on an untrustworthy figure.
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

    ``noise_floor_dbfs_scalar`` is usable evidence for "magnitude" but never sufficient alone
    for "alignment" (see :func:`_band_verdict`). ``estimated_snr_db`` is populated whenever
    computable, even when ``verdict`` reads "unknown" — ``verdict``, not the number's presence,
    is what callers must gate on.

    ``relevant_hz`` scopes which bands can veto the OVERALL verdict: every band gets its own
    entry, but ``worst_relevant``/``verdict`` only reduce over bands overlapping ``relevant_hz``
    — a bad octave outside the window a decision depends on must not refuse the whole capture.
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
                # One-decimal rounding: an unrounded 19.999999 would fail the inclusive 20 dB
                # threshold while displaying as 20.0 dB.
                estimated_snr_db = round(capture_level - noise_level, 1)
                method = band_method
        if method == "none" and noise_floor_dbfs_scalar is not None:
            estimated_snr_db = round(
                capture_level - float(noise_floor_dbfs_scalar), 1
            )
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
