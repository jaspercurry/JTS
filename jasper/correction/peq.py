"""Greedy peak-fit parametric-EQ designer for the modal range.

Defaults match Jasper's known-good REW workflow:
  - Match range:        20–350 Hz (modal-range only — Toole-aligned)
  - Max filters:        5
  - Mode:               cuts only (Floyd Toole's "first do no harm")
  - Max cut:            -10 dB (anything deeper means the room needs
                                acoustic treatment, not EQ)
  - Max boost:          +3 dB (when cuts_only=False; per-filter cap)
  - Overall max boost:  0 dB (preserve digital headroom; enforced by
                              cuts_only=True, which is the default)
  - Q range:            1.0–8.0

Algorithm — greedy peak-fit:
  1. Compute residual(f) = measured(f) − target(f) inside the band.
  2. Find the largest peak in the residual.
  3. Estimate Q from the -3 dB width around the peak.
  4. Add a peaking-EQ that cancels the peak (cuts_only ⇒ skip dips).
  5. Subtract the bell-curve response from residual.
  6. Repeat until max_filters reached OR residual RMS within
     flatness_target_db.

Each peaking-EQ maps directly to a CamillaDSP `Biquad { type:
Peaking, freq, q, gain }` filter — see jasper.correction.camilla_yaml
for the YAML emitter. We don't generate biquad coefficients here;
CamillaDSP does that at config-load time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from jasper.camilla_config_contract import total_positive_boost_db


@dataclass(frozen=True)
class PEQ:
    """A single peaking-EQ filter (parametric EQ biquad).

    Maps 1:1 to a CamillaDSP `Biquad / Peaking` filter.
    """
    freq: float    # Hz — the bell's center frequency
    q: float       # quality factor; ~1 ≈ octave wide, ~8 ≈ 1/8 oct
    gain: float    # dB — negative = cut, positive = boost


def _bell_response_db(
    eval_freqs: np.ndarray,
    fc: float,
    q: float,
    gain_db: float,
) -> np.ndarray:
    """Approximate magnitude response of a peaking bell, in dB.

    Used by the greedy iteration to update its residual estimate
    after adding a filter. We do NOT use this for actually
    generating the filter on the speaker — CamillaDSP does that
    from (freq, q, gain). We just need a shape that's close enough
    that the next greedy iteration picks a sensible second peak.

    The shape is a Lorentzian peak in log-frequency: magnitude_db(f) ≈
    gain_db / (1 + (Δoct / bw)²) where Δoct = log2(f / fc) and `bw` is
    the half-bandwidth in OCTAVES at which the response falls to
    gain_db/2. For a CamillaDSP/RBJ peaking biquad of quality Q that
    half-bandwidth is bw = asinh(1/(2Q)) / ln(2) — so the model's
    half-gain width matches the biquad CamillaDSP will actually realize
    from (freq, q, gain). (The earlier bw = 1/Q was ~1.4× too wide,
    which made predicted_response and the greedy residual over-subtract
    into neighbouring bands — a pessimistic prediction, not a wrong
    filter.) The far skirts are still a Lorentzian approximation, but the
    half-width — what the greedy residual subtraction needs to pick a
    sensible NEXT peak — is now correct.
    """
    if fc <= 0:
        return np.zeros_like(eval_freqs)
    omega = eval_freqs / fc
    # Avoid log of 0 / negative
    safe = np.where(omega > 0, omega, 1.0)
    delta_oct = np.log2(safe)
    # RBJ peaking-EQ half-bandwidth (octaves) for this Q; the max() keeps
    # the q→0 guard the prior 1/Q form had.
    bw = math.asinh(1.0 / (2.0 * max(q, 1e-3))) / math.log(2.0)
    response = gain_db / (1.0 + (delta_oct / bw) ** 2)
    response[omega <= 0] = 0.0
    return response


def _estimate_q(
    band_freqs: np.ndarray,
    band_residual_db: np.ndarray,
    peak_idx: int,
    *,
    q_min: float,
    q_max: float,
) -> float:
    """Estimate Q from the -3 dB width around a peak.

    Walks outward from peak_idx until the residual drops below
    |peak| - 3 dB on each side. Q = fc / bandwidth. If the peak is
    too small (|peak| < 3 dB) for the -3 dB rule, return Q=2.0 as a
    sensible default (~half-octave wide).
    """
    peak_db = band_residual_db[peak_idx]
    abs_peak = abs(peak_db)
    if abs_peak < 3.0:
        return 2.0

    threshold = abs_peak - 3.0
    n = len(band_residual_db)

    lower = peak_idx
    while lower > 0 and abs(band_residual_db[lower]) > threshold:
        lower -= 1
    upper = peak_idx
    while upper < n - 1 and abs(band_residual_db[upper]) > threshold:
        upper += 1

    f_lower = band_freqs[lower]
    f_upper = band_freqs[upper]
    bandwidth = f_upper - f_lower
    fc = band_freqs[peak_idx]

    if bandwidth <= 0:
        return float(np.clip(4.0, q_min, q_max))
    return float(np.clip(fc / bandwidth, q_min, q_max))


def design_peq(
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs: np.ndarray,
    *,
    f_low: float = 20.0,
    f_high: float = 350.0,
    max_filters: int = 5,
    max_cut_db: float = -10.0,
    max_boost_db: float = 3.0,
    cuts_only: bool = True,
    flatness_target_db: float = 1.0,
    q_min: float = 1.0,
    q_max: float = 8.0,
    min_filter_gain_db: float = 0.5,
) -> list[PEQ]:
    """Greedy peak-fit PEQ designer.

    Args:
      measured_db: smoothed magnitude response, in dB, on `freqs`.
      target_db: target curve, in dB, on the same `freqs`.
      freqs: frequency grid (Hz). Must be strictly increasing.
      f_low / f_high: design band. Outside this range no filters are
        placed even if there's residual error.
      max_filters: hard cap on PEQs in the result.
      max_cut_db / max_boost_db: per-filter gain limits (dB).
      cuts_only: when True, only fit filters with negative gain.
        This is the v1 default — Toole's "first do no harm".
      flatness_target_db: stop when residual RMS in the design band
        drops below this.
      q_min / q_max: Q clamp for stability + audibility.
      min_filter_gain_db: don't add a filter whose absolute gain
        would be below this — they're cosmetic and waste filter
        slots.

    Returns:
      List of PEQ in the order they were added (largest impact
      first).
    """
    if len(measured_db) != len(target_db) or len(measured_db) != len(freqs):
        raise ValueError(
            f"length mismatch: measured={len(measured_db)} "
            f"target={len(target_db)} freqs={len(freqs)}"
        )
    if f_high <= f_low:
        raise ValueError(f"f_high ({f_high}) must be > f_low ({f_low})")

    band_mask = (freqs >= f_low) & (freqs <= f_high)
    if not band_mask.any():
        return []

    # Work on a copy — design_peq is pure with respect to its inputs.
    residual = (measured_db - target_db).astype(np.float64).copy()
    peqs: list[PEQ] = []

    band_freqs = freqs[band_mask]

    for _ in range(max_filters):
        band_residual = residual[band_mask]

        # Pick the peak. cuts_only ⇒ only consider positive
        # excursions (where measured > target); else absolute peak.
        if cuts_only:
            search = np.where(band_residual > 0, band_residual, 0.0)
        else:
            search = np.abs(band_residual)
        peak_idx = int(np.argmax(search))
        peak_db = float(band_residual[peak_idx])

        # Stop early if the response is flat ENOUGH — both the
        # band RMS is low AND no narrow peaks remain. RMS-only
        # would miss a sharp narrow mode (RMS low because it's
        # narrow, peak high because it's tall) — a real audible
        # mode is exactly the kind of thing we should fix. Both
        # conditions must be met to stop.
        rms = float(np.sqrt(np.mean(band_residual ** 2)))
        if rms < flatness_target_db and abs(peak_db) < flatness_target_db * 2:
            break

        # No peak left to fit? Stop.
        if cuts_only and peak_db <= 0:
            break
        if abs(peak_db) < min_filter_gain_db:
            break

        peak_freq = float(band_freqs[peak_idx])
        q_est = _estimate_q(
            band_freqs, band_residual, peak_idx,
            q_min=q_min, q_max=q_max,
        )

        # Gain to cancel the peak. Clamp to per-filter limits.
        proposed = -peak_db
        if cuts_only:
            gain_db = float(np.clip(proposed, max_cut_db, 0.0))
        else:
            gain_db = float(np.clip(proposed, max_cut_db, max_boost_db))

        if abs(gain_db) < min_filter_gain_db:
            break

        peq = PEQ(freq=peak_freq, q=q_est, gain=gain_db)
        peqs.append(peq)

        # Update residual: a peaking filter with `gain_db` adds
        # `bell(f, gain_db)` to the response, so the new residual is
        # old_residual + bell.
        bell = _bell_response_db(freqs, peak_freq, q_est, gain_db)
        residual = residual + bell

    return peqs


def total_max_boost_db(peqs: list[PEQ]) -> float:
    """Worst-case additive boost across the PEQ set, in dB.

    Used to verify the 'overall max boost = 0 dB' headroom rule when
    cuts_only=False. Boost stacking is the load-bearing concern: a
    single +3 dB filter is fine, two +3 dB filters at adjacent
    frequencies summing to +6 dB is not.

    Delegates to the canonical contract helper so the design-time boost
    cap and the emit-time room-headroom trim share one definition.
    """
    return total_positive_boost_db(peqs)


def predicted_response(
    peqs: list[PEQ],
    freqs: np.ndarray,
) -> np.ndarray:
    """The dB shift the PEQ chain applies at each frequency.

    Sum of bell responses. Used by the frontend to overlay the
    predicted post-correction curve before the user taps Apply.
    """
    if not peqs:
        return np.zeros_like(freqs, dtype=np.float64)
    out = np.zeros_like(freqs, dtype=np.float64)
    for peq in peqs:
        out += _bell_response_db(freqs, peq.freq, peq.q, peq.gain)
    return out
