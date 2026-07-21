# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of a crossover excitation-program capture.

``analyze_program_capture(program, samples, sample_rate) → ProgramAnalysis`` is
the single, deterministic, fixture-testable half of the conductor model (design
§5.6): every quantity — segment locations, per-segment integrity, in-capture
clock drift, per-driver gated responses, tweeter-vs-woofer alignment, and the
crossover candidate + predicted sum — derives from the ``(program, capture)``
pair with no side-channel state.

Pipeline (per phase):

1. **Locate** — matched-filter the first stimulus (one global offset), then each
   later segment at its scheduled offset ± a small window; record schedule
   residuals. Generalizes ``driver_acoustics._capture_to_magnitude``'s locator.
2. **Integrity** — per-segment peak dBFS + clipped-run detection (a run of ≥3
   consecutive samples at ≥0.999 full scale is a clip).
3. **Drift (MEASURE)** — ε from the woofer→woofer-repeat separation, cross-checked
   against the schedule-residual slope of all located segments; disagreement or
   |ε|>500 ppm ⇒ ``glitch_detected`` (callers must reject the capture).
4. **Per-driver response** — deconvolve → direct-arrival window + first-reflection
   gate → complex TF + magnitude (mic cal applied if given); band-SNR verdicts.
5. **Alignment (MEASURE)** — tweeter-vs-woofer relative delay: band-limited
   (≈Fc/2…2·Fc) GCC-PHAT, ×16-upsampled peak with parabolic sub-sample
   refinement, ε-corrected, geometry-bounded, parallax-subtracted; polarity from
   the correlation sign cross-checked against the flatter predicted sum.
6. **Candidate + prediction** — as-crossed branches (design §5.4) ⇒ trims level-
   match the branches through the crossover, then the predicted applied sum is
   ``W_xo·g_w + s·T_xo·g_t·e^{−jωτ}`` and its Fc±1-octave ripple is reported.

CHECK additionally returns the ambient band floor, per-pilot captured levels +
the behavioral linearity verdict (§3.4), channel-map sanity, and the solved
``GainPlan`` for MEASURE. VERIFY returns the gated summed response + ripple vs a
supplied predicted sum.

Reuses the measurement kernel (:mod:`~jasper.audio_measurement.sweep` /
``deconv`` / ``gating`` / ``snr_policy`` / ``analysis``) and mirrors
``jasper.capture_relay.alignment``'s confidence vocabulary. No I/O, no product
policy, no ``jasper.active_speaker`` import.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import analysis as analysis_mod
from jasper.audio_measurement import calibration as calibration_mod
from jasper.audio_measurement import deconv, gating, snr_policy
from jasper.audio_measurement.program import (
    KIND_PILOT,
    KIND_SWEEP,
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
    STIMULUS_KINDS,
    ExcitationProgram,
    ProgramSegment,
    segment_stimulus,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.quality_model import DRIVER
from jasper.capture_relay.alignment import cross_correlation_alignment
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

logger = logging.getLogger(__name__)

# --- locator / drift / alignment tuning ---
# Per-segment search half-window around the drift-free scheduled offset. Wide
# enough for a few-hundred-ppm drift over a ~25 s program (≈6 ms) plus acoustic
# delay, far tighter than the global first-stimulus search.
SEGMENT_SEARCH_S = 0.030
# Capture bound (kernel contract: defense at the FFT, 1 GB Pi — mirrors
# deconv.cap_capture_length's rationale). A legitimate conductor capture is the
# program plus a small phone-start lead; this margin bounds the global offset
# the locator can see. A stuck recording is truncated to
# program duration + this margin before any full-rate FFT runs.
CAPTURE_BOUND_MARGIN_S = 10.0
# Global-offset locate runs at this downsampled rate (mirrors
# driver_acoustics._capture_to_magnitude's 16 kHz locate) so the whole-capture
# correlation never allocates hundreds of MB; the arrival is then refined at
# the full rate inside a tiny window.
LOCATOR_RATE_HZ = 16_000
# Clip run: a run of at least this many consecutive samples at/above full scale.
# The at-full-scale threshold is the shared digital-full-scale fact owned by
# quality_model (same value every capture-quality layer reads), not a re-declared
# literal.
CLIP_RUN_SAMPLES = 3
CLIP_ABS_THRESHOLD = DRIVER.clip_abs_threshold
DBFS_FLOOR = -120.0

# A capture is rejected when the drift baselines disagree by more than this many
# samples-equivalent, or the primary drift exceeds the ppm bound (design §5.6.3).
GLITCH_RESIDUAL_SAMPLES = 1.5
MAX_DRIFT_PPM = 500.0

# The two woofer sweeps of a MEASURE program are bit-identical stimuli, so a
# clean capture reproduces the same captured level for both. A larger gap is a
# gain-rider (browser AGC nudging the level between the two sweeps) — a
# complement to the timing baselines (design §5.2). PROVISIONAL pending W6 bench
# distributions. A failure REUSES the ``drift_baselines_disagree`` glitch
# verdict — never a new user-facing reason code (design §5.2).
#
# Level is measured band-relative (in-band RMS over the woofer's own declared
# band, via `_band_power` — see `_estimate_drift`), not full-band single-sample
# PEAK — fixing the same 2026-07-20 bug class as `LINEARITY_TOLERANCE_DB`
# below (#1594, #1615): a low-frequency, room-mode-excited sweep's full-band
# peak is an unstable estimator, and two real hardware captures (Dayton
# iMM-6C AND UMIK-2) measured two genuinely-identical woofer sweeps 0.64 dB
# apart by full-band peak but only 0.06-0.24 dB apart by in-band RMS. In-band
# RMS is stable, so this tolerance stays tight.
REPEAT_LEVEL_TOLERANCE_DB = 0.3

# GCC-PHAT sub-sample refinement (design §5.6.5).
GCC_UPSAMPLE = 16
DEFAULT_ALIGN_SEARCH_MS = 2.0  # geometry prior bound on |relative delay|

# Alignment estimator status vocabulary.
ALIGNMENT_OK = "ok"
ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"

# Overlap band for trims / alignment / ripple: Fc ± 1 octave.
OVERLAP_OCTAVE_RATIO = 2.0

# Direct-arrival window used to isolate each driver's IR before deconvolution
# magnitude / alignment (mirrors deconv defaults; the pre guard catches the
# non-causal deconvolution shoulder).
IR_PRE_MS = 5.0
IR_POST_MS = 60.0

# Deconvolution window pre-guard: how far BEFORE the scheduled sweep position the
# window starts, so the window fully contains the sweep even though the global
# offset folds in the first driver's (small) acoustic delay. Both drivers use
# the SAME pre-guard and global-offset anchor, so their deconvolved IR direct
# peaks land at the pre-guard sample ± the relative delay — the aligner relies
# on that shared time base.
DECONV_PRE_GUARD_S = 0.25

# Gain solve: land the MEASURE capture peak in [-12, -9] dBFS with ≥6 dB guard.
DEFAULT_TARGET_CAPTURE_DBFS = -10.5
GAIN_GUARD_DB = 6.0
GAIN_MAX_DIGITAL_PEAK_DBFS = -GAIN_GUARD_DB  # digital peak must sit ≤ this

# Behavioral linearity tolerance (design §3.4): captured delta within this of
# the programmed delta. Measured band-relative + ambient-compensated (see
# `_pilot_observations`) since the 2026-07-20 fix — a full-band PEAK estimate
# (the pre-fix method) let LF room rumble ~30 dB above the tweeter-band
# ambient inflate the quiet woofer pilot's peak and compress the captured
# delta, tripping this tight a tolerance on a perfectly linear driver (the
# same bug class the channel-map discriminator was fixed for in #1594 —
# gotcha #6/#16 in docs/HANDOFF-crossover-measurement-v2.md).
LINEARITY_TOLERANCE_DB = 0.5

# Pilot edge-fade trim: `sweep.synchronized_swept_sine` applies a fixed 5 ms
# fade-in/fade-out to every stimulus it generates (its own "Light fade-in/out"
# comment) to avoid a click at a non-zero-crossing edge — a located pilot
# segment therefore ramps up/down over that fixed span rather than playing at
# full level throughout. Trimming exactly that span from each edge before
# measuring level keeps the RMS estimate to the pilot's steady-state portion.
# This is the composer's REAL fade length (read from the generator, not
# guessed), so no separate justification for "why 5 ms" is needed here.
PILOT_FADE_TRIM_S = 0.005

# Low-SNR honest routing (design note above `_pilot_observations`): ambient
# power subtraction only removes the room's noise-floor BIAS when the quiet
# (lo) pilot's own in-band power clears the in-band ambient power by enough
# margin that residual bias from ambient NONSTATIONARITY — the room's true
# noise power during the ~0.8 s pilot window can differ from the value
# measured over CHECK's separate, earlier ambient window — stays a small
# fraction of `LINEARITY_TOLERANCE_DB`. Modeling that mismatch as a bounded
# multiplicative factor ``k = 10**(AMBIENT_NONSTATIONARITY_DB/10)`` on the
# ambient power estimate:
#
#   subtracted signal estimate      Ŝ = P_measured − N̂
#   bias if the room's ACTUAL noise power during the pilot window is k·N̂
#   instead of N̂:                   bias_power = (k − 1) · N̂
#   bias in dB at signal level S (small-signal slope 10/ln(10)/S):
#       bias_db ≈ (10 / ln(10)) · (k − 1) / (S / N̂)      [S/N̂ = linear SNR]
#
# Budgeting `LINEARITY_SNR_BIAS_BUDGET_FRACTION` of the tolerance for this
# bias (leaving the rest for ordinary estimator/measurement jitter) and
# solving for the linear SNR gives the minimum trustworthy in-band SNR —
# `PILOT_MIN_SNR_DB` works out to ≈12.4 dB with the constants below. Real
# hardware captures that tripped this bug (2026-07-20, jts3) measured ≈26-30
# dB of in-band SNR on the quiet woofer pilot once measured in its own band
# (comfortably above this floor — routed as VALID, not `snr_floor`); this
# threshold exists for the genuinely marginal case (very quiet phone/room),
# not the common one.
AMBIENT_NONSTATIONARITY_DB = 3.0
LINEARITY_SNR_BIAS_BUDGET_FRACTION = 0.5
_pilot_snr_k = 10.0 ** (AMBIENT_NONSTATIONARITY_DB / 10.0)
_pilot_snr_linear_min = (10.0 / math.log(10.0)) * (_pilot_snr_k - 1.0) / (
    LINEARITY_TOLERANCE_DB * LINEARITY_SNR_BIAS_BUDGET_FRACTION
)
PILOT_MIN_SNR_DB = 10.0 * math.log10(_pilot_snr_linear_min)

# Channel-map discriminator (Fix 1, W6.4 — see `_channel_map_ok`). PROVISIONAL
# pending more W6 hardware runs. Derived from the run-5 hardware table
# (2026-07-18/19, jts3): woofer pilots showed +22-30 dB TARGET rise / +<=2 dB
# CROSS rise, tweeter pilots +27 dB TARGET rise / +1.9 dB CROSS rise — both
# comfortably clear of these thresholds even though concurrent room LF rumble
# had put the tweeter pilot's TOTAL in-band energy fraction (the pre-fix test)
# at a coin flip (-51.8 dBFS in-band vs -51.1 dBFS of simultaneous woofer-band
# room noise, against a -78.9 dBFS ambient floor — 27 dB of real, ignored SNR).
CHANNEL_MAP_TARGET_RISE_DB = 12.0
CHANNEL_MAP_CROSS_RISE_DB = 6.0

# VERIFY tracking-error smoothing: 1/6-octave, the constant design §5.2 names
# for the pass/fail comparison (previously 1/24-oct, a display-grade
# smoothing far finer than the design spec).
VERIFY_TRACKING_SMOOTHING_FRACTION = 6

# VERIFY tracking MAX comparator (W6.7 ruling 1): a bin is excluded from the
# max-tracking comparator when the PREDICTED sum sits more than this many dB
# below its own median level over the tracking band. Inside a predicted
# interference notch, depth agreement is hypersensitive to sub-dB/sub-degree
# branch differences and is not a meaningful tracking signal — the W6 run-7
# hardware failure (3.05 dB rms / 27.83 dB max) was entirely a shifted
# predicted notch, not a broadband divergence. RMS stays full-band (it
# already behaves sanely — see `_analyze_verify`). PROVISIONAL pending W6
# bench distributions on notch depth/shift variability.
VERIFY_NOTCH_EXCLUSION_DB = 12.0

ANALYSIS_KIND = "jts_program_analysis"


@dataclass(frozen=True)
class MeasurementGeometry:
    """Declared physical geometry the analysis corrects for.

    ``mic_distance_m`` is the prescribed on-axis mic distance (~1 m, design
    §5.2); ``driver_spacing_m`` is the declared woofer↔tweeter spacing. Their
    deterministic parallax ``(√(r²+d²)−r)/c`` is subtracted from the measured
    delay so what remains is the electrical branch delay to apply.
    """

    driver_spacing_m: float = 0.0
    mic_distance_m: float = 1.0
    speed_of_sound_m_s: float = DEFAULT_SOUND_SPEED_M_S

    def parallax_us(self) -> float:
        """The deterministic mic-parallax term, in µs.

        Aim assumption (design §5.2's placement prompt): the mic sits ON the
        reference axis — the tweeter's axis at tweeter height — at distance
        ``r = mic_distance_m``, so the tweeter path is exactly ``r`` and the
        OTHER driver (the woofer, ``d = driver_spacing_m`` off-axis) carries
        the full geometric excess ``√(r²+d²) − r``. That excess inflates the
        measured woofer-minus-tweeter arrival difference; subtracting it
        leaves the electrical branch delay. A mic placed off the tweeter axis
        splits the excess between the drivers and this correction over-counts
        — the placement screen owns keeping that assumption true.
        """
        r = float(self.mic_distance_m)
        d = float(self.driver_spacing_m)
        c = float(self.speed_of_sound_m_s)
        if r <= 0 or c <= 0 or d <= 0:
            return 0.0
        extra_m = math.sqrt(r * r + d * d) - r
        return extra_m / c * 1e6


@dataclass(frozen=True)
class MeasurementPriors:
    """Per-analysis priors the program itself does not carry.

    ``crossover_fc_hz`` scopes the overlap band (trims / alignment / ripple) and
    the VERIFY window; ``align_search_ms`` bounds the delay search;
    ``target_capture_dbfs`` is the MEASURE capture-peak target the CHECK gain
    solve aims for. ``predicted_sum`` is the MEASURE-predicted summed magnitude
    ``(freqs_hz, magnitude_db)`` VERIFY compares against.

    ``measure_tweeter_sweep_lo_hz``/``measure_woofer_sweep_hi_hz`` carry the
    MEASURE program's actual per-driver sweep bounds forward to VERIFY (§5.6
    fix) — ``predicted_sum`` was itself built only inside that true overlap
    (see ``_overlap_band_hz``), so VERIFY's tracking comparison must trust the
    SAME band; a wider nominal Fc±1-octave band would compare real VERIFY
    capture data against sub-floor noise inherited from an unexcited MEASURE
    branch. ``None`` (legacy callers) falls back to the unclamped nominal band.
    """

    crossover_fc_hz: float | None = None
    align_search_ms: float = DEFAULT_ALIGN_SEARCH_MS
    target_capture_dbfs: float = DEFAULT_TARGET_CAPTURE_DBFS
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    ambient_report: Mapping[str, Any] | None = None
    measure_tweeter_sweep_lo_hz: float | None = None
    measure_woofer_sweep_hi_hz: float | None = None


@dataclass(frozen=True)
class SegmentLocation:
    """Where one program segment landed in the capture, and its integrity."""

    segment_id: str
    kind: str
    role: str | None
    scheduled_start: int
    located_start: int
    residual_samples: float
    confidence: float
    peak_dbfs: float
    clipped: bool


@dataclass(frozen=True)
class DriftEstimate:
    """In-capture clock-drift estimate + the glitch verdict (design §5.6.3).

    ``repeat_level_delta_db`` is the woofer-repeat in-band-RMS level
    disagreement (see ``_estimate_drift``'s docstring) — one of the three
    glitch inputs, threaded through here (not just logged transiently at
    ``program_analysis.glitch``) so a caller building a durable diagnostic
    record (e.g. the crossover v2 conductor's per-capture diag event) has it
    on BOTH a passing and a failing capture, not only the WARN-level line a
    glitch fires. Defaults to ``0.0`` for legacy construction sites that
    predate this field.
    """

    epsilon_ppm: float
    baselines_ppm: Mapping[str, float]
    max_residual_samples: float
    glitch_detected: bool
    repeat_level_delta_db: float = 0.0


@dataclass(frozen=True)
class DriverResponse:
    """One driver's gated complex response, calibrated if a cal was supplied."""

    role: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    complex_tf: np.ndarray
    gating: dict[str, Any]
    snr: dict[str, Any] | None
    validity_floor_hz: float | None


@dataclass(frozen=True)
class AlignmentEstimate:
    """Tweeter-vs-woofer relative delay + polarity (design §5.6.5).

    Sign convention (pinned by test): ``delay_us`` is
    ``(D_woofer − D_tweeter)`` after parallax removal, so **positive delay_us ⇒
    the tweeter's acoustic arrival is EARLIER and the tweeter branch must be
    delayed by that amount** to time-align the crossover.

    ``status`` is :data:`ALIGNMENT_OK` for a trustworthy estimate. When the
    correlation peak lands at (or within one sample of) the ±search-window
    edge, the true delay likely exceeds the geometry prior and the windowed
    peak is a clamped artifact — ``status`` is
    :data:`ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW` and ``confidence`` is forced
    to 0.0; callers must not apply ``delay_us`` from such a result.
    """

    delay_us: float
    raw_delay_us: float
    parallax_us: float
    polarity: str  # "normal" | "inverted"
    polarity_sign: int  # +1 | -1
    polarity_agrees_with_sum: bool
    confidence: float
    status: str = ALIGNMENT_OK


@dataclass(frozen=True)
class CrossoverCandidate:
    """The proposed measured candidate (design §5.6.6)."""

    trim_db: Mapping[str, float]
    polarity: str
    delay_us: float
    predicted_ripple_db: float
    confidence: float


@dataclass(frozen=True)
class PilotObservation:
    """One driver's CHECK pilot pair — level, linearity, channel-map sanity.

    ``level_lo_dbfs``/``level_hi_dbfs`` are band-relative, ambient-compensated
    (when an ambient window is available) — see `_pilot_observations`. They
    feed ONLY the linearity verdict (``captured_delta_db`` is a relative
    delta, so the ambient-subtraction bias cancels between the two levels);
    they must never feed an ABSOLUTE-level consumer like the MEASURE gain
    solve (`_solve_gain_plan`) — ambient subtraction shifts the absolute
    value by however much ambient power was removed, which silently retunes
    a consumer that expects a true signal-peak reference (a review finding:
    threading these into the gain solve moved its captured-peak target
    13-17 dB hotter on the two real captures). ``peak_lo_dbfs``/
    ``peak_hi_dbfs`` are the dedicated, NON-ambient-subtracted levels
    `_solve_gain_plan` reads instead — the exact pre-existing full-band
    `_peak_dbfs`, kept verbatim (see `_pilot_observations`'s docstring for
    why an in-band variant was tried and rejected), preserving
    ``MeasurementPriors.target_capture_dbfs``'s documented capture-PEAK
    semantics exactly as before.

    ``snr_valid`` is True when the quiet (lo) pilot's in-band SNR clears
    `PILOT_MIN_SNR_DB`, i.e. the ambient-subtracted estimate (and therefore
    ``linearity_ok``) is trustworthy; when False, ``linearity_ok`` is forced
    True (an untrustworthy estimate must never register as a linearity
    FAILURE — the caller routes on ``snr_valid`` instead, honestly attributing
    the room/positioning cause rather than the phone's AGC). Defaults to True
    so a caller constructing one directly (fixtures, legacy call sites)
    without an opinion on SNR gets the pre-fix "trust the delta" behavior.

    ``snr_db`` is the actual quiet-pilot in-band SNR estimate ``snr_valid``
    is thresholded from (`_pilot_in_band_snr_db`) — kept as a number, not
    just the pass/fail bool, so a diagnostic consumer (the CHECK diag log
    event) can see how close a borderline capture ran. ``+inf`` when there
    is no ambient window to validate against (nothing to distrust — see
    `_pilot_in_band_snr_db`), matching ``snr_valid``'s default-True stance.

    ``channel_map_target_rise_db``/``channel_map_cross_rise_db`` are the two
    rise numbers `_channel_map_ok` computed on the way to ``channel_map_ok``
    (this driver's own band above ambient, and the worst/failing other
    band's rise above ITS ambient) — diagnostic only, ``None`` when there is
    no ambient window (the fallback total-energy-fraction test has no rise
    concept) or, for the cross figure, when there are no other roles to
    compare against.
    """

    role: str
    level_lo_dbfs: float
    level_hi_dbfs: float
    programmed_delta_db: float
    captured_delta_db: float
    linearity_ok: bool
    channel_map_ok: bool
    snr_valid: bool = True
    peak_lo_dbfs: float = DBFS_FLOOR
    peak_hi_dbfs: float = DBFS_FLOOR
    snr_db: float = math.inf
    channel_map_target_rise_db: float | None = None
    channel_map_cross_rise_db: float | None = None


@dataclass(frozen=True)
class GainPlan:
    """Solved MEASURE digital gains (design §5.2)."""

    gain_db: Mapping[str, float]
    predicted_peak_dbfs: float
    snr_floor_ok: bool


@dataclass(frozen=True)
class ProgramAnalysis:
    """The deterministic result of one ``(program, capture)`` pair."""

    phase: str
    program_id: str
    locations: tuple[SegmentLocation, ...]
    drift: DriftEstimate | None = None
    driver_responses: tuple[DriverResponse, ...] = ()
    alignment: AlignmentEstimate | None = None
    candidate: CrossoverCandidate | None = None
    ambient_report: dict[str, Any] | None = None
    pilots: tuple[PilotObservation, ...] = ()
    linearity_ok: bool | None = None
    channel_map_ok: bool | None = None
    # Aggregate of ``PilotObservation.snr_valid`` across pilots (``all(...)``);
    # ``None`` when there are no pilots (same "no evidence" convention as
    # ``linearity_ok``). False means at least one pilot's quiet-side in-band
    # SNR was too low to trust the ambient-subtracted linearity estimate —
    # the conductor routes this to `REASON_SNR_FLOOR`, never
    # `REASON_AGC_BEHAVIORAL_FAIL` (see `crossover_v2_flow._consume_check`).
    pilot_snr_ok: bool | None = None
    gain_plan: GainPlan | None = None
    summed_response: DriverResponse | None = None
    summed_ripple_db: float | None = None
    verify_tracking: dict[str, Any] | None = None
    # MEASURE-predicted applied summed magnitude ``(freqs_hz, magnitude_db)`` —
    # the flattest-achievable aligned sum for the candidate (design §5.6.6). The
    # v2 conductor hands this to the VERIFY analysis as
    # ``MeasurementPriors.predicted_sum`` so VERIFY's PASS is |measured −
    # predicted| ≤ ±1.5 dB (design §5.2), not merely the summed ripple.
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    glitch_detected: bool = False


# --------------------------------------------------------------------------- #
# low-level signal helpers
# --------------------------------------------------------------------------- #


def _peak_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return DBFS_FLOOR
    peak = float(np.max(np.abs(x)))
    if peak <= 0 or not math.isfinite(peak):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(peak))


def _has_clipped_run(
    x: np.ndarray, *, threshold: float = CLIP_ABS_THRESHOLD, run: int = CLIP_RUN_SAMPLES
) -> bool:
    """True if ``x`` has a run of ``run`` consecutive samples at ≥ full scale."""
    if x.size < run:
        return False
    at_fs = np.abs(x) >= threshold
    if not bool(np.any(at_fs)):
        return False
    # Longest run of True via reset-on-False cumulative counting.
    count = 0
    for flag in at_fs:
        if flag:
            count += 1
            if count >= run:
                return True
        else:
            count = 0
    return False


def _locate(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int,
    max_capture_s: float,
):
    """Matched-filter ``stimulus`` in ``capture``; return the alignment result."""
    return cross_correlation_alignment(
        capture,
        stimulus,
        sample_rate=sample_rate,
        max_capture_s=max_capture_s,
    )


def _analytic_envelope(x: np.ndarray) -> np.ndarray:
    from scipy.signal import hilbert

    return np.abs(hilbert(np.asarray(x, dtype=np.float64)))


def _parabolic_peak(values: np.ndarray, idx: int) -> float:
    """Sub-sample offset of a peak at integer ``idx`` via 3-point parabola.

    The refinement is clamped to ±1 bin: a true local maximum refines within
    ±0.5 bin, so a larger offset means the three points are near-degenerate
    (tiny ``denom``) and the parabola vertex is an extrapolation artifact —
    unclamped, a flat-topped correlation once "refined" a 96-bounded peak out
    to 128 samples. In that case the integer peak is the honest answer.
    """
    if idx <= 0 or idx >= values.size - 1:
        return float(idx)
    y0, y1, y2 = float(values[idx - 1]), float(values[idx]), float(values[idx + 1])
    denom = y0 - 2.0 * y1 + y2
    if denom == 0.0:
        return float(idx)
    offset = 0.5 * (y0 - y2) / denom
    if not -1.0 <= offset <= 1.0:
        return float(idx)
    return idx + offset


def _subsample_separation(
    capture: np.ndarray,
    arrival_a: int,
    arrival_b: int,
    length: int,
) -> float:
    """Sub-sample separation ``arrival_b − arrival_a`` of two identical stimuli.

    Cross-correlates the two captured windows (same stimulus + same room IR, so
    the peak is sharp) and refines it on the upsampled analytic envelope —
    Gamper's repeat-ratio idea. Returns the refined ``(arrival_b − arrival_a)``.
    """
    from scipy.signal import correlate

    a = np.asarray(capture[arrival_a:arrival_a + length], dtype=np.float64)
    b = np.asarray(capture[arrival_b:arrival_b + length], dtype=np.float64)
    n = min(a.size, b.size)
    if n < 8:
        return float(arrival_b - arrival_a)
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    corr = correlate(b, a, mode="full", method="fft")
    env = _analytic_envelope(corr)
    peak = int(np.argmax(env))
    refined = _parabolic_peak(env, peak)
    lag = refined - (n - 1)  # b ≈ a shifted right by lag
    return float((arrival_b - arrival_a) + lag)


def _bandlimit(ir: np.ndarray, sample_rate: int, lo_hz: float, hi_hz: float) -> np.ndarray:
    """Zero-phase band-pass an IR by masking its spectrum to ``[lo, hi]``."""
    n = ir.size
    spectrum = np.fft.rfft(ir)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    spectrum = spectrum * mask
    return np.fft.irfft(spectrum, n=n)


def _gcc_phat(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
    max_lag_samples: float,
):
    """Band-limited GCC-PHAT of ``a`` vs ``b``; ``a ≈ b`` shifted right by the lag.

    Returns ``(lag_samples, polarity_sign, confidence, at_edge)``. The
    cross-power is phase-transform weighted **only inside ``band_hz``**
    (whitening the near-zero out-of-band bins otherwise piles a spurious peak
    near zero lag); the correlation is ×``upsample`` FFT-interpolated and
    parabolically refined. ``polarity_sign`` is the sign of the (signed)
    correlation at the peak, and ``confidence`` mirrors
    ``cross_correlation_alignment``'s primary-over-secondary margin.

    ``at_edge`` is True when the peak lands within one native sample of the
    ±``max_lag_samples`` search bound — the true peak is likely OUTSIDE the
    window and the returned lag is a clamped artifact the caller must refuse.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    L = max(a.size, b.size)
    n = 1
    while n < 2 * L:
        n *= 2
    A = np.fft.rfft(a, n=n)
    B = np.fft.rfft(b, n=n)
    R = A * np.conj(B)
    mag = np.abs(R)
    mag[mag < 1e-12] = 1e-12
    R_phat = R / mag
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    in_band = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    R_phat = R_phat * in_band
    m = n * upsample
    cc = np.fft.irfft(R_phat, n=m) * upsample
    # Circular-lag axis: index i → lag i for i<=m/2 else i-m; native = /upsample.
    max_lag_up = int(round(max_lag_samples * upsample))
    max_lag_up = max(1, min(max_lag_up, m // 2 - 1))
    idxs = np.concatenate(
        [np.arange(0, max_lag_up + 1), np.arange(m - max_lag_up, m)]
    )
    window = cc[idxs]
    peak_local = int(np.argmax(np.abs(window)))
    peak_idx = int(idxs[peak_local])
    # Parabolic refine on |cc| around the (unwrapped) peak.
    abs_cc = np.abs(cc)
    refined = _parabolic_peak(abs_cc, peak_idx)
    circ = refined if refined <= m / 2 else refined - m
    lag_samples = circ / upsample
    polarity_sign = 1 if cc[peak_idx] >= 0 else -1
    primary = float(abs_cc[peak_idx])
    # Secondary: strongest competitor outside the correlation main lobe. A
    # band-limited correlation's main lobe is ~1/bandwidth wide, so a fixed
    # 1-sample exclusion would sit on the main lobe and read a near-primary
    # "secondary" (spuriously low confidence). Exclude one main-lobe half-width.
    bandwidth = max(1.0, band_hz[1] - band_hz[0])
    exclude = max(upsample, int(round(sample_rate / bandwidth * upsample)))
    masked = abs_cc[idxs].copy()
    for j, gi in enumerate(idxs):
        if abs(gi - peak_idx) <= exclude or abs(gi - peak_idx) >= m - exclude:
            masked[j] = 0.0
    secondary = float(masked.max()) if masked.size else 0.0
    confidence = max(0.0, (primary - secondary) / primary) if primary > 0 else 0.0
    max_lag_native = max_lag_up / upsample
    at_edge = abs(lag_samples) >= max_lag_native - 1.0
    return lag_samples, polarity_sign, confidence, at_edge


def _complex_tf(
    ir: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int,
    calibration: "CalibrationCurve | None",
):
    """Complex TF of an IR on a fixed grid, with the mic cal folded in (real)."""
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    H = np.fft.rfft(ir, n=n_fft)
    if calibration is not None:
        correction_db = calibration_mod.apply_calibration_curve(
            freqs, np.zeros_like(freqs), calibration
        )
        H = H * np.power(10.0, correction_db / 20.0)
    return freqs.astype(np.float64), H


def _band_average_db(freqs: np.ndarray, magnitude_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        raise ValueError("overlap band has no frequency bins")
    power = np.power(10.0, magnitude_db[mask] / 10.0)
    return 10.0 * math.log10(max(float(np.mean(power)), 1e-12))


# --------------------------------------------------------------------------- #
# locate + integrity (all phases)
# --------------------------------------------------------------------------- #


def _earliest_strong_peak(
    capture: np.ndarray, stimulus: np.ndarray, *, frac: float = 0.6
) -> int:
    """Index of the EARLIEST normalized-correlation peak within ``frac`` of max.

    The first stimulus can be bit-identical to a later repeat (MEASURE's woofer
    pair) or share its SHAPE at a different level (CHECK's lo/hi pilot pair), so
    a plain global argmax — or a raw-amplitude threshold — can lock onto the
    wrong occurrence. This uses a locally energy-normalized matched filter
    (cosine similarity at each lag), so a quieter-but-identical first occurrence
    scores the same as a louder later one; taking the earliest lag within
    ``frac`` of the max then picks the true first occurrence, while an
    out-of-band interloper stays below the fraction.
    """
    from scipy.signal import correlate

    cap = np.asarray(capture, dtype=np.float64)
    stim = np.asarray(stimulus, dtype=np.float64)
    cap = cap - cap.mean()
    stim = stim - stim.mean()
    L = stim.size
    if cap.size < L or L == 0:
        return 0
    stim_norm = float(np.linalg.norm(stim))
    if stim_norm <= 0.0:
        return 0
    num = correlate(cap, stim, mode="valid", method="fft")
    local_energy = correlate(cap * cap, np.ones(L), mode="valid", method="fft")
    local_norm = np.sqrt(np.maximum(local_energy, 0.0))
    # Floor the denominator so silent (near-zero-energy) windows don't blow the
    # ratio up; a floor at a small fraction of the loudest window is enough.
    floor = 1e-6 * float(local_norm.max()) + 1e-12
    ncc = np.abs(num) / (local_norm * stim_norm + floor)
    peak = float(ncc.max()) if ncc.size else 0.0
    if peak <= 0.0:
        return 0
    return int(np.argmax(ncc >= frac * peak))


def _global_offset(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int
) -> tuple[int, ProgramSegment, dict[str, np.ndarray]]:
    """Locate the first stimulus → integer global offset G. Caches stimuli.

    The whole-capture matched filter runs at :data:`LOCATOR_RATE_HZ` (mirrors
    ``driver_acoustics._capture_to_magnitude``'s 16 kHz downsampled locate) so
    the largest correlation is over a 3× smaller array; the coarse arrival is
    then refined at the full rate inside a tiny window around it, so the
    returned offset is still full-rate-exact.
    """
    from scipy.signal import resample_poly

    stimuli: dict[str, np.ndarray] = {}
    first = None
    for seg in program.segments:
        if seg.kind in STIMULUS_KINDS:
            first = seg
            break
    if first is None:
        raise ValueError("program has no stimulus segment to locate against")
    stim = segment_stimulus(first)
    stimuli[first.segment_id] = stim

    down = max(1, int(round(sample_rate / LOCATOR_RATE_HZ)))
    if down > 1:
        capture_lo = resample_poly(capture, 1, down)
        stim_lo = resample_poly(np.asarray(stim, dtype=np.float64), 1, down)
    else:
        capture_lo = capture
        stim_lo = np.asarray(stim, dtype=np.float64)
    coarse = _earliest_strong_peak(capture_lo, stim_lo) * down

    # Full-rate refinement in a ±4·down window around the coarse arrival —
    # bounded cost (one small correlate), full-rate precision.
    margin = 4 * down
    lo = max(0, coarse - margin)
    hi = min(capture.size, coarse + stim.size + margin)
    window = capture[lo:hi]
    if window.size >= stim.size:
        arrival = lo + _earliest_strong_peak(window, stim)
    else:
        arrival = coarse
    global_offset = arrival - first.start_sample
    return global_offset, first, stimuli


def _locate_segments(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    stimuli: dict[str, np.ndarray],
) -> list[SegmentLocation]:
    """Locate every segment at scheduled offset ± window; record integrity."""
    search = int(round(SEGMENT_SEARCH_S * sample_rate))
    out: list[SegmentLocation] = []
    for seg in program.segments:
        scheduled = global_offset + seg.start_sample
        if seg.kind in STIMULUS_KINDS:
            stim = stimuli.get(seg.segment_id)
            if stim is None:
                stim = segment_stimulus(seg)
                stimuli[seg.segment_id] = stim
            lo = max(0, scheduled - search)
            hi = min(capture.size, scheduled + seg.n_samples + search)
            window = capture[lo:hi]
            if window.size >= stim.size:
                res = _locate(
                    window, stim, sample_rate=sample_rate,
                    max_capture_s=window.size / sample_rate + 1.0,
                )
                located = lo + int(res.lag_samples)
                confidence = float(res.confidence)
            else:
                located = scheduled
                confidence = 0.0
            seg_samples = capture[located:located + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=located,
                residual_samples=float(located - scheduled),
                confidence=confidence,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
        else:
            seg_samples = capture[max(0, scheduled):scheduled + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=scheduled,
                residual_samples=0.0,
                confidence=1.0,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
    return out


# --------------------------------------------------------------------------- #
# drift (MEASURE)
# --------------------------------------------------------------------------- #


def _estimate_drift(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    locations: Sequence[SegmentLocation],
) -> DriftEstimate:
    by_id = {loc.segment_id: loc for loc in locations}
    w1 = by_id.get("sweep_w")
    w2 = by_id.get("sweep_w_rep")
    # Only the SWEEP-kind stimuli anchor the drift baselines / residual guard.
    # A v2 MEASURE program may open with a leading pilot pair (linearity probe,
    # design §5.2) whose short/quiet windows are located more coarsely; folding
    # them into the residual guard would manufacture spurious desync. Pilots are
    # judged separately (their own linearity verdict), never as a drift baseline.
    stimulus_locs = [loc for loc in locations if loc.kind == KIND_SWEEP]

    baselines: dict[str, float] = {}
    epsilon = 0.0
    if w1 is not None and w2 is not None:
        seg_w = program.segment("sweep_w")
        scheduled_sep = program.segment("sweep_w_rep").start_sample - seg_w.start_sample
        if scheduled_sep > 0:
            # Primary: sub-sample separation of the two identical woofer sweeps
            # (τ cancels; drift is the ratio). Design §3.1 / §5.6.3.
            measured_sep = _subsample_separation(
                capture, w1.located_start, w2.located_start, seg_w.n_samples
            )
            epsilon = measured_sep / scheduled_sep - 1.0
            baselines["woofer_repeat"] = epsilon * 1e6
            # Cross-check baseline: the integer-located separation ratio (no
            # sub-sample refinement) — a coarse independent view of the same span.
            eps_int = (w2.located_start - w1.located_start) / scheduled_sep - 1.0
            baselines["woofer_repeat_integer"] = eps_int * 1e6

    # Per-driver-demeaned schedule residual after applying ε. A driver's own
    # acoustic delay is a constant offset (removed by demeaning), so this does
    # NOT flag the real tweeter-vs-woofer delay; it catches a within-driver
    # desync (a dropped buffer between a driver's own repeated sweeps). A
    # mid-program dropped buffer between the two woofer sweeps instead surfaces
    # as an out-of-band ε (the ppm bound below), because the repeat spans it.
    # NOTE: with one located sweep per role the demeaned residual is
    # identically zero, so this guard only ACTIVATES for a role with ≥2
    # located sweeps — in the 2-way 3-sweep MEASURE program that is the woofer
    # pair only; the single-sweep tweeter is covered by the ε ppm bound alone.
    # A future program shape with per-role repeats gets the residual guard on
    # every role for free.
    groups: dict[Any, list[float]] = {}
    for loc in stimulus_locs:
        start = program.segment(loc.segment_id).start_sample
        residual = loc.located_start - (global_offset + start * (1.0 + epsilon))
        groups.setdefault(loc.role, []).append(residual)
    max_residual = 0.0
    for resids in groups.values():
        mean = sum(resids) / len(resids)
        for r in resids:
            max_residual = max(max_residual, abs(r - mean))

    # Woofer-repeat LEVEL agreement (design §5.2): the two woofer sweeps are
    # bit-identical stimuli, so a clean capture reproduces the same captured
    # level for both. Measured band-relative — in-band RMS over the woofer's
    # OWN declared band (`_band_power`, the same Hann+bandpass mechanism
    # `_pilot_observations` uses), after trimming the composer's fixed edge
    # fade (`_pilot_trim_fade`) — never full-band single-sample PEAK: a
    # low-frequency, room-mode-excited sweep's full-band peak is an unstable
    # estimator (the loudest sample jumps between otherwise-identical sweeps),
    # the same bug class already fixed for the channel-map discriminator
    # (#1594) and the pilot linearity gate (#1615). Two real hardware captures
    # (Dayton iMM-6C AND UMIK-2, 2026-07-20) measured two genuinely-identical
    # woofer sweeps 0.64 dB apart by full-band peak — enough to trip this gate
    # — but only 0.06-0.24 dB apart by in-band RMS. Real AGC gain-riding (this
    # gate's actual purpose) still shows up in-band (a uniform per-sweep gain
    # shift survives band-limiting), so this keeps the gate's teeth while
    # dropping the false rejection. A larger delta REUSES the
    # drift-baselines-disagree glitch verdict — never a new user-facing code.
    repeat_level_delta_db = 0.0
    repeat_level_disagrees = False
    if w1 is not None and w2 is not None:
        level_seg_w = program.segment("sweep_w")
        if level_seg_w.f1_hz is None or level_seg_w.f2_hz is None:
            raise ValueError("woofer sweep segment has no declared band")
        w1_samples = _pilot_trim_fade(
            capture[w1.located_start:w1.located_start + level_seg_w.n_samples], sample_rate,
        )
        w2_samples = _pilot_trim_fade(
            capture[w2.located_start:w2.located_start + level_seg_w.n_samples], sample_rate,
        )
        level_w1 = _band_rms_dbfs(w1_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        level_w2 = _band_rms_dbfs(w2_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        repeat_level_delta_db = abs(level_w1 - level_w2)
        repeat_level_disagrees = repeat_level_delta_db > REPEAT_LEVEL_TOLERANCE_DB

    glitch = (
        abs(epsilon) * 1e6 > MAX_DRIFT_PPM
        or max_residual > GLITCH_RESIDUAL_SAMPLES
        or repeat_level_disagrees
    )
    if glitch:
        log_event(
            logger,
            "program_analysis.glitch",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            epsilon_ppm=round(epsilon * 1e6, 2),
            max_residual_samples=round(max_residual, 2),
            repeat_level_delta_db=round(repeat_level_delta_db, 3),
        )
    return DriftEstimate(
        epsilon_ppm=epsilon * 1e6,
        baselines_ppm=baselines,
        max_residual_samples=max_residual,
        glitch_detected=glitch,
        repeat_level_delta_db=repeat_level_delta_db,
    )


# --------------------------------------------------------------------------- #
# per-driver response + alignment + candidate (MEASURE)
# --------------------------------------------------------------------------- #


def _deconvolve_window(
    capture: np.ndarray,
    segment: ProgramSegment,
    anchor: int,
    sample_rate: int,
    *,
    epsilon: float = 0.0,
    pre_guard_s: float = DECONV_PRE_GUARD_S,
    tail_s: float = 0.5,
) -> tuple[np.ndarray, int]:
    """Deconvolve one sweep → ``(full_ir, pre_guard_samples)``.

    The window starts ``pre_guard_samples`` before ``anchor`` (the scheduled
    capture position ``global_offset + start``), so it fully contains the sweep
    even though the global offset folds in the first driver's small acoustic
    delay. With a shared anchor + pre-guard across drivers, each deconvolved IR's
    direct peak lands at ``pre_guard_samples`` ± the relative delay.

    ``epsilon`` divides the measured clock drift out (design §3.1): the captured
    sweep is stretched by ``(1+ε)``, so the reference is resampled to match
    before inversion — keeping the deconvolution sharp (and the delay estimate
    accurate) under drift instead of smearing the IR.
    """
    stim = segment_stimulus(segment)
    if epsilon != 0.0:
        from scipy.signal import resample

        stretched_len = int(round(stim.size * (1.0 + epsilon)))
        if stretched_len > 0:
            stim = resample(np.asarray(stim, dtype=np.float64), stretched_len)
    pre = int(round(pre_guard_s * sample_rate))
    tail = int(round(tail_s * sample_rate))
    window_start = anchor - pre
    lo = max(0, window_start)
    pre_effective = anchor - lo  # shrinks if the window clamps at the capture head
    hi = min(capture.size, anchor + segment.n_samples + tail)
    window = np.asarray(capture[lo:hi], dtype=np.float64)
    if window.size < stim.size:
        raise ValueError(f"deconvolution window for {segment.segment_id!r} too short")
    full_ir = deconv.regularized_deconvolution_full(
        window, np.asarray(stim, dtype=np.float64), sample_rate
    )
    return full_ir, pre_effective


def _gate_floor_hz(fragment: Mapping[str, Any]) -> float | None:
    """Validity floor from a gate fragment, or ``None`` when ungateable.

    Shared by every caller that windows an IR through
    :func:`gating.gate_impulse_response`: ``floor_source is None`` means the
    IR was never gated (silent/NaN capture, no room to search), so the
    fragment's ``f_valid_floor_hz`` key is not a real floor even though it's
    present — mirrors :mod:`gating`'s own ``applied`` rule.
    """
    if fragment.get("floor_source") is None:
        return None
    floor = fragment.get("f_valid_floor_hz")
    return float(floor) if isinstance(floor, (int, float)) else None


def _driver_response(
    role: str,
    full_ir: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None",
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    n_fft: int,
) -> DriverResponse:
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, sample_rate, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
    applied = fragment["floor_source"] is not None
    gating_block = {
        "applied": applied,
        "exempt_reason": None,
        **fragment,
    }
    validity_floor_hz = _gate_floor_hz(fragment)

    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    mag_db = 20.0 * np.log10(np.maximum(np.abs(H), 1e-12))

    snr_block = None
    if ambient_report is not None and fc_hz is not None:
        capture_bands = snr_policy.magnitude_band_levels(freqs, mag_db)
        _domain, noise_bands = snr_policy.unwrap_noise_report(ambient_report)
        snr_block = snr_policy.band_snr_verdicts(
            decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
            capture_bands=capture_bands,
            noise_bands=noise_bands,
            noise_floor_dbfs_scalar=None,
            relevant_hz=(fc_hz / OVERLAP_OCTAVE_RATIO, fc_hz * OVERLAP_OCTAVE_RATIO),
            model=DRIVER,
            band_method="deconvolved_band_difference",
        )
    return DriverResponse(
        role=role,
        freqs_hz=freqs,
        magnitude_db=mag_db,
        complex_tf=H,
        gating=gating_block,
        snr=snr_block,
        validity_floor_hz=validity_floor_hz,
    )


def _aligned_branch_tf(
    full_ir: np.ndarray,
    sample_rate: int,
    n_fft: int,
    *,
    calibration: "CalibrationCurve | None",
):
    """Delay-referenced, gating-consistent complex TF for the sum prediction.

    :func:`deconv.direct_arrival_window` places each branch's direct peak at the
    same fixed offset (``IR_PRE_MS``) inside the window, so both branches share a
    common time reference (bulk delay removed) WITHOUT a circular roll — a roll
    followed by zero-padding to ``n_fft`` is not shift-invariant and would inject
    a spurious echo into the magnitude.

    The windowed IR is then run through the SAME adaptive reflection gate
    :func:`_driver_response` applies (W6.9 forensics, 2026-07-19): before this
    fix, the prediction composed branches from the fixed ``IR_PRE_MS``/
    ``IR_POST_MS`` window alone, so a room reflection within that 65 ms tail
    was baked into the predicted sum even though VERIFY's measured sum (via
    ``_driver_response``) already reflection-gated it out — a run-7/8 hardware
    failure traced to a 15 cm desk-bounce reflection producing a spurious
    ~1125 Hz null in the FIXED-window prediction that the adaptively-gated
    measured sum never had. Gating a window that already has the peak at a
    fixed local offset preserves that offset (the gate only shortens/tapers
    the TAIL), so the shared time base survives.
    """
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, sample_rate, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    return freqs, H, fragment


def _overlap_band_hz(
    fc_hz: float,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
) -> tuple[float, float]:
    """SSOT overlap band for the GCC alignment, trim solve, ripple, and
    VERIFY-tracking comparisons: the nominal ``Fc ± 1 octave`` band, clamped to
    the TRUE driver-sweep overlap.

    The nominal ``[Fc/OVERLAP_OCTAVE_RATIO, Fc*OVERLAP_OCTAVE_RATIO]`` band
    silently assumes both drivers were excited across the whole span, but each
    driver's MEASURE sweep only covers its own declared band (design §5.4) —
    e.g. a tweeter sweep starting AT Fc means ``[Fc/2, Fc)`` is pure
    deconvolution noise for that branch (the driver was never excited there).
    That noise corrupted the GCC delay/confidence, the trim solve, the
    predicted ripple, and (via the MEASURE-predicted sum) VERIFY's tracking
    comparison — a real hardware run never cleared the alignment confidence
    floor because of it. Clamping ``lo`` UP to the tweeter's actual sweep
    floor and ``hi`` DOWN to the woofer's actual sweep ceiling keeps every one
    of those consumers inside frequencies BOTH branches actually have real
    excited energy. ``None`` bounds (legacy callers with no sweep-segment
    evidence) leave that side at the nominal Fc/octave edge — byte-identical
    to the pre-fix band.
    """
    lo = fc_hz / OVERLAP_OCTAVE_RATIO
    hi = fc_hz * OVERLAP_OCTAVE_RATIO
    if tweeter_sweep_lo_hz is not None:
        lo = max(lo, float(tweeter_sweep_lo_hz))
    if woofer_sweep_hi_hz is not None:
        hi = min(hi, float(woofer_sweep_hi_hz))
    return lo, hi


def _estimate_alignment(
    capture: np.ndarray,
    program: ExcitationProgram,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    fc_hz: float,
    geometry: MeasurementGeometry,
    priors: MeasurementPriors,
    *,
    woofer_full_ir: np.ndarray,
    tweeter_full_ir: np.ndarray,
    pre_samples: int,
) -> AlignmentEstimate:
    seg_w = program.segment("sweep_w")
    seg_t = program.segment("sweep_t")
    lo, hi = _overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
    )

    max_lag = priors.align_search_ms * 1e-3 * sample_rate
    # Both IRs were deconvolved from windows sharing the pre-guard + global
    # offset, so each direct peak sits at ``pre_samples`` ± the relative delay.
    # Slice the SAME [pre−H, pre+H] region from both (bounds the FFT and keeps
    # the shared time base), band-limit to the overlap, then GCC-PHAT: the peak
    # lag is τ + ε·Δstart (tweeter later ⇒ positive).
    half = int(round(0.010 * sample_rate)) + int(math.ceil(max_lag)) + 1
    a = max(0, pre_samples - half)
    b_w = min(woofer_full_ir.size, pre_samples + half)
    b_t = min(tweeter_full_ir.size, pre_samples + half)
    b = min(b_w, b_t)
    ir_w = _bandlimit(np.asarray(woofer_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    ir_t = _bandlimit(np.asarray(tweeter_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    length = min(ir_w.size, ir_t.size)
    ir_w, ir_t = ir_w[:length], ir_t[:length]

    lag_samples, polarity_sign, confidence, at_edge = _gcc_phat(
        ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, max_lag_samples=max_lag,
    )
    # ε-correct: the tweeter's schedule offset is stretched by ε.
    delta_start = seg_t.start_sample - seg_w.start_sample
    tau_samples = lag_samples - epsilon * delta_start
    # delay_us = (D_woofer − D_tweeter) = −τ (τ = D_tweeter − D_woofer).
    raw_delay_us = -tau_samples / sample_rate * 1e6
    parallax_us = geometry.parallax_us()
    delay_us = raw_delay_us - parallax_us

    polarity = "normal" if polarity_sign >= 0 else "inverted"

    status = ALIGNMENT_OK
    if at_edge:
        # A peak clamped at the search bound is not a measurement of the delay —
        # the true delay likely exceeds the geometry prior. Fail explicitly at
        # confidence 0 rather than returning a moderate-confidence wrong value.
        status = ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW
        confidence = 0.0
        log_event(
            logger,
            "program_analysis.alignment_edge",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            lag_samples=round(lag_samples, 3),
            search_window_ms=priors.align_search_ms,
        )

    # Cross-check polarity against the flatter predicted sum.
    agrees = _flatter_sum_polarity(
        capture, program, sample_rate, global_offset, fc_hz, priors,
        woofer_full_ir=woofer_full_ir, tweeter_full_ir=tweeter_full_ir,
    )
    polarity_agrees = agrees == polarity_sign
    return AlignmentEstimate(
        delay_us=delay_us,
        raw_delay_us=raw_delay_us,
        parallax_us=parallax_us,
        polarity=polarity,
        polarity_sign=polarity_sign,
        polarity_agrees_with_sum=polarity_agrees,
        confidence=confidence,
        status=status,
    )


def _predicted_sum(
    W: np.ndarray,
    T: np.ndarray,
    trim_w_db: float,
    trim_t_db: float,
    sign: int,
) -> np.ndarray:
    g_w = 10.0 ** (trim_w_db / 20.0)
    g_t = 10.0 ** (trim_t_db / 20.0)
    return W * g_w + sign * T * g_t


def _ripple_db(freqs: np.ndarray, magnitude: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return float("inf")
    band = magnitude[mask]
    band_db = 20.0 * np.log10(np.maximum(np.abs(band), 1e-12))
    return float(np.max(band_db) - np.min(band_db))


def _solve_trims(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    fc_hz: float,
    *,
    lo_hz: float | None = None,
    hi_hz: float | None = None,
) -> tuple[float, float, float, float]:
    """Level-match trims over ``[lo_hz, hi_hz]`` (default: Fc ± 1 octave).

    ``lo_hz``/``hi_hz`` let a caller narrow the band away from the nominal
    overlap — the candidate's gating-consistent trim solve (`_build_candidate`)
    clamps ``lo_hz`` up to a branch's validity floor when a room reflection
    gates it tighter than the nominal band.
    """
    lo = lo_hz if lo_hz is not None else fc_hz / OVERLAP_OCTAVE_RATIO
    hi = hi_hz if hi_hz is not None else fc_hz * OVERLAP_OCTAVE_RATIO
    level_w = _band_average_db(freqs, 20.0 * np.log10(np.maximum(np.abs(W), 1e-12)), lo, hi)
    level_t = _band_average_db(freqs, 20.0 * np.log10(np.maximum(np.abs(T), 1e-12)), lo, hi)
    target = min(level_w, level_t)  # attenuate the louder branch
    return target - level_w, target - level_t, level_w, level_t


def _flatter_sum_polarity(
    capture, program, sample_rate, global_offset, fc_hz, priors,
    *, woofer_full_ir, tweeter_full_ir,
) -> int:
    n_fft = _n_fft_for(woofer_full_ir, tweeter_full_ir)
    freqs, W, _gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=None)
    _f2, T, _gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=None)
    trim_w, trim_t, _lw, _lt = _solve_trims(freqs, W, T, fc_hz)
    lo = fc_hz / OVERLAP_OCTAVE_RATIO
    hi = fc_hz * OVERLAP_OCTAVE_RATIO
    ripple_pos = _ripple_db(freqs, _predicted_sum(W, T, trim_w, trim_t, +1), lo, hi)
    ripple_neg = _ripple_db(freqs, _predicted_sum(W, T, trim_w, trim_t, -1), lo, hi)
    return 1 if ripple_pos <= ripple_neg else -1


def _n_fft_for(*irs: np.ndarray) -> int:
    longest = max(ir.size for ir in irs)
    return max(8192, 1 << (max(longest, 1) - 1).bit_length())


# --------------------------------------------------------------------------- #
# CHECK helpers
# --------------------------------------------------------------------------- #


def _ambient_from_capture(
    capture: np.ndarray, sample_rate: int, ambient_seg: ProgramSegment, global_offset: int
) -> tuple[np.ndarray, dict[str, Any]]:
    start = max(0, global_offset + ambient_seg.start_sample)
    end = min(capture.size, start + ambient_seg.n_samples)
    samples = capture[start:end]
    return samples, snr_policy.framed_ambient_band_report(samples, sample_rate, percentile=95)


def _band_power(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """Mean-square (linear power) of ``samples`` restricted to ``[f1_hz, f2_hz]``.

    Hann-windowed before :func:`_bandlimit`'s zero-phase FFT bandpass: a raw
    slice (a pilot window, or the ambient window) almost never starts/ends at
    a zero crossing, so an un-windowed brick-wall FFT filter treats the slice
    as implicitly periodic and leaks broadband energy from that boundary
    discontinuity into every band — including a driver's OWN declared band
    read back out of its own capture. The Hann taper (a fixed, length-
    independent mean-square ratio) suppresses that leak; the constant
    windowing loss it introduces cancels out of every comparison that reads
    both sides through this same function (the channel-map TARGET/CROSS rise,
    and the pilot linearity delta), so it does not need correcting for here.

    Returned as LINEAR power (not dB) so a caller can SUBTRACT an ambient
    noise-power estimate before converting to dB — subtraction is only valid
    in the power domain, never on dB values directly.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8:
        return 0.0
    filtered = _bandlimit(x * np.hanning(x.size), sample_rate, f1_hz, f2_hz)
    return float(np.mean(np.square(filtered)))


def _band_rms_dbfs(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """RMS level (dBFS) of ``samples`` restricted to ``[f1_hz, f2_hz]``.

    Thin dB wrapper over :func:`_band_power` (``20·log10(rms) ==
    10·log10(power)``) — see that function's docstring for the windowing
    rationale.
    """
    power = _band_power(samples, sample_rate, f1_hz, f2_hz)
    if power <= 0 or not math.isfinite(power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(power))


def _pilot_trim_fade(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Drop the composer's fixed edge fade from a located pilot segment.

    See `PILOT_FADE_TRIM_S`. Falls back to the untrimmed segment when
    trimming would leave nothing (a pathologically short/corrupt capture) —
    the level estimate then rides the fade too rather than raising here; the
    SNR/linearity gates downstream still catch a genuinely bad capture.
    """
    trim = int(round(PILOT_FADE_TRIM_S * sample_rate))
    if samples.size <= 2 * trim:
        return samples
    return samples[trim:-trim]


def _ambient_subtracted_dbfs(power: float, ambient_power: float) -> float:
    """dB of ``power`` after subtracting ``ambient_power`` (power domain).

    ``ambient_power`` is 0.0 when there is no ambient evidence (a v2
    MEASURE/VERIFY leading pilot pair has no ambient window of its own — see
    `_pilot_verdicts`'s docstring); subtracting zero is a no-op, so this
    degrades to plain in-band RMS in that case.
    """
    signal_power = power - ambient_power if ambient_power > 0 else power
    if signal_power <= 0 or not math.isfinite(signal_power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(signal_power))


def _pilot_in_band_snr_db(power: float, ambient_power: float) -> float:
    """SNR (dB) of the ambient-subtracted estimate: ``(P − N) / N``.

    In the ``P = S + N`` model (measured power = signal + ambient noise
    power), this is exactly ``S / N`` — the linear SNR the
    `PILOT_MIN_SNR_DB` derivation is stated in terms of. Returns ``+inf``
    when there is no ambient evidence to contaminate the estimate (nothing to
    validate against, so nothing to distrust), and ``-inf`` when the pilot's
    measured power does not even exceed the ambient (the estimate is
    unusable).
    """
    if ambient_power <= 0 or not math.isfinite(ambient_power):
        return math.inf
    ratio = power / ambient_power - 1.0
    if ratio <= 0 or not math.isfinite(ratio):
        return -math.inf
    return 10.0 * math.log10(ratio)


def _band_exclusive_pieces(
    other_band: tuple[float, float], own_band: tuple[float, float]
) -> list[tuple[float, float]]:
    """The part(s) of ``other_band`` that fall OUTSIDE ``own_band``.

    Two drivers' declared bands legitimately overlap around the crossover
    point (design §5.2/§5.4 — MEASURE needs response through the Fc overlap
    from both drivers), so a role's own pilot content routinely also falls
    inside the shared part of an adjacent role's declared band — that shared
    part carries no map-discrimination signal. The CROSS test (see
    `_channel_map_ok`) only asks about the EXCLUSIVE remainder of the other
    role's band (interval subtraction; 0, 1, or 2 pieces), where a
    correctly-wired driver's own out-of-band rolloff makes energy absent.
    """
    o1, o2 = other_band
    a1, a2 = own_band
    pieces: list[tuple[float, float]] = []
    if o1 < a1:
        pieces.append((o1, min(o2, a1)))
    if o2 > a2:
        pieces.append((max(o1, a2), o2))
    return [(lo, hi) for lo, hi in pieces if hi > lo]


def _pilot_observations(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    ambient_samples: np.ndarray | None = None,
) -> list[PilotObservation]:
    """Per-role pilot level/linearity/channel-map observations (design §3.4).

    Level is measured band-relative (each pilot's OWN declared band, via
    `_band_power` — same Hann+bandpass mechanism `_channel_map_ok` uses, not a
    second filtering idiom) and, when an ambient window is available (CHECK's
    own leading silence), ambient-power-subtracted before converting to dB —
    fixing the 2026-07-20 bug where a full-band PEAK estimate let LF room
    rumble inflate the quiet pilot's level and compress the captured delta
    (see `LINEARITY_TOLERANCE_DB`'s comment). A v2 MEASURE/VERIFY leading
    pilot pair has no ambient window (`_pilot_verdicts`'s docstring), so
    ``ambient_samples`` is ``None`` there — subtraction degrades to a no-op
    (`_ambient_subtracted_dbfs`) and SNR is trusted unconditionally (nothing
    to validate against).

    The located segment's fixed composer fade (`_pilot_trim_fade`) is trimmed
    before measuring so the RMS estimate rides the steady-state portion, not
    the ramp.

    Low-SNR honest routing: the quiet (lo) pilot is the binding constraint
    (10 dB quieter than hi, same ambient), so its in-band SNR
    (`_pilot_in_band_snr_db`) gates trust. Below `PILOT_MIN_SNR_DB` the
    ambient-subtracted estimate isn't reliable either way —
    ``linearity_ok`` is forced True (never a false FAILURE) and
    ``snr_valid=False`` lets the caller route to the honest "room/
    positioning" reason instead of blaming the phone's AGC (see
    `ProgramAnalysis.pilot_snr_ok` and `crossover_v2_flow._consume_check`).

    ``peak_lo_dbfs``/``peak_hi_dbfs`` are a SEPARATE, non-ambient-subtracted
    measurement: the exact pre-fix `_peak_dbfs` (full-band peak of the
    located, untrimmed samples) `_solve_gain_plan` used before this function
    grew the band-relative/ambient-subtracted level. They exist because
    `_solve_gain_plan` uses a pilot level ABSOLUTELY (``k = level -
    gain_db``, an estimate of the whole capture chain's dB gain), not as a
    delta — feeding it the ambient-subtracted level would silently shift
    that absolute reference by however much ambient power was subtracted
    (measured 13-17 dB across both real captures), retuning
    `MeasurementPriors.target_capture_dbfs`'s documented capture-PEAK target
    hotter than intended. An in-band (band-limited) peak was evaluated as a
    more-robust replacement but empirically introduced its own bandlimiting-
    leakage bias (up to ~1.3 dB on a real capture — worse than "a few
    tenths") whether or not the slice was windowed first, so the exact
    pre-fix computation is kept verbatim for this consumer instead.
    """
    by_id = {loc.segment_id: loc for loc in locations}
    roles = sorted({seg.role for seg in program.segments if seg.kind == KIND_PILOT and seg.role})
    # Every role's declared band, so each role's channel-map check can ask
    # whether energy also rose in every OTHER role's band (the CROSS test).
    # A pilot (stimulus) segment always carries f1_hz/f2_hz — enforced by
    # ProgramSegment.__post_init__ — so a None here means a corrupt schedule.
    role_bands: dict[str, tuple[float, float]] = {}
    for role in roles:
        hi_seg = program.segment(f"pilot_{role}_hi")
        if hi_seg.f1_hz is None or hi_seg.f2_hz is None:
            raise ValueError(f"pilot segment for role {role!r} has no declared band")
        role_bands[role] = (hi_seg.f1_hz, hi_seg.f2_hz)

    ambient_arr = None if ambient_samples is None else np.asarray(ambient_samples)
    if ambient_arr is not None and ambient_arr.size < 8:
        ambient_arr = None
    has_ambient = ambient_arr is not None

    out: list[PilotObservation] = []
    for role in roles:
        lo_seg = program.segment(f"pilot_{role}_lo")
        hi_seg = program.segment(f"pilot_{role}_hi")
        lo_loc = by_id[f"pilot_{role}_lo"]
        hi_loc = by_id[f"pilot_{role}_hi"]
        lo_samples = capture[lo_loc.located_start:lo_loc.located_start + lo_seg.n_samples]
        hi_samples = capture[hi_loc.located_start:hi_loc.located_start + hi_seg.n_samples]

        own_f1, own_f2 = role_bands[role]
        lo_interior = _pilot_trim_fade(lo_samples, sample_rate)
        hi_interior = _pilot_trim_fade(hi_samples, sample_rate)
        lo_power = _band_power(lo_interior, sample_rate, own_f1, own_f2)
        hi_power = _band_power(hi_interior, sample_rate, own_f1, own_f2)
        ambient_power = (
            _band_power(ambient_arr, sample_rate, own_f1, own_f2)
            if ambient_arr is not None
            else 0.0
        )

        level_lo = _ambient_subtracted_dbfs(lo_power, ambient_power)
        level_hi = _ambient_subtracted_dbfs(hi_power, ambient_power)
        programmed_delta = hi_seg.gain_db - lo_seg.gain_db
        captured_delta = level_hi - level_lo

        lo_snr_db = _pilot_in_band_snr_db(lo_power, ambient_power) if has_ambient else math.inf
        snr_valid = lo_snr_db >= PILOT_MIN_SNR_DB
        linearity_ok = (
            True if not snr_valid
            else abs(captured_delta - programmed_delta) <= LINEARITY_TOLERANCE_DB
        )

        # Gain-solve reference: exact pre-fix full-band peak (see the
        # docstring above) — deliberately NOT the ambient-subtracted level.
        peak_lo = _peak_dbfs(lo_samples)
        peak_hi = _peak_dbfs(hi_samples)

        own_band = role_bands[role]
        other_bands = tuple(
            piece
            for other_role, other_band in role_bands.items()
            if other_role != role
            for piece in _band_exclusive_pieces(other_band, own_band)
        )
        channel_ok, channel_target_rise_db, channel_cross_rise_db = _channel_map_ok(
            hi_samples, sample_rate, hi_seg,
            ambient_samples=ambient_samples, other_bands=other_bands,
        )
        out.append(PilotObservation(
            role=role,
            level_lo_dbfs=level_lo,
            level_hi_dbfs=level_hi,
            programmed_delta_db=programmed_delta,
            captured_delta_db=captured_delta,
            linearity_ok=linearity_ok,
            channel_map_ok=channel_ok,
            snr_valid=snr_valid,
            peak_lo_dbfs=peak_lo,
            peak_hi_dbfs=peak_hi,
            snr_db=lo_snr_db,
            channel_map_target_rise_db=channel_target_rise_db,
            channel_map_cross_rise_db=channel_cross_rise_db,
        ))
    return out


def _pilot_verdicts(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
) -> tuple[tuple[PilotObservation, ...], bool | None, bool | None, bool | None]:
    """Pilot observations + the aggregate linearity / channel-map / SNR verdicts.

    ``None`` verdicts when the program carries no pilots (a legacy MEASURE /
    VERIFY program), so a caller can distinguish "no pilot evidence" from
    "pilot evidence, all clean". Shared by CHECK, and by v2 MEASURE / VERIFY
    whose leading pilot pair (design §5.2) carries per-capture linearity
    evidence CHECK-only verification cannot. Unlike CHECK, a MEASURE/VERIFY
    pilot pair has no leading ambient window of its own (no silence precedes
    it), so its channel-map check falls back to `_channel_map_ok`'s
    total-in-band-energy-fraction test (unchanged from before Fix 1), and its
    ``pilot_snr_ok`` is always trusted (``True``) — see `_pilot_observations`.
    """
    pilots = _pilot_observations(program, capture, sample_rate, locations)
    linearity_ok = all(p.linearity_ok for p in pilots) if pilots else None
    channel_map_ok = all(p.channel_map_ok for p in pilots) if pilots else None
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    return tuple(pilots), linearity_ok, channel_map_ok, pilot_snr_ok


def _channel_map_ok(
    samples: np.ndarray,
    sample_rate: int,
    seg: ProgramSegment,
    *,
    ambient_samples: np.ndarray | None = None,
    other_bands: Sequence[tuple[float, float]] = (),
) -> tuple[bool, float | None, float | None]:
    """Band-relative channel-map sanity (design note above `CHANNEL_MAP_*`).

    Given a leading ambient (room-noise) window — CHECK's own 12 s ambient
    segment — this asks two independent questions per pilot instead of the
    old single "is most of the TOTAL energy in-band" fraction test (which a
    concurrent, unrelated room-noise band could veto even when the driver
    under test was behaving correctly — the run-5 hardware bug):

    1. TARGET: did THIS driver's own declared band rise
       ``CHANNEL_MAP_TARGET_RISE_DB`` above that band's ambient level? (the
       driver actually played, above the room's floor in its own band.)
    2. CROSS: did every OTHER driver's band stay BELOW
       ``CHANNEL_MAP_CROSS_RISE_DB`` above ITS ambient level during this same
       pilot window? (energy did not land in the wrong driver's band — the
       actual map-swap discriminator.)

    Without an ambient window (v2 MEASURE/VERIFY's leading pilot pair has none
    — see `_pilot_verdicts`), falls back to the original test: energy inside
    the declared band must exceed half of the pilot window's TOTAL spectral
    energy.

    Returns ``(ok, target_rise_db, cross_rise_db)`` — the two rise numbers are
    ADDITIVE diagnostic evidence for operator logging (surfaced on
    ``PilotObservation``); the pass/fail decision below is byte-identical to
    before this return shape grew. ``cross_rise_db`` is the rise that failed
    the CROSS test when ``ok`` is False, or the worst (highest) rise observed
    across every other band when ``ok`` is True. Both rises are ``None`` in
    the no-ambient-window fallback path (no rise concept there);
    ``cross_rise_db`` is also ``None`` when ``other_bands`` is empty or the
    TARGET test alone already failed.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8 or seg.f1_hz is None or seg.f2_hz is None:
        return False, None, None

    if ambient_samples is None or np.asarray(ambient_samples).size < 8:
        window = np.hanning(x.size)
        spectrum = np.abs(np.fft.rfft(x * window)) ** 2
        freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
        in_band = (freqs >= seg.f1_hz) & (freqs <= seg.f2_hz)
        total = float(np.sum(spectrum))
        if total <= 0:
            return False, None, None
        return float(np.sum(spectrum[in_band])) / total > 0.5, None, None

    target_rise = (
        _band_rms_dbfs(x, sample_rate, seg.f1_hz, seg.f2_hz)
        - _band_rms_dbfs(ambient_samples, sample_rate, seg.f1_hz, seg.f2_hz)
    )
    if target_rise < CHANNEL_MAP_TARGET_RISE_DB:
        return False, target_rise, None
    worst_cross_rise: float | None = None
    for other_f1, other_f2 in other_bands:
        cross_rise = (
            _band_rms_dbfs(x, sample_rate, other_f1, other_f2)
            - _band_rms_dbfs(ambient_samples, sample_rate, other_f1, other_f2)
        )
        if worst_cross_rise is None or cross_rise > worst_cross_rise:
            worst_cross_rise = cross_rise
        if cross_rise >= CHANNEL_MAP_CROSS_RISE_DB:
            return False, target_rise, cross_rise
    return True, target_rise, worst_cross_rise


def _solve_gain_plan(
    program: ExcitationProgram,
    pilots: Sequence[PilotObservation],
    ambient_report: Mapping[str, Any],
    priors: MeasurementPriors,
) -> GainPlan:
    target = priors.target_capture_dbfs
    gains: dict[str, float] = {}
    predicted_peaks: list[float] = []
    for pilot in pilots:
        lo_seg = program.segment(f"pilot_{pilot.role}_lo")
        hi_seg = program.segment(f"pilot_{pilot.role}_hi")
        # captured = digital_gain + K (unit slope). K from the two pilots.
        # Deliberately the PEAK-referenced levels (`peak_*_dbfs`), not
        # `level_*_dbfs` — the latter is ambient-subtracted for the
        # linearity verdict and would shift this ABSOLUTE reference (see
        # `PilotObservation`'s docstring); `target_capture_dbfs` is
        # documented as a capture-PEAK target and K must match that.
        k_lo = pilot.peak_lo_dbfs - lo_seg.gain_db
        k_hi = pilot.peak_hi_dbfs - hi_seg.gain_db
        k = (k_lo + k_hi) / 2.0
        gain = target - k
        gain = min(gain, GAIN_MAX_DIGITAL_PEAK_DBFS)  # ≥6 dB guard
        gains[pilot.role] = gain
        predicted_peaks.append(gain)
    predicted_peak = max(predicted_peaks) if predicted_peaks else GAIN_MAX_DIGITAL_PEAK_DBFS

    snr_floor_ok = _snr_floor_ok(ambient_report, target)
    return GainPlan(
        gain_db=gains,
        predicted_peak_dbfs=predicted_peak,
        snr_floor_ok=snr_floor_ok,
    )


def _snr_floor_ok(ambient_report: Mapping[str, Any], target_capture_dbfs: float) -> bool:
    bands = ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    if not bands:
        return False
    worst = max(float(b["level_dbfs"]) for b in bands if "level_dbfs" in b)
    return (target_capture_dbfs - worst) >= DRIVER.snr_ok_db


# --------------------------------------------------------------------------- #
# phase dispatch
# --------------------------------------------------------------------------- #


def analyze_program_capture(
    program: ExcitationProgram,
    samples: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None" = None,
    geometry: MeasurementGeometry | None = None,
    priors: MeasurementPriors | None = None,
) -> ProgramAnalysis:
    """Analyze a program capture into a :class:`ProgramAnalysis` (design §5.6)."""
    if sample_rate != program.sample_rate_hz:
        raise ValueError(
            f"capture rate {sample_rate} != program rate {program.sample_rate_hz}"
        )
    capture = np.asarray(samples, dtype=np.float64).ravel()
    # Bound the capture BEFORE any full-rate FFT (kernel contract: defense at
    # the FFT, 1 GB Pi). A legitimate conductor capture is the program plus a
    # small phone-start lead; a stuck recording is truncated to the program
    # duration plus CAPTURE_BOUND_MARGIN_S. A program that genuinely starts
    # beyond the margin fails downstream location checks loudly instead of
    # allocating hundreds of MB here.
    capture = deconv.cap_capture_length(
        capture,
        sweep_len=program.total_samples,
        sample_rate=sample_rate,
        max_capture_seconds=(
            program.total_samples / sample_rate + CAPTURE_BOUND_MARGIN_S
        ),
    )
    geometry = geometry or MeasurementGeometry()
    priors = priors or MeasurementPriors()

    global_offset, _first, stimuli = _global_offset(program, capture, sample_rate)
    locations = _locate_segments(program, capture, sample_rate, global_offset, stimuli)

    if program.phase == PHASE_CHECK:
        return _analyze_check(program, capture, sample_rate, global_offset, locations, priors)
    if program.phase == PHASE_MEASURE:
        return _analyze_measure(
            program, capture, sample_rate, global_offset, locations,
            calibration, geometry, priors,
        )
    if program.phase == PHASE_VERIFY:
        return _analyze_verify(
            program, capture, sample_rate, global_offset, locations,
            calibration, priors,
        )
    raise ValueError(f"unknown phase: {program.phase!r}")


def _analyze_check(
    program, capture, sample_rate, global_offset, locations, priors,
) -> ProgramAnalysis:
    ambient_seg = program.segment("ambient")
    ambient_samples, ambient_report = _ambient_from_capture(
        capture, sample_rate, ambient_seg, global_offset
    )
    pilots = _pilot_observations(
        program, capture, sample_rate, locations, ambient_samples=ambient_samples,
    )
    linearity_ok = all(p.linearity_ok for p in pilots) if pilots else None
    channel_map_ok = all(p.channel_map_ok for p in pilots) if pilots else None
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    gain_plan = _solve_gain_plan(program, pilots, ambient_report, priors)
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        ambient_report=ambient_report,
        pilots=tuple(pilots),
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        gain_plan=gain_plan,
    )


def _analyze_measure(
    program, capture, sample_rate, global_offset, locations,
    calibration, geometry, priors,
) -> ProgramAnalysis:
    if priors.crossover_fc_hz is None:
        raise ValueError("MEASURE analysis requires priors.crossover_fc_hz")
    fc_hz = float(priors.crossover_fc_hz)
    drift = _estimate_drift(program, capture, sample_rate, global_offset, locations)

    seg_w = program.segment("sweep_w")
    seg_t = program.segment("sweep_t")
    epsilon = drift.epsilon_ppm / 1e6
    # Deconvolve both sweeps anchored at their SCHEDULE window (with a shared
    # pre-guard) so relative timing survives (the aligner relies on this); the
    # measured ε is divided out of the reference so drift can't smear the IR.
    woofer_full_ir, pre_w = _deconvolve_window(
        capture, seg_w, global_offset + seg_w.start_sample, sample_rate,
        epsilon=epsilon,
    )
    tweeter_full_ir, pre_t = _deconvolve_window(
        capture, seg_t, global_offset + seg_t.start_sample, sample_rate,
        epsilon=epsilon,
    )
    pre_samples = min(pre_w, pre_t)
    n_fft = _n_fft_for(woofer_full_ir, tweeter_full_ir)

    responses = (
        _driver_response(
            seg_w.role, woofer_full_ir, sample_rate,
            calibration=calibration, ambient_report=priors.ambient_report,
            fc_hz=fc_hz, n_fft=n_fft,
        ),
        _driver_response(
            seg_t.role, tweeter_full_ir, sample_rate,
            calibration=calibration, ambient_report=priors.ambient_report,
            fc_hz=fc_hz, n_fft=n_fft,
        ),
    )

    alignment = _estimate_alignment(
        capture, program, sample_rate, global_offset, drift.epsilon_ppm / 1e6,
        fc_hz, geometry, priors,
        woofer_full_ir=woofer_full_ir, tweeter_full_ir=tweeter_full_ir,
        pre_samples=pre_samples,
    )

    candidate, predicted_sum = _build_candidate(
        woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
        seg_w.role, seg_t.role, alignment, calibration,
        tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
    )
    # Per-capture behavioral-linearity evidence (design §5.2): a v2 MEASURE
    # program opens with a leading pilot pair; legacy programs carry none, so
    # the verdicts stay ``None`` (byte-identical to the pre-v2 analysis).
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        drift=drift,
        driver_responses=responses,
        alignment=alignment,
        candidate=candidate,
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        predicted_sum=predicted_sum,
        glitch_detected=drift.glitch_detected,
    )


def _build_candidate(
    woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
    woofer_role, tweeter_role, alignment, calibration,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
) -> tuple[CrossoverCandidate, tuple[np.ndarray, np.ndarray]]:
    freqs, W, gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=calibration)
    _f2, T, gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=calibration)
    lo, hi = _overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=tweeter_sweep_lo_hz, woofer_sweep_hi_hz=woofer_sweep_hi_hz,
    )
    # Gating-consistent prediction (W6.9 forensics): ``_aligned_branch_tf`` now
    # reflection-gates each branch the same way ``_driver_response`` does, so a
    # branch near a reflective mic position can be valid only above a floor
    # HIGHER than the nominal Fc±1-oct band. Clamp every quantity derived from
    # W/T — the trim solve, the predicted sum's ripple — to the worse (higher)
    # of the two branches' floors, never silently trusting sub-floor bins.
    # If the floor consumes the whole band, `_solve_trims`/`_ripple_db` raise
    # ValueError on the now-empty mask — the existing catch-all seam in
    # `jasper.web.correction_crossover_v2` already classifies that as
    # `internal_error` (see its comment: "analyze/emit raise ValueError"), so
    # this degrades through an existing signal rather than a new reason code.
    branch_floor_hz = max(
        (f for f in (_gate_floor_hz(gate_w), _gate_floor_hz(gate_t)) if f is not None),
        default=None,
    )
    lo_clamped = (
        max(lo, branch_floor_hz)
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
        else lo
    )
    trim_w, trim_t, _lw, _lt = _solve_trims(freqs, W, T, fc_hz, lo_hz=lo_clamped, hi_hz=hi)
    # Predicted APPLIED sum ``W_xo·g_w + s·T_xo·g_t·e^{−jωτ}`` (design §5.6.6).
    # ``_aligned_branch_tf`` references each branch to its own direct peak, i.e.
    # the proposed delay is already applied (τ_residual → 0, e^{−jωτ} → 1), so
    # this is the flattest-achievable aligned sum for the candidate.
    predicted = _predicted_sum(W, T, trim_w, trim_t, alignment.polarity_sign)
    ripple = _ripple_db(freqs, predicted, lo_clamped, hi)
    predicted_db = 20.0 * np.log10(np.maximum(np.abs(predicted), 1e-12))
    candidate = CrossoverCandidate(
        trim_db={woofer_role: trim_w, tweeter_role: trim_t},
        polarity=alignment.polarity,
        delay_us=alignment.delay_us,
        predicted_ripple_db=ripple,
        confidence=alignment.confidence,
    )
    return candidate, (freqs, predicted_db)


def _analyze_verify(
    program, capture, sample_rate, global_offset, locations,
    calibration, priors,
) -> ProgramAnalysis:
    fc_hz = float(priors.crossover_fc_hz) if priors.crossover_fc_hz else None
    seg = program.segment("sweep_verify")
    full_ir, _pre = _deconvolve_window(
        capture, seg, global_offset + seg.start_sample, sample_rate
    )
    n_fft = _n_fft_for(full_ir)
    summed = _driver_response(
        "summed", full_ir, sample_rate,
        calibration=calibration, ambient_report=None, fc_hz=fc_hz, n_fft=n_fft,
    )
    ripple = None
    tracking = None
    if fc_hz is not None:
        lo, hi = _overlap_band_hz(
            fc_hz,
            tweeter_sweep_lo_hz=priors.measure_tweeter_sweep_lo_hz,
            woofer_sweep_hi_hz=priors.measure_woofer_sweep_hi_hz,
        )
        ripple = _ripple_db(summed.freqs_hz, summed.complex_tf, lo, hi)
        if priors.predicted_sum is not None:
            pred_freqs, pred_db = priors.predicted_sum
            measured_db = analysis_mod.smooth_fractional_octave(
                summed.freqs_hz, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION
            )
            predicted_db_interp = np.interp(summed.freqs_hz, pred_freqs, pred_db)
            # Validity-floor clamp (W6.9 forensics, 2026-07-19): this capture's
            # OWN reflection gate (`summed.validity_floor_hz`, from the same
            # `_driver_response` call above) can be tighter than the nominal
            # Fc±1-oct band at a reflective mic position — bins below that
            # floor are not a measurement, they're an artifact of a truncated
            # gate window (gating.f_valid_floor_hz), so they must not decide
            # PASS/FAIL either way. This generalizes the W6.7 notch exclusion
            # from "deep predicted notch" to "below measurement validity": the
            # W6 run-7/8 hardware failures were a fixed 65 ms prediction
            # window baking a desk-bounce reflection into the predicted sum's
            # sub-floor region, invisible to the notch-exclusion rule because
            # the false notch wasn't always deep enough to trip it. Applies to
            # BOTH rms and max, and to the notch-exclusion bin set — the two
            # exclusions compose (clamp first, then still exclude a genuine
            # deep predicted notch above the floor).
            floor_hz = summed.validity_floor_hz
            lo_clamped = (
                max(lo, floor_hz) if floor_hz is not None and math.isfinite(floor_hz) else lo
            )
            tracking_band = (lo_clamped, hi)
            rms, max_abs = analysis_mod.tracking_error_db(
                summed.freqs_hz, measured_db, predicted_db_interp, tracking_band,
            )
            # Notch-excluded: the actual gating comparator
            # (`crossover_v2_flow._consume_verify` reads ``max_db_notch_excluded``).
            rms_excl, max_excl = analysis_mod.notch_excluded_tracking_error_db(
                summed.freqs_hz, measured_db, predicted_db_interp, tracking_band,
                notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            )
            # Raw full-band (pre-floor-clamp) numbers, kept as DIAGNOSTIC
            # fields only — never consumed by the gate. What ``rms_db``/
            # ``max_db`` used to mean before the floor clamp landed.
            raw_rms, raw_max = analysis_mod.tracking_error_db(
                summed.freqs_hz, measured_db, predicted_db_interp, (lo, hi),
            )
            tracking = {
                "rms_db": rms,
                "max_db": max_abs,
                "rms_db_notch_excluded": rms_excl,
                "max_db_notch_excluded": max_excl,
                "tracking_band_hz": [tracking_band[0], tracking_band[1]],
                "rms_db_full_band": raw_rms,
                "max_db_full_band": raw_max,
            }
    # A v2 VERIFY program opens with a leading pilot pair (design §5.2) so the
    # post-apply capture carries its own behavioral-linearity evidence too;
    # legacy VERIFY programs carry none and the verdicts stay ``None``.
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        summed_response=summed,
        summed_ripple_db=ripple,
        verify_tracking=tracking,
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
    )


# --------------------------------------------------------------------------- #
# diagnostic summary (operator capture retention — jasper.web.correction_crossover_v2)
# --------------------------------------------------------------------------- #


def _gate_window_ms_of(response: "DriverResponse | None") -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def analysis_diagnostic_summary(analysis: Any) -> dict[str, Any]:
    """Flat, JSON-safe numeric diagnostics from one :class:`ProgramAnalysis`.

    The operator capture-retention sidecar (``jasper.web.correction_crossover_v2``
    ``_maybe_retain_capture``) attaches this to every retained WAV so the clip
    is self-describing without replaying the analysis. Reads only fields
    ``ProgramAnalysis``/its nested dataclasses already carry — nothing here is
    recomputed. Per-driver/per-pilot fields key off each entry's OWN ``role``
    string (whatever the program declared — "woofer"/"tweeter" in production)
    rather than a hardcoded label, since this runs at the analyze seam, before
    the v2 conductor's role mapping exists.

    Deliberately duck-typed (``analysis: Any``) and defensive throughout: this
    is called from a best-effort retention path that must never raise even if
    a test double stands in for a real ``ProgramAnalysis`` (see
    ``bind_production_analyze``'s own tests, which monkeypatch
    ``analyze_program_capture`` to return a bare string) — every field access
    goes through ``getattr(..., None)`` so a malformed/foreign ``analysis``
    degrades to an emptier summary rather than raising past the caller's
    guard.
    """
    out: dict[str, Any] = {"phase": getattr(analysis, "phase", None)}

    drift = getattr(analysis, "drift", None)
    if drift is not None:
        out["epsilon_ppm"] = round(float(drift.epsilon_ppm), 3)
        out["max_residual_samples"] = round(float(drift.max_residual_samples), 3)
        out["repeat_level_delta_db"] = round(
            float(getattr(drift, "repeat_level_delta_db", 0.0)), 3
        )
        out["glitch_detected"] = bool(drift.glitch_detected)

    alignment = getattr(analysis, "alignment", None)
    if alignment is not None:
        out["alignment_confidence"] = round(float(alignment.confidence), 4)
        out["alignment_status"] = alignment.status
        out["delay_us"] = round(float(alignment.delay_us), 3)
        out["polarity"] = alignment.polarity

    candidate = getattr(analysis, "candidate", None)
    if candidate is not None:
        out["predicted_ripple_db"] = round(float(candidate.predicted_ripple_db), 4)

    for resp in getattr(analysis, "driver_responses", None) or ():
        role = resp.role
        out[f"{role}_gate_window_ms"] = _gate_window_ms_of(resp)
        out[f"{role}_validity_floor_hz"] = resp.validity_floor_hz
        if resp.snr is not None:
            worst = resp.snr.get("worst_relevant") or {}
            out[f"{role}_snr_db"] = worst.get("estimated_snr_db")
            out[f"{role}_snr_verdict"] = worst.get("verdict")

    for pilot in getattr(analysis, "pilots", None) or ():
        role = pilot.role
        snr_db = getattr(pilot, "snr_db", math.inf)
        out[f"{role}_pilot_snr_db"] = round(snr_db, 2) if math.isfinite(snr_db) else None
        out[f"{role}_captured_delta_db"] = round(float(pilot.captured_delta_db), 3)
        out[f"{role}_programmed_delta_db"] = round(float(pilot.programmed_delta_db), 3)
        out[f"{role}_channel_map_target_rise_db"] = pilot.channel_map_target_rise_db
        out[f"{role}_channel_map_cross_rise_db"] = pilot.channel_map_cross_rise_db

    gain_plan = getattr(analysis, "gain_plan", None)
    if gain_plan is not None:
        out["gain_plan_snr_floor_ok"] = gain_plan.snr_floor_ok
        out["gain_plan_predicted_peak_dbfs"] = round(
            float(gain_plan.predicted_peak_dbfs), 3
        )

    for flag in ("pilot_snr_ok", "linearity_ok", "channel_map_ok"):
        value = getattr(analysis, flag, None)
        if value is not None:
            out[flag] = value

    summed_response = getattr(analysis, "summed_response", None)
    if summed_response is not None:
        out["verify_gate_window_ms"] = _gate_window_ms_of(summed_response)
        out["verify_validity_floor_hz"] = summed_response.validity_floor_hz

    tracking = getattr(analysis, "verify_tracking", None)
    if tracking:
        for key in ("rms_db", "max_db", "rms_db_notch_excluded", "max_db_notch_excluded"):
            if key in tracking:
                out[key] = tracking[key]
        band = tracking.get("tracking_band_hz")
        if isinstance(band, (list, tuple)) and len(band) == 2:
            out["tracking_band_lo_hz"] = band[0]
            out["tracking_band_hi_hz"] = band[1]

    return out
