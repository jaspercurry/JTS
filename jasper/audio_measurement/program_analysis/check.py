# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CHECK-phase helpers: ambient, pilots, channel map and the gain plan."""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import snr_policy
from jasper.audio_measurement.alignment import _bandlimit
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    ExcitationProgram,
    KIND_PILOT,
    ProgramSegment,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.log_event import log_event
from .model import (
    CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB,
    CHANNEL_MAP_MIN_ISOLATION_DB,
    CHANNEL_MAP_TARGET_RISE_DB,
    DBFS_FLOOR,
    GAIN_BOUND_CAPTURE_FLOOR,
    GAIN_BOUND_DEGENERATE_AMBIENT,
    GAIN_BOUND_FLAT_TARGET,
    GAIN_BOUND_NO_AMBIENT_EVIDENCE,
    GAIN_BOUND_PILOT_SNR,
    GAIN_BOUND_ROOM_SNR,
    GAIN_MAX_DIGITAL_PEAK_DBFS,
    GainPlan,
    LINEARITY_TOLERANCE_DB,
    logger,
    MEASURE_SNR_SOLVE_MARGIN_DB,
    MeasurementPriors,
    PILOT_FADE_TRIM_S,
    PILOT_MIN_SNR_DB,
    PilotObservation,
    RoleGainSolve,
    SegmentLocation,
    sweep_band_crest_factor_db,
    SWEEP_PEAK_TO_RMS_DB,
)
from .signals import _peak_dbfs


# How much of the scheduled ambient window must actually be present in the
# capture for it to count as evidence. A capture that started late clips the
# window's HEAD (never its tail — the pilots follow it), and RMS is
# length-independent, so a shortened window is still an honest floor estimate;
# what this rejects is the degenerate case where a couple of hundred samples
# survive and the estimate is noise about noise. Below the fraction the caller
# gets ``None`` and the analysis degrades to "no ambient evidence, trust the
# pilots" — never to a fabricated floor.
#
# ONE policy, both windows: CHECK's 12 s session-ambient window
# (`_ambient_from_capture`) and MEASURE/VERIFY's 1 s pilot-ambient window
# (`_pilot_ambient_samples`) ask the same question of the same kind of
# evidence, so they share this constant rather than each carrying a number that
# can drift from the other.
AMBIENT_MIN_USABLE_FRACTION = 0.5


def _ambient_from_capture(
    capture: np.ndarray, sample_rate: int, ambient_seg: ProgramSegment, global_offset: int
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """CHECK's session-ambient window and its band-floor report.

    The window is CLIPPED to the capture, never SLID along it: ``end`` is
    computed from the window's own (possibly negative) schedule position,
    not the clamped start — sliding forward would read whatever the
    schedule put AFTER the window (on the shipped CHECK program, the
    courtesy beep) as if it were room floor, 39.5 dB hot on a 0.6 s late
    start. That number feeds BOTH `_snr_floor_ok` and `_solve_gain_plan`.

    Below :data:`AMBIENT_MIN_USABLE_FRACTION` of the window this degrades
    the same honest way `_pilot_ambient_samples` does: ``None`` samples
    plus an EMPTY band report (fail-closed, never a fabricated floor).
    """
    begin = global_offset + ambient_seg.start_sample
    start = max(0, begin)
    end = min(capture.size, begin + ambient_seg.n_samples)
    if end - start < AMBIENT_MIN_USABLE_FRACTION * ambient_seg.n_samples:
        # Never a silent degrade: distinguishes "the room was quiet" from
        # "we never heard the room", which costs a commissioning attempt.
        log_event(
            logger,
            "program_analysis.ambient_window_unusable",
            level=logging.WARNING,
            scheduled_samples=int(ambient_seg.n_samples),
            surviving_samples=int(max(0, end - start)),
            capture_late_samples=int(max(0, -begin)),
        )
        empty = np.empty(0, dtype=np.float64)
        return None, snr_policy.framed_ambient_band_report(empty, sample_rate, percentile=95)
    samples = capture[start:end]
    return samples, snr_policy.framed_ambient_band_report(samples, sample_rate, percentile=95)


def _pilot_ambient_samples(
    program: ExcitationProgram, capture: np.ndarray, global_offset: int,
) -> np.ndarray | None:
    """The program's own room-listening window, or ``None`` if it has none.

    MEASURE/VERIFY programs carry an
    :data:`~jasper.audio_measurement.program.AMBIENT_SEGMENT_ID` window
    ahead of their leading pilot pair so `_pilot_observations`' in-band SNR
    guard has something to measure against; without it the guard's input
    is ``+inf`` and can never fire.

    Located by SCHEDULE offset, not correlation (it is silence). Clipped to
    the capture, never slid along it, sharing :data:`AMBIENT_MIN_USABLE_FRACTION`
    with `_ambient_from_capture`. Replay failure direction is safe: a
    too-loud "ambient" reads as low SNR, resolving ``linearity_ok`` to
    ``None`` rather than a false AGC accusation.
    """
    try:
        seg = program.segment(AMBIENT_SEGMENT_ID)
    except KeyError:
        return None
    begin = global_offset + seg.start_sample
    start = max(0, begin)
    end = min(capture.size, begin + seg.n_samples)
    if end - start < AMBIENT_MIN_USABLE_FRACTION * seg.n_samples:
        return None
    return capture[start:end]


def _band_power(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """Mean-square (linear power) of ``samples`` restricted to ``[f1_hz, f2_hz]``.

    Hann-windowed before :func:`_bandlimit`'s zero-phase FFT bandpass: a raw
    slice rarely starts/ends at a zero crossing, so an un-windowed
    brick-wall filter leaks broadband energy from that boundary into every
    band. The Hann taper's constant windowing loss cancels out of every
    comparison that reads both sides through this same function.

    Returned as LINEAR power (not dB) so a caller can SUBTRACT an ambient
    noise-power estimate before converting to dB.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8:
        return 0.0
    filtered = _bandlimit(x * np.hanning(x.size), sample_rate, f1_hz, f2_hz)
    return float(np.mean(np.square(filtered)))


def _band_rms_dbfs(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """RMS level (dBFS) of ``samples`` restricted to ``[f1_hz, f2_hz]``.
    Thin dB wrapper over :func:`_band_power`."""
    power = _band_power(samples, sample_rate, f1_hz, f2_hz)
    if power <= 0 or not math.isfinite(power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(power))


def _pilot_trim_fade(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Drop the composer's fixed edge fade (`PILOT_FADE_TRIM_S`) from a
    located pilot segment. Falls back to the untrimmed segment when
    trimming would leave nothing; downstream SNR/linearity gates still
    catch a genuinely bad capture.
    """
    trim = int(round(PILOT_FADE_TRIM_S * sample_rate))
    if samples.size <= 2 * trim:
        return samples
    return samples[trim:-trim]


def _ambient_subtracted_dbfs(power: float, ambient_power: float) -> float:
    """dB of ``power`` after subtracting ``ambient_power`` (power domain).
    ``ambient_power`` is 0.0 with no ambient evidence (see
    `_pilot_ambient_samples`), degrading to plain in-band RMS.
    """
    signal_power = power - ambient_power if ambient_power > 0 else power
    if signal_power <= 0 or not math.isfinite(signal_power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(signal_power))


def _pilot_in_band_snr_db(power: float, ambient_power: float) -> float:
    """SNR (dB) of the ambient-subtracted estimate: ``(P - N) / N`` = ``S / N``
    in the ``P = S + N`` model, the linear SNR `PILOT_MIN_SNR_DB` is stated
    in. ``+inf`` with no ambient evidence; ``-inf`` when measured power
    does not even exceed ambient.
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

    Declared bands legitimately overlap around the crossover point (design
    §5.2/§5.4), and that shared part carries no map-discrimination signal —
    the CROSS test (`_channel_map_ok`) only asks about the EXCLUSIVE
    remainder (interval subtraction; 0, 1, or 2 pieces).
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
    channel_map_ambient_samples: np.ndarray | None = None,
) -> list[PilotObservation]:
    """Per-role pilot level/linearity/channel-map observations (design §3.4).

    Level is measured band-relative (each pilot's OWN declared band via
    `_band_power`) and, when an ambient window is available,
    ambient-power-subtracted before converting to dB — a full-band PEAK
    estimate would let LF room rumble inflate the quiet pilot's level and
    compress the captured delta. With no window (``ambient_samples=None``)
    subtraction is a no-op and SNR is trusted unconditionally.

    Two ambient parameters: ``ambient_samples`` feeds level/SNR;
    ``channel_map_ambient_samples`` feeds `_channel_map_ok`'s TARGET/CROSS
    rise test. CHECK passes the same 12 s window to both; MEASURE/VERIFY
    pass only the first, since their ~1 s pre-pilot window was never
    calibrated for the rise thresholds — their channel-map check keeps the
    total-in-band-energy-fraction fallback instead.

    The composer's fixed edge fade (`_pilot_trim_fade`) is trimmed before
    measuring so the RMS rides the steady-state portion, not the ramp.

    Low-SNR honest routing: the quiet (lo) pilot's in-band SNR
    (`_pilot_in_band_snr_db`) gates trust. Below `PILOT_MIN_SNR_DB`,
    ``linearity_ok`` is ``None`` (UNKNOWN, never a false failure or pass)
    and ``snr_valid=False`` routes to the honest room/positioning reason
    instead of blaming the phone's AGC.

    ``peak_lo_dbfs``/``peak_hi_dbfs`` are a SEPARATE, non-ambient-subtracted
    full-band `_peak_dbfs`: `_solve_gain_plan` uses a pilot level
    ABSOLUTELY, so an ambient-subtracted level would shift that reference
    by however much power was subtracted (measured 13-17 dB on real
    captures).
    """
    by_id = {loc.segment_id: loc for loc in locations}
    roles = sorted({seg.role for seg in program.segments if seg.kind == KIND_PILOT and seg.role})
    # Every role's declared band, for the CROSS test's "did energy also
    # rise in every OTHER role's band" question.
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
        # UNKNOWN below the SNR floor, never True: the captured delta is not
        # evidence in EITHER direction down there.
        linearity_ok = (
            None if not snr_valid
            else abs(captured_delta - programmed_delta) <= LINEARITY_TOLERANCE_DB
        )

        # Gain-solve reference: full-band peak, NOT the ambient-subtracted level.
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
            ambient_samples=channel_map_ambient_samples, other_bands=other_bands,
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
            programmed_hi_gain_db=hi_seg.gain_db,
        ))
    return out


def _aggregate_tri_state_ok(
    verdicts: Sequence[bool | None],
) -> bool | None:
    """Reduce per-role tri-state verdicts to one, FAILURE-dominant.

    A FAILURE anywhere is the verdict; otherwise an UNKNOWN anywhere makes
    the whole verdict unknown ("the roles we could read were fine" is not
    "every role was fine"). ``None`` for no roles at all. Written out
    rather than ``all(...)``, which folds ``None`` to False — for
    ``channel_map_ok`` that would be a hard stop on evidence never there.
    """
    if not verdicts:
        return None
    if any(v is False for v in verdicts):
        return False
    if any(v is None for v in verdicts):
        return None
    return True


def _aggregate_linearity_ok(
    pilots: Sequence[PilotObservation],
) -> bool | None:
    """Per-pilot ``linearity_ok`` over the roles, through the shared fold."""
    return _aggregate_tri_state_ok([p.linearity_ok for p in pilots])


def _pilot_verdicts(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    global_offset: int,
) -> tuple[tuple[PilotObservation, ...], bool | None, bool | None, bool | None]:
    """Pilot observations + the aggregate linearity/channel-map/SNR verdicts.

    ``None`` verdicts when the program carries no pilots, so a caller can
    distinguish "no pilot evidence" from "pilot evidence, all clean".
    Shared by v2 MEASURE/VERIFY, whose leading pilot pair (design §5.2)
    reads its own pre-pilot ambient window so ``pilot_snr_ok`` is a real
    verdict; the channel-map check still uses
    `_channel_map_ok`'s one-sided fallback (see `_pilot_observations`).
    """
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=_pilot_ambient_samples(program, capture, global_offset),
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = _aggregate_tri_state_ok([p.channel_map_ok for p in pilots])
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    return tuple(pilots), linearity_ok, channel_map_ok, pilot_snr_ok


def _channel_map_ok(
    samples: np.ndarray,
    sample_rate: int,
    seg: ProgramSegment,
    *,
    ambient_samples: np.ndarray | None = None,
    other_bands: Sequence[tuple[float, float]] = (),
) -> tuple[bool | None, float | None, float | None]:
    """Band-relative channel-map sanity (design note above `CHANNEL_MAP_*`).

    Given an ambient window, asks two independent questions per pilot
    rather than a single total-energy fraction a concurrent unrelated
    room-noise band can veto:

    1. TARGET: did THIS driver's own declared band rise
       ``CHANNEL_MAP_TARGET_RISE_DB`` above that band's ambient level?
    2. CROSS: did every OTHER driver's band stay at least
       ``CHANNEL_MAP_MIN_ISOLATION_DB`` below this driver's own rise (the
       ISOLATION RATIO)? Guards ABNORMAL CROSS-BAND ENERGY (bleed, skirt,
       nonlinearity) — not the mis-wire discriminator, which rung 1
       catches. A ratio rather than an additive bound because honest
       cross-band content sits at a roughly fixed RELATIVE level (see
       ``CHANNEL_MAP_MIN_ISOLATION_DB``'s derivation). Judged only above
       ``CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB``, since below it the CROSS
       test would turn a retriable ``snr_floor`` into a rewire hard stop.

    Without an ambient window, falls back to the fraction test (energy in
    the declared band must exceed half the pilot window's total spectral
    energy) — the path v2 MEASURE/VERIFY and a windowless program take.
    That fallback is ONE-SIDED: a cleared fraction is ``None`` (UNKNOWN,
    since broadband room noise clears it too); a failed fraction keeps its
    ``False``.

    Returns ``(ok, target_rise_db, cross_rise_db)`` — the two RAW rise
    numbers, so an operator can see WHICH half moved (the ratio is derived
    by `channel_map_isolation_db`). ``cross_rise_db`` is the rise that
    failed CROSS when ``ok`` is False, or the worst rise observed when
    ``ok`` is True; both are ``None`` on the fallback path or with no
    ``other_bands``.
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
        # One-sided: the fail is a finding, the pass is not evidence.
        if float(np.sum(spectrum[in_band])) / total > 0.5:
            return None, None, None
        return False, None, None

    target_rise = (
        _band_rms_dbfs(x, sample_rate, seg.f1_hz, seg.f2_hz)
        - _band_rms_dbfs(ambient_samples, sample_rate, seg.f1_hz, seg.f2_hz)
    )
    if target_rise < CHANNEL_MAP_TARGET_RISE_DB:
        return False, target_rise, None
    # Cross rises are always MEASURED; only JUDGED above CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB.
    judge_cross = target_rise >= CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB
    worst_cross_rise: float | None = None
    for other_f1, other_f2 in other_bands:
        cross_rise = (
            _band_rms_dbfs(x, sample_rate, other_f1, other_f2)
            - _band_rms_dbfs(ambient_samples, sample_rate, other_f1, other_f2)
        )
        if worst_cross_rise is None or cross_rise > worst_cross_rise:
            worst_cross_rise = cross_rise
        if not judge_cross:
            continue
        isolation = channel_map_isolation_db(target_rise, cross_rise)
        # Fail-closed: an unjudgeable ratio must never read as a PASS.
        if isolation is None or isolation < CHANNEL_MAP_MIN_ISOLATION_DB:
            return False, target_rise, cross_rise
    return True, target_rise, worst_cross_rise


def channel_map_isolation_db(
    target_rise_db: float | None, cross_rise_db: float | None
) -> float | None:
    """The channel-map ISOLATION RATIO: this driver's rise minus the cross
    rise. ONE definition, read by both `_channel_map_ok`'s decision and
    every reporting surface. ``None`` whenever either rise is absent — a
    caller must treat that as "no evidence", never a pass.
    """
    if target_rise_db is None or cross_rise_db is None:
        return None
    return target_rise_db - cross_rise_db


def _bands_overlap(
    lo_a: float, hi_a: float, lo_b: float, hi_b: float
) -> bool:
    return hi_a > lo_b and lo_a < hi_b


def _ambient_rows_in_band(
    band_hz: tuple[float, float],
    ambient_bands: Sequence[Any],
) -> list[tuple[float, float, float]]:
    """The ``(lo_hz, hi_hz, level_dbfs)`` ambient rows overlapping ``band_hz``.
    A row this cannot read is skipped rather than raised on, never crashing
    inside CHECK's accept path.
    """
    lo, hi = band_hz
    rows: list[tuple[float, float, float]] = []
    for entry in ambient_bands or ():
        if not isinstance(entry, Mapping):
            continue
        edges = entry.get("band_hz")
        if not (isinstance(edges, (list, tuple)) and len(edges) == 2):
            continue
        try:
            b_lo, b_hi, level = (
                float(edges[0]), float(edges[1]), float(entry["level_dbfs"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(b_lo) and math.isfinite(b_hi) and math.isfinite(level)):
            continue
        if _bands_overlap(lo, hi, b_lo, b_hi):
            rows.append((b_lo, b_hi, level))
    return rows


def _band_required_snr_db(
    lo_hz: float, hi_hz: float, overlap_hz: tuple[float, float] | None
) -> float:
    """The SNR the fit needs in one band, per the split SNR policy.

    ``jasper.audio_measurement.snr_policy`` splits SNR trust by what a number
    is used FOR: a magnitude/trim decision is usable at
    ``DRIVER.snr_ok_db``, while a null/alignment decision (MEASURE's GCC
    delay + polarity estimate, which reads the crossover overlap band) needs
    ``DRIVER.alignment_snr_ok_db``. A band inside the overlap window carries
    the alignment requirement; every other band carries the magnitude one.

    ``overlap_hz`` is ``None`` when no Fc prior reached this analysis. That
    resolves to the alignment requirement EVERYWHERE — the conservative
    direction, since a higher requirement means a LOUDER solve. An unknown Fc
    must never buy a quieter measurement.
    """
    if overlap_hz is None or _bands_overlap(
        lo_hz, hi_hz, overlap_hz[0], overlap_hz[1]
    ):
        return DRIVER.alignment_snr_ok_db
    return DRIVER.snr_ok_db


def _solve_role_gain(
    *,
    role: str,
    k_db: float,
    flat_target_gain_db: float,
    band_hz: tuple[float, float] | None,
    pilot_delta_db: float,
    ambient_bands: Sequence[Any],
    overlap_hz: tuple[float, float] | None,
) -> RoleGainSolve:
    """The quietest MEASURE gain for one driver that still serves the fit.

    ``k_db`` is this driver's measured chain gain (captured peak minus the
    digital gain that produced it), so a target capture peak ``C`` is
    reached at digital gain ``C - k_db``. Three floors compete and the
    LOUDEST wins. Every arm is peak-expressed via
    :func:`sweep_band_crest_factor_db`, since ambient levels and SNR
    requirements are RMS.

    * **room SNR** — worst ``ambient + required_snr`` across ambient bands
      overlapping this driver's own measurement band (band-scoped so a
      tweeter needs less drive than a woofer). The ambient table
      (``snr_policy.CROSSOVER_SNR_BANDS_HZ``) is coarse in two known ways,
      both erring LOUD: wide rows near a sweep's edge inherit the row's
      full level, and the table stops at 12 kHz (room noise there is below
      every lower band anyway).
    * **pilot SNR** — MEASURE's leading pilot pair fails when its quiet
      side's in-band SNR falls under ``PILOT_MIN_SNR_DB``; applied to every
      role as a floor so it stays correct even if the composer moves the pair.
    * **capture floor** — ``DRIVER.peak_too_low_dbfs``, a TRIPWIRE not a
      shippable bound: if it wins, both other arms have resolved below an
      unmeasurable level, so the solve is REFUSED (falls back to
      ``flat_target_gain_db`` with ``bound_by=GAIN_BOUND_DEGENERATE_AMBIENT``
      and a WARNING) rather than shipping a level that once solved a real
      driver to -45 dBFS, 34 dB below the flat level.

    The result is clamped by ``flat_target_gain_db``: this solve can only
    make MEASURE quieter than the level-only figure, never louder.
    """
    rows = _ambient_rows_in_band(band_hz, ambient_bands) if band_hz else []
    if not rows:
        # Disclosed fallback: no ambient evidence to solve against.
        return RoleGainSolve(
            role=role,
            gain_db=flat_target_gain_db,
            flat_target_gain_db=flat_target_gain_db,
            bound_by=GAIN_BOUND_NO_AMBIENT_EVIDENCE,
            band_hz=band_hz,
        )

    demands: list[tuple[float, float, float, float]] = []
    for lo, hi, level in rows:
        required_snr = (
            _band_required_snr_db(lo, hi, overlap_hz) + MEASURE_SNR_SOLVE_MARGIN_DB
        )
        # level + required_snr is band-RMS; carry the crest factor so both
        # sides of the eventual peak comparison are peak-expressed.
        crest = sweep_band_crest_factor_db(band_hz, (lo, hi)) if band_hz else 0.0
        demands.append((level + required_snr + crest, level, required_snr, crest))
    required_capture_dbfs, ambient_dbfs, required_snr_db, crest_factor_db = max(
        demands, key=lambda item: item[0]
    )
    # Named residual, erring QUIET: built from the single worst overlapping
    # ROW rather than the pilot's whole band. Not the binding arm anywhere
    # measured (JTS3: room arm wins by 16-19 dB).
    worst_ambient_dbfs = max(level for _lo, _hi, level in rows)
    pilot_floor_dbfs = (
        worst_ambient_dbfs + pilot_delta_db + PILOT_MIN_SNR_DB
        + MEASURE_SNR_SOLVE_MARGIN_DB
        + SWEEP_PEAK_TO_RMS_DB
    )
    capture_dbfs, bound_by = max(
        (
            (required_capture_dbfs, GAIN_BOUND_ROOM_SNR),
            (pilot_floor_dbfs, GAIN_BOUND_PILOT_SNR),
            (DRIVER.peak_too_low_dbfs, GAIN_BOUND_CAPTURE_FLOOR),
        ),
        key=lambda item: item[0],
    )
    if bound_by == GAIN_BOUND_CAPTURE_FLOOR:
        # A floor-bound solve is not a level, it is evidence the ambient
        # report cannot be solved against — refuse, keep the flat target.
        log_event(
            logger,
            "program_analysis.measure_level_solve_refused",
            level=logging.WARNING,
            role=role,
            reason=GAIN_BOUND_DEGENERATE_AMBIENT,
            capture_floor_dbfs=round(DRIVER.peak_too_low_dbfs, 2),
            required_capture_dbfs=round(required_capture_dbfs, 2),
            pilot_floor_dbfs=round(pilot_floor_dbfs, 2),
            ambient_dbfs=round(ambient_dbfs, 2),
            fallback_gain_db=round(flat_target_gain_db, 3),
        )
        return RoleGainSolve(
            role=role,
            gain_db=flat_target_gain_db,
            flat_target_gain_db=flat_target_gain_db,
            bound_by=GAIN_BOUND_DEGENERATE_AMBIENT,
            band_hz=band_hz,
            # Retained deliberately: what the ambient report claimed, not just that it was rejected.
            ambient_dbfs=ambient_dbfs,
            required_snr_db=required_snr_db,
            required_capture_dbfs=required_capture_dbfs,
            crest_factor_db=crest_factor_db,
        )
    gain_db = capture_dbfs - k_db
    if gain_db >= flat_target_gain_db:
        gain_db, bound_by = flat_target_gain_db, GAIN_BOUND_FLAT_TARGET
    return RoleGainSolve(
        role=role,
        gain_db=gain_db,
        flat_target_gain_db=flat_target_gain_db,
        bound_by=bound_by,
        band_hz=band_hz,
        ambient_dbfs=ambient_dbfs,
        required_snr_db=required_snr_db,
        required_capture_dbfs=required_capture_dbfs,
        crest_factor_db=crest_factor_db,
    )


def _solve_gain_plan(
    program: ExcitationProgram,
    pilots: Sequence[PilotObservation],
    ambient_report: Mapping[str, Any],
    priors: MeasurementPriors,
) -> GainPlan:
    target = priors.target_capture_dbfs
    ambient_bands = (
        ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    ) or ()
    # The nominal Fc +/- 1 octave window, UNCLAMPED: a narrower band would
    # buy a quieter solve on a technicality. Wider is the safe read here.
    overlap_hz = (
        overlap_band_hz(float(priors.crossover_fc_hz))
        if priors.crossover_fc_hz else None
    )
    gains: dict[str, float] = {}
    solves: dict[str, RoleGainSolve] = {}
    predicted_peaks: list[float] = []
    for pilot in pilots:
        lo_seg = program.segment(f"pilot_{pilot.role}_lo")
        hi_seg = program.segment(f"pilot_{pilot.role}_hi")
        # captured = digital_gain + K (unit slope). K from the two pilots,
        # deliberately the PEAK-referenced levels, not the ambient-subtracted ones.
        k_lo = pilot.peak_lo_dbfs - lo_seg.gain_db
        k_hi = pilot.peak_hi_dbfs - hi_seg.gain_db
        k = (k_lo + k_hi) / 2.0
        # The level-only answer, and the CEILING of the solve below.
        flat_gain = min(target - k, GAIN_MAX_DIGITAL_PEAK_DBFS)  # >=6 dB guard
        solve = _solve_role_gain(
            role=pilot.role,
            k_db=k,
            flat_target_gain_db=flat_gain,
            # A CHECK pilot's band IS the role's MEASURE sweep band.
            band_hz=(
                (float(lo_seg.f1_hz), float(lo_seg.f2_hz))
                if lo_seg.f1_hz is not None and lo_seg.f2_hz is not None
                else None
            ),
            pilot_delta_db=abs(hi_seg.gain_db - lo_seg.gain_db),
            ambient_bands=ambient_bands,
            overlap_hz=overlap_hz,
        )
        gains[pilot.role] = solve.gain_db
        solves[pilot.role] = solve
        predicted_peaks.append(solve.gain_db)
    predicted_peak = max(predicted_peaks) if predicted_peaks else GAIN_MAX_DIGITAL_PEAK_DBFS

    # Deliberately judged at `target_capture_dbfs`, NOT the solved level —
    # this is the room-quality gate ("is this room quiet enough at all"),
    # a different question from the per-driver solve above.
    snr_floor_ok = _snr_floor_ok(ambient_report, target)
    return GainPlan(
        gain_db=gains,
        predicted_peak_dbfs=predicted_peak,
        snr_floor_ok=snr_floor_ok,
        role_solves=solves,
    )


def _snr_floor_ok(ambient_report: Mapping[str, Any], target_capture_dbfs: float) -> bool:
    """False when the ambient report is missing, empty, or every row is
    unreadable — never raises on a malformed ``level_dbfs``.
    """
    bands = ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    if not bands:
        return False
    worst: float | None = None
    for b in bands:
        if not isinstance(b, Mapping):
            continue
        try:
            level = float(b["level_dbfs"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(level):
            continue
        if worst is None or level > worst:
            worst = level
    if worst is None:
        return False
    return (target_capture_dbfs - worst) >= DRIVER.snr_ok_db
