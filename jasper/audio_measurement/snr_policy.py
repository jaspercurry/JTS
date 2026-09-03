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
  through ``jasper.correction.acoustic_quality`` with Room's band table) so
  room correction and active-crossover commissioning share one implementation
  instead of two forks.
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
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import deconv
from jasper.audio_measurement.quality_model import QualityModel

# Six bands spanning the trusted phone-mic analysis window. The first four are
# byte-identical to jasper.correction.acoustic_quality.SNR_BANDS_HZ (room
# correction's shipped table, pinned by test_audio_measurement_snr_policy.py so
# the two never drift apart). "mid" and "treble" extend the table up through a
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
    "paired_signal_window_deconvolution",
})

# Per-band verdict severity, worst last. Used to reduce a list of per-band
# verdicts to a single "worst" verdict for a frequency window. "unknown" is
# deliberately absent — it carries no evidence, so it never outranks a real
# verdict (see worst_band_verdict).
#
# These words are NOT quality_model's TrustLevel ("high"/"medium"/"low") and
# must not be unified into it, however alike the two look — the magnitude
# class even reads the same two thresholds. A TrustLevel LABELS a number; this
# REFUSES a decision, ships a `shortfall_db` saying how many dB would clear it,
# and is scoped per decision class, so one capture is legitimately
# magnitude-"ok" and alignment-"insufficient" at the same time. Two trust
# labels contradicting each other on one number would be a bug; two refusals
# disagreeing about two different decisions is the whole point of the split
# policy. `program_analysis.ALIGNMENT_SNR_REFUSAL_VERDICT` names the worst
# member for what it is.
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

    Each entry's ``level_dbfs`` is ``20*log10`` of the band's RMS amplitude:
    exactly the number a band-pass filter followed by an RMS meter would
    read. A -20 dBFS sine inside one band puts -20.0 in that band's row; a
    signal whose energy is split across bands has its total recovered by
    summing the bands' powers.

    **This was wrong until issue #1838** and the defect is worth naming,
    because both the old shape and the fix are load-bearing. The previous
    implementation returned ``sqrt(mean(power[mask])) / x.size`` — a per-BIN
    mean, i.e. a PSD-like quantity, not band power. Three consequences:

    * It read low by ``7.27 + 10*log10(n_bins)`` dB — 25 dB on a 60 Hz-wide
      band from a 1 s frame, 42 dB across ``mid`` — and a quiet room's upper
      bands saturated flat against :data:`DBFS_FLOOR`, destroying the
      evidence outright.
    * It was not even a stable statistic: ``n_bins`` scales with the input's
      own length, so the SAME stationary noise measured over 1/2/4 s read
      -111.4/-114.4/-117.1 dBFS. A ratio between two levels therefore only
      cancelled when both sides were computed over an equal-length window.
    * It was benign for years because every consumer used it in such a
      ratio (an SNR verdict). Issue #1829 made it an ABSOLUTE authority —
      the MEASURE per-driver level solve — and the cancellation stopped: the
      solve read the room 18-39 dB too quiet, backed MEASURE off 30-34 dB,
      and the field session died on its own buried pilots.

    The estimator is Parseval-exact: the one-sided ``rfft`` bins are weighted
    back to two-sided energy (all but DC and, for an even-length input,
    Nyquist), and the Hann window's energy loss is divided out by its own
    ``sum(w**2)`` rather than a hard-coded 3/8, so the correction stays right
    for any window this ever uses. Against closed-form band power for white
    noise it is UNBIASED; the residual spread is the chi-square variance of a
    finite-bin estimate (up to ~0.8 dB observed on the narrowest 60-bin band
    from a 1 s frame, shrinking as bins grow) and is not an accuracy budget.
    A full-band total, where that variance is negligible, matches the true
    RMS to <0.05 dB.

    **``window`` picks "hann" (default) or "rectangular" — this is a
    non-stationary-input escape hatch, not free choice.** Hann is correct
    for the stationary ambient this reads by default, but a sweep is
    non-stationary: the window re-weights a swept sine's frequencies by WHEN
    they occur in the capture — a 4 s sweep's reported band split was wrong
    by tens of dB, and it varied with capture length (issue #1847; measured
    ~-10 dB on ``sub_bass``, ~1.5 dB of capture-length dependence, on room
    correction's own ~11 s sweep). Pass ``window="rectangular"`` for a
    sweep/chirp capture instead: an unwindowed FFT weights every sample
    equally regardless of when its energy lands in time, which is what a
    non-stationary signal needs. It is the same choice
    ``sweep_band_crest_factor_db``'s own validation test makes
    (:mod:`jasper.audio_measurement.program_analysis`,
    ``test_sweep_band_crest_factor_matches_the_rendered_sweep``), and it
    matches that function's analytical dwell-time law to within 0.3 dB on
    room correction's own sweep shape (see
    ``test_band_levels_dbfs_rectangular_window_matches_the_sweep_law`` in
    ``tests/test_audio_measurement_snr_policy.py``). ``capture_band_snr``
    (room correction's sweep-capture disclosure path) passes
    ``window="rectangular"`` for exactly this reason; every OTHER caller —
    the ambient/noise reports here, and
    ``driver_acoustics._capture_band_levels``'s own sweep-capture SC-1 SNR
    gate — still reads through the Hann default. That gate's bias has since
    been MEASURED (issue #2010, 2026-08-01) and is real, but no production
    caller reaches it, so it keeps the Hann default deliberately rather than
    trading a characterised dead error for an uncharacterised one. The
    numbers, the reachability evidence, and what reviving it would need live
    at that consumer, in ``_capture_band_levels``'s own docstring.

    Bounds the FFT input the same way
    :func:`~jasper.audio_measurement.deconv.deconvolve` does
    (``deconv.cap_capture_length``), since callers pass uploaded WAVs
    (ambient noise, capture band levels) limited only by the HTTP body cap —
    unbounded would otherwise drive this rfft + hanning to OOM on the 1 GB Pi.
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
        # window == "rectangular": x * ones(N) == x and sum(ones(N)**2) == N,
        # so both the ones() array and the elementwise multiply are skipped
        # outright — ~2x N x 8 bytes avoided on the path whose own docstring
        # below names OOM risk on an uploaded WAV up to the 30 s cap.
        windowed = x
        window_energy = float(x.size)
    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    # One-sided -> two-sided energy: every bin except DC (and Nyquist, which
    # only exists for an even-length input) stands for a conjugate pair.
    power = power * 2.0
    power[0] = power[0] / 2.0
    if x.size % 2 == 0:
        power[-1] = power[-1] / 2.0
    # Parseval + window-energy normalization: mean-square of the unwindowed
    # signal in a band = (two-sided band energy) / (N * sum(w**2)).
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

    A regularized deconvolution (:func:`jasper.audio_measurement.deconv.regularized_deconvolution_full`)
    divides by the reference sweep's own spectrum, clamped by a fixed
    (frequency-independent) Tikhonov epsilon. Outside ``[f1_hz, f2_hz]`` — and
    right at that edge, where the sweep's fade-in/out tapers its energy toward
    zero — the reference carries essentially no deliberate energy, so that
    division is dominated by epsilon rather than real signal. Right at the
    knee where the reference's power crosses epsilon, the regularized inverse
    has a well-known resonant peak (its gain is maximized exactly where
    ``|X(f)|**2 == epsilon``, tapering in both directions) that amplifies
    whatever is on the OTHER side of the division — real driver output for a
    signal capture, incoherent room noise for an ambient capture — well
    beyond its true level. A signal capture usually swamps this artifact (a
    near-mic'd driver is loud); an ambient capture has nothing to swamp it
    with, so the artifact dominates and the reported noise floor is overstated
    by tens of dB.

    A band that is not fully covered by the reference is not safe to read
    from the deconvolved domain at all — callers should fall back to a
    non-deconvolved (raw) measurement for that band instead of trusting this
    resonance-corrupted value. This check is deliberately exact (no margin):
    widening it to "give the fade some berth" would also flag bands that
    empirically read fine today (e.g. a band starting 20 Hz above ``f1_hz``),
    trading a real bug for an unforced regression.
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

    ``noise_bands`` is the deconvolved-domain per-band noise report (e.g.
    :func:`magnitude_band_levels` on a deconvolved+windowed ambient IR).
    ``robust_bands``/``baseline_bands`` are the matching non-deconvolved
    ambient reports (:func:`framed_ambient_band_report` at ``percentile=95``
    and ``percentile=50``). ``covered`` is
    :func:`excitation_covered_bands`'s per-band verdict for whether the
    reference sweep actually excited that band.

    For a COVERED band, this is the pre-existing behavior unchanged: the
    deconvolved level plus the small robust-minus-baseline delta (a
    non-stationarity correction — see :func:`framed_ambient_band_report`'s
    docstring). For an UNCOVERED band, the deconvolved level is a Tikhonov
    regularization artifact, not a measurement (see
    :func:`excitation_covered_bands`), so this reports the raw robust (p95)
    ambient level directly instead — UNLESS that raw reading is itself
    floor-clamped at :data:`DBFS_FLOOR` (no real precision to trust either),
    in which case the deconvolved+delta value is kept as the least-bad
    available estimate. Each returned band carries a diagnostic ``"basis"``
    key (``"deconvolved"`` or ``"raw_ambient_fallback"``) recording which path
    was taken.

    **The fallback changes the band's UNITS, and a caller that subtracts it
    from a deconvolved signal level must account for that.** A
    ``"deconvolved"`` band is a gated transfer-function level (dimensionless,
    ``20*log10|Y/X|``, per-bin power MEAN); a ``"raw_ambient_fallback"`` band
    is a band-INTEGRATED RMS in true dBFS over ungated one-second frames.
    Three things differ — the division by ``|X(f)|``, the per-bin-mean vs
    band-sum statistic, and the observation window (a <=7 ms gated impulse
    response vs 1 s of room) — so the substitution is not a constant offset
    and its sign is not stable. It is not even stable in the SWEEP LENGTH: on
    the summed-crossover capture the error ran -22.08 to +11.11 dB at an 8 s
    sweep and -13.32 to +27.44 dB at 1 s, because the raw substitute gets no
    sweep processing gain while the deconvolved side does (SC-1 SNR units
    defect, 2026-08-01). Any number quoted for this substitution has to name
    the sweep length it was measured at — and note that the summed sweep's
    length is ``min(SUMMED_SWEEP_DURATION_S, both drivers' declared limits)``,
    so 8 s is its ceiling, not its typical value.

    This is a correct substitution for the case it was built for (issue #1563:
    a WIDE per-driver near-field sweep, where the uncovered bands are the ones
    the gate does not read and the deconvolved value there is a Tikhonov
    artifact). It is not a licence to mix domains inside a gated band. A
    consumer whose gate reads uncovered bands should narrow its band table to
    the excited range instead.
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
    """A band's ``estimated_snr_db`` as a comparable tie-break key.

    Lower sorts worse. The consumer of the winning entry's SNR reads a number
    the measurement must actually support, so it wants the minimum:
    ``jasper.web.correction_crossover_backend``'s completion-time level
    correction subtracts it from the solver's requirement to size a
    playback-level shortfall.

    A missing or unparseable number sorts as ``+inf`` — the most PERMISSIVE
    value — so such a band never displaces one carrying a real number. That
    is the safe direction: neither consumer can act on a number it does not
    have, so electing the numberless band would silently REMOVE the cap and
    the correction rather than tighten them.

    Non-finite numbers share that bucket, ``-inf`` included. That looks
    backwards for ``-inf`` (arithmetically the worst possible SNR) and is
    deliberate: ``-inf`` is a degenerate sentinel, not a measurement, so it
    is no more actionable than a missing value. It is also unreachable
    through :func:`band_snr_verdicts`, which is the only builder of these
    entries — a ``-inf`` SNR verdicts ``insufficient`` in every decision
    class, so verdict RANK selects such a band before this key is ever
    consulted against an ``ok`` sibling.
    """
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

    Two quantities are read off the returned entry, and "worst" has to mean
    the right thing for both:

    * ``verdict`` — the REFUSAL signal. Ranks insufficient > reduced > ok, and
      dominates the selection: one ``insufficient`` band vetoes its ``ok``
      siblings however good its own SNR is.
    * ``estimated_snr_db`` — the number the live consumer grades against.
      ``jasper.web.correction_crossover_backend``'s completion-time level
      correction subtracts it from the solver's requirement to size a
      playback-level shortfall (magnitude class — the route
      ``analyze_driver_capture`` and
      ``program_analysis._driver_response`` feed). Among entries of EQUAL
      verdict rank the LOWEST one wins, so this is the minimum over the
      window, not whichever band happened to come first in the table (issue
      #2026: a positional pick graded against a band up to 17 dB more
      permissive than the true worst, and made the reported figure depend on
      table order).

      Both consumers therefore read a stricter number than before #2026: a
      null caps nearer, and a level correction sizes a larger shortfall — so a
      session can solve to a HIGHER capture level than it used to. That is the
      corrected behaviour, not a new demand; see :func:`_worst_snr_key`.

    An entry whose ``verdict`` is "unknown" (or anything unrecognized) never
    wins — it carries no evidence, so it can neither veto nor clear the
    window. Returns ``None`` when no *evidenced* band overlaps the window
    (nothing overlaps, or everything that does is "unknown") — callers read
    that as "unknown" for the whole window: a partial-pass rule shared by
    :func:`band_snr_verdicts` (reducing over its own ``relevant_hz``) and
    ``jasper.active_speaker.driver_acoustics`` (reducing over one overlap-band
    Fc window) — one rule, not two.
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
                # Verdict and displayed evidence share the measurement's
                # meaningful one-decimal precision. Without this normalization
                # a binary-float 19.999999 result displayed as 20.0 dB failed
                # the inclusive 20 dB reduced-confidence threshold.
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
