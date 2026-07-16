# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop measurement-level solver (W2.1 / punchlist #31).

Hardware session 2026-07-16 (jts3, build 28e93c044) measured driver sweeps
running at ~24 dB below the driver-safety effective-peak ceiling, in a room
whose ambient noise put worst-band SNR at 11.0-16.1 dB against a 20 dB floor
-- a deterministic "insufficient" terminal every time, even though every
input needed to choose a louder, still-safe sweep level was already known
before the tone played. Nothing in the pipeline used that headroom.

This module is the ONE new owner of that decision: given the driver's
measured chain gain, the room's ambient noise, and every existing safety
ceiling, choose the quietest (main_volume_db, commissioning_gain_db) pair
that clears the SNR requirement -- or a typed refusal when no safe pair
does. Pure math: stdlib/``math`` only, frozen dataclasses, no I/O, no
CamillaDSP or playback awareness. Callers own reading a driver's level lock,
loading its confirmed safety ceilings, and feeding the chosen level into the
EXISTING excitation-plan / admission machinery
(:mod:`jasper.active_speaker.excitation_safety_plan`,
:mod:`jasper.audio_measurement.excitation_admission`) unchanged -- this
module's output is a proposal; admission is still the law (defense in
depth).

Ledger recap (the invariant every formula below assumes):
:class:`~jasper.active_speaker.excitation_safety_plan.DriverSweepGeneratorPlan`
defines ``effective_peak_dbfs = 20*log10(amplitude) + commissioning_gain_db +
main_volume_db``, with ``commissioning_gain_db`` and ``main_volume_db`` both
constrained non-positive (CamillaDSP's 0 dB safety ceiling -- see
``devices.volume_limit`` and ``CamillaController.set_volume_db``). This
module never proposes a value that would violate that invariant.

``gain_map_db`` (the level-match ramp's recovered chain gain,
``settled_mic_dbfs - main_volume_db``,
:func:`jasper.audio_measurement.ramp.MeasurementRamp._record_settled_evidence`)
is measured while the ramp plays its own fixed calibration tone at
``AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS`` (-12 dBFS today), NOT at 0 dBFS
full scale. So for an arbitrary sweep excitation with its own
``effective_peak_dbfs``, the predicted mic peak is::

    predicted_mic_peak_dbfs = effective_peak_dbfs + gain_map_db
                               - tone_amplitude_dbfs

(the ``- tone_amplitude_dbfs`` backs the fixed calibration-tone level out of
``gain_map_db`` before recombining it with a *different* excitation's own
ledger). When the sweep's own source amplitude equals the tone's reference
amplitude -- true today, both pinned at -12 dBFS -- the two cancel and this
reduces to ``commissioning_gain_db + main_volume_db + gain_map_db``.

Two solver-owned ceilings have no other owner:
* ``MIC_CLIP_CEILING_DBFS`` -- the predicted mic peak must stay this far
  below true digital full scale, so a "successful" solve cannot itself
  corrupt the capture it is trying to enable.
* ``LF_AMBIENT_MARGIN_DB`` -- when no per-band ambient measurement is
  available (the phone-side ``ambient_stats`` event is a later PR; see
  :func:`parse_ambient_stats_event`), the fallback synthesizes per-band
  ambient from one broadband reading. Low-frequency room noise (HVAC,
  traffic, appliance rumble) routinely runs louder than a broadband RMS
  average suggests, so bands whose center sits at or below
  ``LF_MARGIN_FULL_HZ`` get the full conservative margin added; bands at or
  above ``LF_MARGIN_ZERO_HZ`` get none; in between the margin tapers
  log-linearly. This is a deliberately pessimistic guess, never a measured
  fact -- it exists so the solver does not choose a level that turns out too
  quiet against the room's real low end.

Selection rule: the solver always prefers the quietest total level that
clears the target requirement (``quality_model.snr_warn_db`` +
``SOLVER_MARGIN_DB``), never the loudest one available. When the target
cannot be reached even at every ceiling's limit, it falls back to the
loudest available level PROVIDED that still clears the bare floor
(``snr_warn_db``, no margin) -- this is what "engage commissioning_gain,
main volume pinned at cap" describes for an unusually insensitive driver
(the tweeter case in the pinned regression below). Only when even that bare
floor is unreachable does the solver refuse.

Levers, in preference order: ``main_volume_db`` climbs first (bounded by the
caller-supplied ``main_volume_cap_db`` -- the level-match ramp's own
``RampData.cap_db``, i.e. "cap_db from ramp context"); ``commissioning_gain_db``
only rises off its current baseline (never below it -- this module never
attenuates further than the caller's baseline) when ``main_volume_db`` is
already pinned at its cap and the requirement is still unmet.

Regression anchor (2026-07-16 jts3 session numbers, pinned by
``tests/test_audio_measurement_level_solver.py``): ambient -42.3 dBFS
broadband, gain_map_db +1.9 dB (woofer) / -16.4 dB (tweeter), driver-safety
ceiling -8 dBFS, floor 20 dB + margin 6 dB -> the woofer solve predicts
>=26 dB worst-band SNR using main_volume_db alone; the tweeter solve pins
main_volume_db at its cap and engages commissioning_gain_db to close as much
of the gap as the ceilings allow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .excitation import AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
from .quality_model import QualityModel

# The solver's own margin on top of quality_model.snr_warn_db -- insurance
# against the solver's prediction not exactly matching the room's real
# behavior (see the "Bounded correction" retry this margin exists to make
# usually unnecessary).
SOLVER_MARGIN_DB = 6.0

# How far below true digital full scale (0 dBFS) a solved level's predicted
# mic peak must stay. A solve that itself clips the mic corrupts the very
# capture it exists to enable.
MIC_CLIP_CEILING_DBFS = -6.0

# Fallback low-frequency ambient synthesis (see the module docstring). A band
# centered at or below LF_MARGIN_FULL_HZ gets the full margin; at or above
# LF_MARGIN_ZERO_HZ, none; log-linear taper between.
LF_AMBIENT_MARGIN_DB = 8.0
LF_MARGIN_FULL_HZ = 200.0
LF_MARGIN_ZERO_HZ = 2000.0

# Number of log-spaced sub-bands the fallback synthesizes across the
# admitted excitation range when no per-band ambient-stats event is
# available. Coarse on purpose -- this is a conservative guess, not a
# measurement; a handful of bands is enough to let a wide admitted range
# (e.g. a woofer's 40-400 Hz) show a worse low-end sub-band than its high
# end without pretending to 1/3-octave precision it doesn't have.
FALLBACK_BAND_COUNT = 4

REFUSAL_ROOM_TOO_NOISY = "room_too_noisy_for_safe_measurement"

# The ambient-stats event schema this Pi build understands. PR-a (this
# module) only ever PARSES the event; the phone-side emitter is a later PR.
AMBIENT_STATS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AmbientBand:
    """One measured or synthesized ambient-noise reading."""

    lo_hz: float
    hi_hz: float
    rms_dbfs: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.lo_hz) or not math.isfinite(self.hi_hz):
            raise ValueError("AmbientBand edges must be finite")
        if self.lo_hz <= 0.0 or self.hi_hz <= self.lo_hz:
            raise ValueError("AmbientBand edges must increase and be positive")
        if not math.isfinite(self.rms_dbfs):
            raise ValueError("AmbientBand rms_dbfs must be finite")


@dataclass(frozen=True)
class BandDetail:
    """One admitted-band entry considered by the solve, for observability."""

    lo_hz: float
    hi_hz: float
    ambient_dbfs: float
    predicted_snr_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lo_hz": round(self.lo_hz, 1),
            "hi_hz": round(self.hi_hz, 1),
            "ambient_dbfs": round(self.ambient_dbfs, 2),
            "predicted_snr_db": round(self.predicted_snr_db, 2),
        }


@dataclass(frozen=True)
class SolvedLevel:
    """The chosen sweep-level proposal. Still subject to admission."""

    main_volume_db: float
    commissioning_gain_db: float
    predicted_worst_band_snr_db: float
    band_detail: tuple[BandDetail, ...]
    achieved_target: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "main_volume_db": round(self.main_volume_db, 2),
            "commissioning_gain_db": round(self.commissioning_gain_db, 2),
            "predicted_worst_band_snr_db": round(
                self.predicted_worst_band_snr_db, 2
            ),
            "band_detail": [band.to_dict() for band in self.band_detail],
            "achieved_target": self.achieved_target,
        }


@dataclass(frozen=True)
class LevelSolveRefusal:
    """Typed refusal: no safe (main_volume_db, commissioning_gain_db) pair
    clears even the bare SNR floor. Fires BEFORE any tone plays."""

    code: str
    failing_band_hz: tuple[float, float]
    required_db: float
    available_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "failing_band_hz": [
                round(self.failing_band_hz[0], 1),
                round(self.failing_band_hz[1], 1),
            ],
            "required_db": round(self.required_db, 2),
            "available_db": round(self.available_db, 2),
        }


def _fallback_ambient_bands(
    admitted_lo_hz: float,
    admitted_hi_hz: float,
    ambient_broadband_dbfs: float,
) -> tuple[AmbientBand, ...]:
    """Synthesize per-band ambient from one broadband reading + LF margin."""

    log_lo = math.log10(admitted_lo_hz)
    log_hi = math.log10(admitted_hi_hz)
    step = (log_hi - log_lo) / FALLBACK_BAND_COUNT
    edges = [10.0 ** (log_lo + step * i) for i in range(FALLBACK_BAND_COUNT + 1)]
    edges[0] = admitted_lo_hz
    edges[-1] = admitted_hi_hz
    log_full = math.log10(LF_MARGIN_FULL_HZ)
    log_zero = math.log10(LF_MARGIN_ZERO_HZ)
    bands: list[AmbientBand] = []
    for lo, hi in zip(edges, edges[1:]):
        if hi <= lo:
            continue
        center = math.sqrt(lo * hi)
        ratio = (log_zero - math.log10(center)) / (log_zero - log_full)
        weight = min(1.0, max(0.0, ratio))
        bands.append(
            AmbientBand(
                lo_hz=lo,
                hi_hz=hi,
                rms_dbfs=ambient_broadband_dbfs + LF_AMBIENT_MARGIN_DB * weight,
            )
        )
    return tuple(bands)


def _clip_ambient_bands(
    bands: Sequence[AmbientBand],
    admitted_lo_hz: float,
    admitted_hi_hz: float,
) -> tuple[AmbientBand, ...]:
    """Keep only bands overlapping the admitted excitation range."""

    return tuple(
        band
        for band in bands
        if band.hi_hz > admitted_lo_hz and band.lo_hz < admitted_hi_hz
    )


def solve_level(
    *,
    gain_map_db: float,
    admitted_band_hz: tuple[float, float],
    commissioning_gain_baseline_db: float,
    main_volume_cap_db: float,
    max_effective_peak_dbfs: float,
    ambient_broadband_dbfs: float,
    ambient_bands: Sequence[AmbientBand] | None = None,
    model: QualityModel,
    solver_margin_db: float = SOLVER_MARGIN_DB,
    sweep_amplitude_dbfs: float = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
    tone_amplitude_dbfs: float = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
) -> SolvedLevel | LevelSolveRefusal:
    """Choose the quietest safe (main_volume_db, commissioning_gain_db).

    ``gain_map_db`` is the driver's level-match lock's recovered chain gain.
    ``admitted_band_hz`` is the driver-safety-confirmed excitation band
    (already the intersection of the hard and measurement bands -- unchanged
    from today's admission). ``commissioning_gain_baseline_db`` is the
    currently-applied per-role gain this driver's sweep would otherwise use
    (a value <= 0.0); the solve never goes MORE negative than this baseline,
    only less. ``main_volume_cap_db`` is the level-match ramp's own dynamic
    cap for this geometry (``RampData.cap_db``). ``max_effective_peak_dbfs``
    is the driver-safety-confirmed ceiling on
    ``DriverSweepGeneratorPlan.effective_peak_dbfs``.

    ``ambient_bands``, when given (from a validated phone ``ambient_stats``
    event -- see :func:`parse_ambient_stats_event`), is used directly, no LF
    weighting. When absent or empty after clipping to the admitted band, the
    solver synthesizes a conservative per-band estimate from
    ``ambient_broadband_dbfs`` (see the module docstring).
    """

    admitted_lo, admitted_hi = float(admitted_band_hz[0]), float(admitted_band_hz[1])
    if admitted_hi <= admitted_lo:
        raise ValueError("admitted_band_hz must increase")
    if commissioning_gain_baseline_db > 0.0:
        raise ValueError("commissioning_gain_baseline_db must be non-positive")
    if main_volume_cap_db > 0.0:
        raise ValueError("main_volume_cap_db must be non-positive")

    bands = _clip_ambient_bands(ambient_bands or (), admitted_lo, admitted_hi)
    if not bands:
        bands = _fallback_ambient_bands(
            admitted_lo, admitted_hi, ambient_broadband_dbfs
        )

    requirement_db = model.snr_warn_db + solver_margin_db
    bare_floor_db = model.snr_warn_db

    # The amplitude terms only matter when the sweep's own source peak
    # differs from the level-match tone's reference peak; kept general
    # rather than assuming they always match (see the module docstring).
    amplitude_correction_db = tone_amplitude_dbfs - sweep_amplitude_dbfs

    def predicted_mic_peak_dbfs(commissioning_gain_db: float, main_volume_db: float) -> float:
        effective_peak_dbfs = (
            sweep_amplitude_dbfs + commissioning_gain_db + main_volume_db
        )
        return effective_peak_dbfs + gain_map_db - tone_amplitude_dbfs

    def sum_for_target_mic_dbfs(target_mic_dbfs: float) -> float:
        """The (commissioning_gain_db + main_volume_db) total achieving
        ``target_mic_dbfs`` at the mic, given the fixed sweep amplitude."""
        return (
            target_mic_dbfs
            - gain_map_db
            + tone_amplitude_dbfs
            - sweep_amplitude_dbfs
        )

    max_sum_from_levers = main_volume_cap_db + 0.0  # V<=cap, C<=0
    max_sum_from_peak_ceiling = max_effective_peak_dbfs - sweep_amplitude_dbfs
    max_sum_from_mic_clip = sum_for_target_mic_dbfs(MIC_CLIP_CEILING_DBFS)
    max_sum = min(max_sum_from_levers, max_sum_from_peak_ceiling, max_sum_from_mic_clip)

    worst_band = max(bands, key=lambda band: band.rms_dbfs)
    required_sum = sum_for_target_mic_dbfs(worst_band.rms_dbfs + requirement_db)
    achieved_target = required_sum <= max_sum
    chosen_sum = required_sum if achieved_target else max_sum

    # The achieved worst-band SNR at chosen_sum, computed directly so the
    # reported number matches whichever ceiling actually bound it (levers,
    # peak, or mic-clip), not just the lever-cap case.
    predicted_worst_band_snr_db = (
        chosen_sum + gain_map_db - amplitude_correction_db
    ) - worst_band.rms_dbfs

    if not achieved_target and predicted_worst_band_snr_db < bare_floor_db:
        return LevelSolveRefusal(
            code=REFUSAL_ROOM_TOO_NOISY,
            failing_band_hz=(worst_band.lo_hz, worst_band.hi_hz),
            required_db=bare_floor_db,
            available_db=predicted_worst_band_snr_db,
        )

    # Allocate chosen_sum: main_volume_db climbs first (bounded by its cap),
    # commissioning_gain_db only rises off baseline when volume alone (at
    # its cap) still cannot reach chosen_sum.
    volume_needed = chosen_sum - commissioning_gain_baseline_db
    if volume_needed <= main_volume_cap_db:
        main_volume_db = volume_needed
        commissioning_gain_db = commissioning_gain_baseline_db
    else:
        main_volume_db = main_volume_cap_db
        commissioning_gain_db = min(0.0, chosen_sum - main_volume_db)

    band_detail = tuple(
        BandDetail(
            lo_hz=band.lo_hz,
            hi_hz=band.hi_hz,
            ambient_dbfs=band.rms_dbfs,
            predicted_snr_db=(
                predicted_mic_peak_dbfs(commissioning_gain_db, main_volume_db)
                - band.rms_dbfs
            ),
        )
        for band in bands
    )

    return SolvedLevel(
        main_volume_db=main_volume_db,
        commissioning_gain_db=commissioning_gain_db,
        predicted_worst_band_snr_db=predicted_worst_band_snr_db,
        band_detail=band_detail,
        achieved_target=achieved_target,
    )


def parse_ambient_stats_event(
    raw: Mapping[str, Any] | None,
    *,
    expected_run_token: str,
) -> tuple[AmbientBand, ...] | None:
    """Validate one phone ``ambient_stats`` event; ``None`` means fall back.

    PR-a (this module) only parses this event -- the phone-side emitter
    lands in a later PR, so today every caller feeds ``None`` and always
    takes the broadband-fallback path in :func:`solve_level`. The parser
    exists now so the wire contract is pinned before the emitter exists,
    and so a future emitter (or a stale/old phone page) that omits or
    malforms the event degrades cleanly rather than crashing.

    Returns ``None`` (never raises) for: a missing/non-mapping event, a
    ``run_token`` mismatch (a stale event from a previous attempt), a
    ``schema`` this build does not understand, a ``clipped`` capture (the
    phone's own quiet-window recording clipped -- its levels cannot be
    trusted), or malformed/empty ``bands``.
    """

    if not isinstance(raw, Mapping):
        return None
    stats = raw.get("ambient_stats")
    if not isinstance(stats, Mapping):
        return None
    if str(stats.get("run_token") or "") != expected_run_token:
        return None
    schema = stats.get("schema")
    if not isinstance(schema, int) or bool(isinstance(schema, bool)):
        return None
    if schema != AMBIENT_STATS_SCHEMA_VERSION:
        # Unknown schema: WARN-and-fallback is the caller's job (it has the
        # logger); this pure parser just declines to produce bands.
        return None
    if stats.get("clipped") is True:
        return None
    raw_bands = stats.get("bands")
    if not isinstance(raw_bands, (list, tuple)) or not raw_bands:
        return None
    bands: list[AmbientBand] = []
    for entry in raw_bands:
        if not isinstance(entry, Mapping):
            return None
        try:
            band = AmbientBand(
                lo_hz=float(entry["lo_hz"]),
                hi_hz=float(entry["hi_hz"]),
                rms_dbfs=float(entry["rms_dbfs"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        bands.append(band)
    return tuple(bands)
