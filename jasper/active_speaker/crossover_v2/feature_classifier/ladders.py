# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Window ladders, timing, pose persistence and decay readings."""

from __future__ import annotations

import math
from collections.abc import (
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.audio_measurement.excess_phase import (
    FEATURE_HALF_OCT,
    NEIGHBOURHOOD_OCT,
)
from jasper.audio_measurement.gating import analytic_envelope

from ..feature_classification import (
    GATE_MOVED,
    GATE_STABLE,
    UNRESOLVED,
)
from ..feature_optics import (
    CENTRE_SEARCH_OCT,
    detrend,
    read_feature,
)
from ..gate_sweep import (
    GATE_DELTA_SLACK_DB,
    SIGMA_GROWTH_MIN_SIGMA_DB,
    WINDOW_MOVED,
    WINDOW_STABLE,
    analysis_grid,
    frame_descriptor,
    sweep_features,
)
from ..round_captures import (
    REFUSE_RADIATED_BAND_MISSING,
    PoseCapture,
    RoundCapturesRefused,
)
from .captures import (
    RoundCapture,
    RoundPoseCurve,
)

#: A ladder of one rung. The window verdict compares the shortest and longest
#: resolution-valid rung, so one rung compares with nothing. Deliberately
#: not a :data:`CLASSIFICATION_REFUSAL_REASONS` member: it costs the window
#: verdict and nothing else.
GATE_LADDER_NEEDS_TWO_RUNGS = "gate_ladder_needs_two_rungs"

#: The engine declined this ladder or a bin on it for a reason of its own
#: (:exc:`ValueError`), carried through rather than raised: the same trade as
#: above, and for the same reason.
GATE_LADDER_UNUSABLE = "gate_ladder_unusable"


#: Half-width of the direct-sound window the timing residual is measured over.
#: Wide enough to hold the arrival's own main lobe, narrow enough to exclude
#: the first reflection at any usable gate.
DIRECT_SOUND_HALF_WINDOW_MS = 1.0


#: The drop time-to-decay is measured against. Named once so the field
#: (``time_to_neg20_db_ms``) and the reachability test (``below_floor``) read
#: the same number back rather than one of them drifting from the other.
DECAY_TARGET_DROP_DB = 20.0

#: How far beyond a feature's own +/-``FEATURE_HALF_OCT`` skirts a flanking
#: decay band's matching skirt sits. One third-octave: the flanking reads
#: show whether extended ringing is peculiar to the resonance or is the
#: room's tail nearby — the campaign's ~48 ms on the 898 Hz ridge against
#: ~6-10 ms a third-octave outside it. Each flank is the SAME width as the
#: centre band.
DECAY_FLANK_SKIRT_OFFSET_OCT = 1.0 / 3.0

#: Fraction of a band-limited envelope's own tail read for its noise floor.
#: The IRs this reads are the full deconvolved capture, not a gated
#: fragment, so any decay this instrument could report has long since
#: finished by the last fifth of it.
DECAY_NOISE_FLOOR_TAIL_FRACTION = 0.2


def _sweep_ladder(
    captures: Sequence[RoundCapture],
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    features: Sequence[float],
    rungs_ms: Sequence[float],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    """Every feature through :func:`~.gate_sweep.sweep_features`, plus its frame.

    Returns ``(by_feature, frame, poses, refusal)``. ``refusal`` is ``None``
    when the ladder ran; otherwise it names why it could not and
    ``by_feature`` is empty — the LADDER is refused for the round, never the
    classification. A round with one capture, with sidecars banking no
    radiated band, or with a ladder of one rung still gets its phase class,
    its decay reads and its per-pose facts, and every row says so by name
    rather than reading :data:`GATE_STABLE` off a test that never ran.

    ``poses`` is who each pose row of every feature IS, banked once for the
    round beside the frame, in the order those rows are in.
    """
    rungs = tuple(sorted(float(rung) for rung in rungs_ms))
    frame = frame_descriptor(rungs, analysis_grid())
    # Everything but `radiated_band_hz`, `sample_rate`, `ir` and `peak_idx` is
    # disclosure: it names the capture a pose row was read from, and none of
    # it moves a number.
    poses: list[PoseCapture] = []
    unbanded: list[str] = []
    for capture, ir, peak in zip(captures, irs, peaks):
        band = capture.radiated_band_hz
        if band is None:
            unbanded.append(capture.wav.name)
            continue
        poses.append(
            PoseCapture(
                capture_id=capture.wav.stem,
                phase=capture.phase,
                wav=capture.wav,
                program=capture.program,
                program_sha256="",
                azimuth_deg=(
                    None if capture.degrees is None else float(capture.degrees)
                ),
                vertical_deg=None,
                mark_distance_m=None,
                radiated_band_hz=band,
                sample_rate=sample_rate,
                ir=ir,
                peak_idx=peak,
            )
        )
    if unbanded:
        return (
            {},
            frame,
            [],
            {
                "reason": REFUSE_RADIATED_BAND_MISSING,
                "captures": unbanded,
                "note": (
                    "the ladder normalises each capture on a reference band "
                    "intersected with the band its own DUT radiates, and no "
                    "declared band substitutes for one the capture did not "
                    "bank (E5, #1969)"
                ),
            },
        )
    banked_poses = [
        {
            "pose_key": pose.pose_key,
            "capture_id": pose.capture_id,
            "phase": pose.phase,
            "azimuth_deg": pose.azimuth_deg,
            "vertical_deg": pose.vertical_deg,
            "mark_distance_m": pose.mark_distance_m,
            "capture_wav": pose.wav.name if pose.wav is not None else None,
        }
        for pose in poses
    ]
    if len(rungs) < 2:
        # The engine raises on this, and a ladder is not worth the round: a
        # caller who asked for one rung still gets every other fact.
        return (
            {},
            frame,
            banked_poses,
            {
                "reason": GATE_LADDER_NEEDS_TWO_RUNGS,
                "rungs_ms": list(rungs),
                "note": (
                    "the window verdict compares the shortest and longest "
                    "resolution-valid rung, so one rung compares with nothing"
                ),
            },
        )
    try:
        swept = sweep_features(poses, rungs_ms=rungs, at_hz=list(features))
    except RoundCapturesRefused as refusal:
        return {}, frame, banked_poses, {"reason": refusal.reason, **refusal.detail}
    except ValueError as exc:
        # Anything else the engine's own input check refuses -- today only a bin off
        # its 200-20000 Hz grid, which this caller can reach with a long enough
        # `gate_ms`. Folded, not swallowed: the message rides the refusal into
        # the artifact. Remove when the engine refuses by name instead.
        return (
            {},
            frame,
            banked_poses,
            {"reason": GATE_LADDER_UNUSABLE, "detail": str(exc)},
        )
    return (
        {f"{fc:.0f}": result for fc, result in zip(features, swept)},
        frame,
        banked_poses,
        None,
    )


def _timing_scatter(
    captures: Sequence[RoundCapture],
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    trusted_band_hz: tuple[float, float],
) -> dict[str, Any]:
    """Arrival-time scatter between captures at the SAME angle.

    The raw arrival spread is dominated by the capture's capture-start offset,
    which every other test removes by re-finding the peak. The SUB-SAMPLE
    residual is what survives into a phase comparison, measured by
    cross-spectrum phase slope over the direct-sound window. It needs an
    angle visited twice; with no pair the result says NOT RUN and carries no
    numbers, a dimension that did not run being a different fact from one
    that measured zero.
    """
    arrivals = [peak / sample_rate * 1e3 for peak in peaks]
    spread = {
        "min_ms": float(min(arrivals)),
        "max_ms": float(max(arrivals)),
        "spread_ms": float(max(arrivals) - min(arrivals)),
    }

    by_angle: dict[int, list[int]] = {}
    for index, capture in enumerate(captures):
        if capture.degrees is not None:
            by_angle.setdefault(capture.degrees, []).append(index)

    half = int(round(DIRECT_SOUND_HALF_WINDOW_MS * 1e-3 * sample_rate))

    def _direct(index: int) -> np.ndarray:
        start = max(0, peaks[index] - half)
        return irs[index][start : peaks[index] + half]

    residuals: list[float] = []
    for indexes in (by_angle[angle] for angle in sorted(by_angle)):
        for i in range(len(indexes)):
            for j in range(i + 1, len(indexes)):
                a, b = _direct(indexes[i]), _direct(indexes[j])
                if a.size != b.size:
                    # One capture's peak sits inside the first millisecond, so
                    # the two direct-sound windows are not the same shape and
                    # differencing them would measure the truncation.
                    continue
                residuals.append(
                    _subsample_delay_us(a, b, sample_rate, trusted_band_hz)
                )

    if not residuals:
        return {
            "available": False,
            "n_pairs": 0,
            "raw_arrival_ms": spread,
            "note": (
                "NOT RUN: no angle was captured twice, so no pair of captures "
                "shares a geometry to difference. This is an unmeasured "
                "dimension, not a measured zero."
            ),
        }
    values = np.array(residuals)
    return {
        "available": True,
        "n_pairs": int(values.size),
        "raw_arrival_ms": spread,
        "subsample_residual_us": {
            "sd": float(values.std(ddof=1)) if values.size > 1 else 0.0,
            "max_abs": float(np.abs(values).max()),
        },
        "phase_error_deg_at_2k": float(
            360 * 2000 * np.abs(values).max() * 1e-6
        ),
    }


def _subsample_delay_us(
    a: np.ndarray, b: np.ndarray, sample_rate: int, trusted_band_hz: tuple[float, float]
) -> float:
    n = 1 << 14
    spectrum_a = np.fft.rfft(a, n)
    spectrum_b = np.fft.rfft(b, n)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    band = (freqs >= trusted_band_hz[0]) & (freqs <= min(8000.0, trusted_band_hz[1]))
    cross = spectrum_a[band] * np.conj(spectrum_b[band])
    slope = np.polyfit(
        2 * np.pi * freqs[band], np.unwrap(np.angle(cross)), 1, w=np.abs(cross)
    )[0]
    return float(-slope * 1e6)


#: Fewest samples needed to read an extremum on the lateral walk's grid
#: (``spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE`` = 12/octave).
_POSE_MIN_CENTRE_SAMPLES = 3


def _centre_search_mask(freqs: np.ndarray, fc: float) -> np.ndarray:
    return (freqs >= fc * 2**-CENTRE_SEARCH_OCT) & (freqs <= fc * 2**CENTRE_SEARCH_OCT)


def _extremum_reading(
    values: np.ndarray, freqs: np.ndarray, fc: float, is_dip: bool
) -> tuple[float, float]:
    """The extremum inside ``fc``'s centre-search window: (value, centre_hz).

    The one implementation of "where is this feature's centre" — the gate
    ladder and the pose-persistence read call it alike, so the same feature
    can never grow two centres.
    """
    search = _centre_search_mask(freqs, fc)
    window = values[search]
    apex = int(np.argmin(window) if is_dip else np.argmax(window))
    return float(window[apex]), float(freqs[search][apex])


def _pose_reading(
    curve: RoundPoseCurve, detrended: np.ndarray, fc: float, is_dip: bool
) -> dict[str, Any]:
    """One pose curve's own depth/centre near ``fc``, or NOT-RESOLVED.

    Direct on the curve's own banked ``(freqs_hz, magnitude_db)``, never a
    raw WAV, and detrended with this module's own :func:`detrend` so a
    departure read here means what it means in the primary detector.

    NOT-RESOLVED (``resolved=False``, both numbers ``None``) whenever this
    pose cannot answer at all: ``fc``'s neighbourhood falls outside the
    curve's driven ``band_hz``, or too few grid points land inside the
    centre-search span. Never a fabricated 0 dB — absence is not zero.
    ``detrended`` is computed once per curve by the caller.
    """
    lo = fc * 2**-NEIGHBOURHOOD_OCT
    hi = fc * 2**NEIGHBOURHOOD_OCT
    base: dict[str, Any] = {
        "pose_id": curve.pose_id,
        "position_deg": curve.position_deg,
        "vertical_deg": curve.vertical_deg,
        "role": curve.role,
        "resolved": False,
        "pooled_db": None,
        "centre_hz": None,
    }
    if not (curve.band_hz[0] <= lo and hi <= curve.band_hz[1]):
        return base
    search = _centre_search_mask(curve.freqs_hz, fc)
    if int(np.count_nonzero(search)) < _POSE_MIN_CENTRE_SAMPLES:
        return base
    pooled, centre = _extremum_reading(detrended, curve.freqs_hz, fc, is_dip)
    return {**base, "resolved": True, "pooled_db": pooled, "centre_hz": centre}


def _pose_persistence_block(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """One feature's pose table, under the spread across the poses that
    RESOLVED it, so the position question is a number rather than N rows.

    ``sigma_pooled_db`` is the sample standard deviation (ddof=1) of those
    rows' ``pooled_db``, and ``None`` under two of them, where it is
    undefined: a fabricated 0.0 would read as a feature that held perfectly
    across a walk nobody took. A statistic, never a verdict -- what a spread
    means for a filter is the reader's, on the methodology's thresholds.
    """
    resolved = [row["pooled_db"] for row in readings if row["resolved"]]
    return {
        "n_poses": len(readings),
        "n_resolved": len(resolved),
        "sigma_pooled_db": (
            float(np.std(resolved, ddof=1)) if len(resolved) > 1 else None
        ),
        "poses": readings,
    }


def _pose_bank_block(pose_curves: Sequence[RoundPoseCurve]) -> dict[str, Any]:
    """This round's lateral-pose bank, once -- what every row's
    ``pose_persistence`` table is read against.

    Mirrors :func:`_timing_scatter`'s NOT-RUN shape: "no lateral poses
    banked" is a different fact from "every pose read as not-resolved".
    """
    if not pose_curves:
        return {
            "available": False,
            "n_poses": 0,
            "note": (
                "NOT RUN: this round banked no lateral-walk pose curves, so "
                "no feature's off-axis persistence can be read. This is an "
                "unmeasured dimension, not evidence a feature is on-axis only."
            ),
        }
    return {
        "available": True,
        "n_poses": len({curve.pose_id for curve in pose_curves}),
    }


def _decay_bands_hz(fc: float) -> dict[str, tuple[float, float]]:
    """The centre band and its two flanks, every one ``FEATURE_HALF_OCT`` wide.

    The centre band is :func:`read_feature`'s, restated so a magnitude read
    and a decay read agree on what "the feature's own band" means. Each
    flank is the SAME width, its own inner edge
    :data:`DECAY_FLANK_SKIRT_OFFSET_OCT` beyond the centre band's matching
    skirt, never narrower.
    """
    half = FEATURE_HALF_OCT
    far = 3 * half + DECAY_FLANK_SKIRT_OFFSET_OCT
    near = half + DECAY_FLANK_SKIRT_OFFSET_OCT
    return {
        "center": (fc * 2**-half, fc * 2**half),
        "flank_lo": (fc * 2**-far, fc * 2**-near),
        "flank_hi": (fc * 2**near, fc * 2**far),
    }


@dataclass(frozen=True)
class _DecayHost:
    """One IR's forward FFT, computed once — every band read shares it.

    The per-band work is only the mask and the inverse transform; the
    spectrum itself is feature-independent, and a round reads three bands
    per feature off the same host IR.
    """

    n: int
    sample_rate: int
    spectrum: np.ndarray
    freqs: np.ndarray

    @classmethod
    def of(cls, ir: np.ndarray, sample_rate: int) -> "_DecayHost":
        return cls(
            n=ir.size,
            sample_rate=sample_rate,
            spectrum=np.fft.rfft(ir),
            freqs=np.fft.rfftfreq(ir.size, d=1.0 / sample_rate),
        )


def _band_limited_envelope(
    host: _DecayHost, band_hz: tuple[float, float]
) -> np.ndarray:
    """The analytic envelope of the host IR restricted to ``band_hz``.

    An FFT-domain brick-wall mask, zero outside the band: the IR here is the
    module's own reflection-free deconvolution, already long and clean, so no
    taper is needed. :func:`~jasper.audio_measurement.gating.analytic_envelope`
    is REUSED, not duplicated.
    """
    mask = (host.freqs >= band_hz[0]) & (host.freqs <= band_hz[1])
    band_limited = np.fft.irfft(host.spectrum * mask, n=host.n)
    return analytic_envelope(band_limited)


def _decay_read(host: _DecayHost, band_hz: tuple[float, float]) -> dict[str, Any]:
    """Time-to-``DECAY_TARGET_DROP_DB`` in one band, or an honest non-answer.

    ``noise_floor_db`` is the envelope's own late-tail level, dB relative to
    THIS band's own peak, so it is directly comparable to
    :data:`DECAY_TARGET_DROP_DB`. ``below_floor`` is a property of that
    number alone. ``time_to_neg20_db_ms`` is ``None`` whenever
    ``below_floor`` is true OR the envelope never reaches the target within
    the IR's own length — never a fabricated time.
    """
    envelope = _band_limited_envelope(host, band_hz)
    peak_idx = int(np.argmax(envelope))
    peak_level = float(envelope[peak_idx])
    tail_start = int(envelope.size * (1.0 - DECAY_NOISE_FLOOR_TAIL_FRACTION))
    tail = envelope[tail_start:]
    floor_level = float(np.median(tail)) if tail.size else 0.0
    noise_floor_db = 20.0 * math.log10(
        max(floor_level, 1e-300) / max(peak_level, 1e-300)
    )
    below_floor = noise_floor_db > -DECAY_TARGET_DROP_DB
    target_level = peak_level * 10 ** (-DECAY_TARGET_DROP_DB / 20.0)
    crossings = np.flatnonzero(envelope[peak_idx:] <= target_level)
    time_ms = (
        float(crossings[0] / host.sample_rate * 1000.0)
        if not below_floor and crossings.size
        else None
    )
    return {
        "band_hz": [float(band_hz[0]), float(band_hz[1])],
        "noise_floor_db": noise_floor_db,
        "below_floor": bool(below_floor),
        "time_to_neg20_db_ms": time_ms,
    }


#: The two cycle counts research 03's Stage 3 names: a narrow one that best
#: rejects reflections and a wide one closer to the shipped primary window's
#: own length. ADR-0201 binds what this buys: re-analysis EVIDENCE only
#: -- no FDW output here may feed a target or a grade, and the diagnostic
#: reading rule (a dip that fills in under FDW but stays deep under the
#: fixed gate is reflection-caused) is guide content, not code.
FDW_CYCLES: tuple[float, ...] = (5.0, 15.0)

#: Half-width of the band an FDW rung is read over. Reused rather than
#: re-picked: :data:`NEIGHBOURHOOD_OCT` is already this module's standing
#: "local context" width and both feature readers' search spans fit
#: inside it, so they serve the FDW curve unmodified.
FDW_BAND_OCT = NEIGHBOURHOOD_OCT

#: Points across that band, log-spaced. Hundreds, not the ~2000/octave main
#: analysis grid: FDW re-picks its window at EVERY point, so each point
#: is its own direct frequency read rather than a shared FFT bin — an
#: offline, twice-per-feature cost, not a global one.
FDW_GRID_POINTS = 300

#: Taper shape for the FDW window. Hann: it is already the family
#: :func:`gate`'s own tail uses, so this instrument carries one taper family
#: rather than introducing a second one for a diagnostic reading.
FDW_TAPER = "hann"


def _fdw_read_hz(
    ir: np.ndarray, sample_rate: int, peak: int, freq_hz: float, cycles: float
) -> float:
    """One frequency's FDW magnitude, dB, from a window of ``cycles`` cycles
    of ``freq_hz`` (``cycles / freq_hz`` seconds) CENTRED on ``peak``.

    A direct single-bin read: the window differs at every frequency, so no
    one FFT serves the whole curve. :data:`FDW_TAPER`, then the
    exact-frequency DFT term, normalised by the taper's own coherent gain so
    a window-LENGTH change alone cannot move the level.
    """
    half = max(1, int(round(cycles / freq_hz * sample_rate / 2.0)))
    start = max(0, peak - half)
    end = min(ir.size, peak + half)
    segment = ir[start:end]
    if segment.size < 2:
        return float("-inf")
    taper = np.hanning(segment.size)
    coherent_gain = float(taper.sum())
    if coherent_gain <= 0:
        return float("-inf")
    n = np.arange(start, end, dtype=np.float64)
    phasor = np.exp(-2j * np.pi * freq_hz * n / sample_rate)
    amplitude = abs(np.sum(segment * taper * phasor)) / coherent_gain
    return 20.0 * math.log10(max(amplitude, 1e-12))


def _fdw_local_curve(
    ir: np.ndarray, sample_rate: int, peak: int, fc: float, cycles: float
) -> tuple[np.ndarray, np.ndarray]:
    """One capture's FDW-``cycles`` curve across ``fc``'s own local band.

    ``(grid, db)``, so the caller reuses :func:`detrend`, :func:`read_feature`
    and :func:`_extremum_reading` unmodified, rather than a second "how big is
    this feature" for one more window shape.
    """
    grid = np.geomspace(fc * 2**-FDW_BAND_OCT, fc * 2**FDW_BAND_OCT, FDW_GRID_POINTS)
    db = np.array(
        [_fdw_read_hz(ir, sample_rate, peak, float(f), cycles) for f in grid]
    )
    return grid, db


def _fdw_rungs(
    irs: Sequence[np.ndarray],
    peaks: Sequence[int],
    sample_rate: int,
    fc: float,
    is_dip: bool,
) -> dict[str, Any]:
    """This feature's :data:`FDW_CYCLES` variants, pooled like a gate rung.

    Diagnostic only (ADR-0201) -- ``pooled_db`` / ``centre_hz`` per cycle
    count, pooled across the round's own captures the identical way the
    primary curve is. Never a target, never a grade: nothing downstream reads
    this key for either.
    """
    out: dict[str, Any] = {}
    for cycles in FDW_CYCLES:
        pooled_values: list[float] = []
        centre_values: list[float] = []
        for ir, peak in zip(irs, peaks):
            grid, db = _fdw_local_curve(ir, sample_rate, peak, fc, cycles)
            det = detrend(db, grid)
            pooled_values.append(read_feature(det, grid, fc))
            _, centre = _extremum_reading(det, grid, fc, is_dip)
            centre_values.append(centre)
        out[f"{cycles:.0f}"] = {
            "pooled_db": float(np.mean(pooled_values)),
            "centre_hz": float(np.mean(centre_values)),
        }
    return out


#: The engine's three words in the register's own. A mapping, never a second
#: rule: :mod:`.gate_sweep` decides what counts as the window having moved a
#: feature, and ``unresolved`` — the test did not answer — becomes
#: :data:`UNRESOLVED`, never :data:`GATE_STABLE`, which is a finding.
_GATE_VERDICT_OF = {WINDOW_STABLE: GATE_STABLE, WINDOW_MOVED: GATE_MOVED}


def _gate_call(
    sweep: Mapping[str, Any] | None, refusal: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One feature's window verdict and its working, off the engine's result.

    The verdict is :data:`~.gate_sweep.WINDOW_MOVED` and friends translated
    through :data:`_GATE_VERDICT_OF`; which routes fired, and the thresholds
    they fired against, are the engine's and are carried through in
    ``gate_sensitivity`` rather than re-derived here.
    """
    notes: list[str] = []
    if refusal is not None:
        notes.append(f"gate ladder refused: {refusal['reason']}")
    sensitivity = None if sweep is None else sweep.get("sensitivity")
    if sweep is not None and sensitivity is None:
        notes.append(
            f"no window sensitivity: {sweep.get('sensitivity_null_reason')}"
        )

    excess_loss: dict[str, float] = {}
    slack_by_rung: dict[str, float] = {}
    tension = False
    if sweep is None:
        gate_verdict = UNRESOLVED
    else:
        gate_verdict = _GATE_VERDICT_OF.get(sweep["window_verdict"], UNRESOLVED)
    if sweep is not None and sensitivity is not None:
        long_key = f"{sensitivity['longest_valid_rung_ms']:g}"
        delta = float(sensitivity["corrected_delta_db"])
        # Keyed by the rung the delta was read AT, against the shortest valid
        # one; both endpoints are named in `gate_sensitivity`.
        excess_loss[long_key] = delta
        slack_by_rung[long_key] = GATE_DELTA_SLACK_DB
        if not sensitivity["sigma_growth_readable"]:
            long_sigma = float(sweep["sigma_db_by_rung"][long_key])
            notes.append(
                f"across-pose sigma is {long_sigma:.3f} dB at {long_key} ms, "
                f"under the {SIGMA_GROWTH_MIN_SIGMA_DB:g} dB floor: these "
                "captures did not disagree, so the growth ratio is their own "
                "noise and is not read"
            )
        tension = (
            gate_verdict == GATE_STABLE and abs(delta) > 0.5 * GATE_DELTA_SLACK_DB
        )

    rungs = _gate_rungs(sweep)
    for rung_key, entry in rungs.items():
        if not entry["resolved"]:
            notes.append(
                f"{rung_key} ms is resolution-invalid at {entry['cycles']:.1f} "
                "cycles in the window: it is read but it bounds no sensitivity"
            )
    return {
        "gate_verdict": gate_verdict,
        "excess_loss_vs_null": excess_loss,
        "gate_slack": slack_by_rung,
        "gate_notes": notes,
        "resolved_gates": 0 if sweep is None else int(sweep["n_valid_rungs"]),
        "gate_rungs": rungs,
        "gate_sensitivity": (
            {"ladder_refused": dict(refusal or {})}
            if sweep is None
            else {
                "bin_hz": sweep["bin_hz"],
                "window_verdict": sweep["window_verdict"],
                "window_verdict_reasons": list(sweep["window_verdict_reasons"]),
                "sensitivity": sensitivity,
                "null_reason": sweep.get("sensitivity_null_reason"),
                "poses": sweep["poses"],
            }
        ),
        "tension": tension,
    }


def _gate_rungs(sweep: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every rung's own facts, additive to the verdict the ladder composed.

    ``_gate_call`` carries only what the verdict turned on. A reader auditing
    the ladder itself needs every rung, including the ones whose own
    resolution bars them from bounding a sensitivity. The per-pose values are
    in ``gate_sensitivity``; this is their pooled form.
    """
    if sweep is None:
        return {}
    poses = sweep["poses"]
    return {
        rung_key: {
            "pooled_db": float(
                np.median([pose["detrended_db_by_rung"][rung_key] for pose in poses])
            ),
            "sigma_db": sigma,
            "cycles": sweep["cycles_by_rung"][rung_key],
            "resolution": sweep["resolution_by_rung"][rung_key],
            "resolved": sweep["resolution_by_rung"][rung_key] != "invalid",
        }
        for rung_key, sigma in sweep["sigma_db_by_rung"].items()
    }
