# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Delta-probe verification: did the speaker do what the correction asked?

The linearization-integrity ladder's PR-L5 primitive
(``docs/linearization-integrity-plan.md``). Every applied correction change is
verified as a **realized-vs-commanded per-frequency map** and classified into
one of four verdicts; the three non-matched ones roll the correction back
automatically. Pure computation — numpy in, a frozen verdict record out. No
I/O, no conductor state, no rollback: the conductor owns those.

**Why this exists.** On 2026-07-27 a linearization shipped whose emitted
shelves were realized at Q 0.476 while every gate in the fit engine evaluated
them at Q 0.707 (``slope: 6`` is not CamillaDSP's Butterworth — PR-L2). The
fit's realization gate, its residual, and its VERIFY prediction all used the
same wrong evaluator, so a shelf that missed its design by up to 1.70 dB
scored as exact. **A model cannot audit itself.** The only instrument that can
is a measurement of what the hardware actually did, compared against what the
filters were told to do. That is this module. PR-L2 fixed the specific Q bug;
this catches the whole class, permanently, including the next one.

**What "realized" and "commanded" are, exactly** (read this before trusting a
verdict — the algebra matters):

* ``commanded_delta_db`` is the correction's own predicted transfer on the
  summed response: the linearized-branch prediction minus the raw-branch
  prediction, both built from the SAME measured branches with the SAME
  summation model. The branch measurements and the summation model therefore
  cancel out of it; what survives is the shape the emitted filters and trims
  command.
* ``realized_delta_db`` is the measured post-apply response minus the SAME
  raw-branch prediction, at the same microphone position.

Their difference — the ``error_db`` map this module classifies — is
algebraically ``measured_post − predicted_post``: the raw-branch prediction
cancels. That is deliberate and is stated here so nobody later reads the delta
framing as implying an independent pre-apply *measurement* at the mark (there
is none; MEASURE captures per-driver sweeps, not a summed curve). The delta
framing earns its keep in two places the plain residual cannot reach: the
commanded curve is the axis the shortfall-vs-model-error discriminator
regresses against, and the spatial arm below IS two real measurements.

**This is not the VERIFY tracking check, and does not replace it.**
``crossover_v2_flow._verify_verdict`` compares the same two curves over the
crossover handoff band alone (``[Fc/2, 2·Fc]``, ~2–4 kHz on JTS3) at 1.5 dB.
The 2026-07-27 shelf error lived at 5–12 kHz — an octave and a half above
that band's top — and tracking could not have seen it at any tolerance. This
probe runs over **the band the correction actually commands something in**,
which is the only band where "did it do what we asked" is a question.

**Verdict priority.** ``matched`` at the mark and ``spatially_costly`` are
independent questions, so the order between them is a policy choice and it is
this: a map that does NOT match at the mark is diagnosed as a chain defect
(``model_error`` / ``level_dependent_shortfall``) even when the spatial arm
also flags, because the chain defect is the more proximate cause and the more
actionable remedy. ``spatially_costly`` is reserved for the case that is
otherwise invisible — the correction did exactly what it was asked at the
mark, and the room got less even for it. The two remedies are genuinely
different (fix the model / move the speaker), which is why one verdict must
win rather than both being reported as equals. The losing arm's evidence
still travels in the record.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

# --------------------------------------------------------------------------- #
# verdict vocabulary
# --------------------------------------------------------------------------- #

#: The correction realized what it commanded. Keep it.
VERDICT_MATCHED = "matched"
#: Realized and commanded disagree in SHAPE — the emitted filters are not
#: doing what the fit's model of them says they do. Roll back and flag. This
#: is the verdict that catches the PR-L2 shelf-Q class forever.
VERDICT_MODEL_ERROR = "model_error"
#: Realized tracks commanded in shape but falls materially short in scale,
#: where what was commanded is a lift — the driver did not deliver the level
#: it was asked for. A compression diagnostic. Roll back and flag.
VERDICT_LEVEL_DEPENDENT_SHORTFALL = "level_dependent_shortfall"
#: The map matched at the mark, but the cross-position spread WIDENED — the
#: correction bought flatness at one spot by trading it elsewhere, which is
#: the signature of correcting a position-specific interference feature. Roll
#: back and route the household to a placement-vs-speaker service verdict.
VERDICT_SPATIALLY_COSTLY = "spatially_costly"
#: No verdict is available — the correction commands nothing inside the
#: probe band, or the curves could not be compared. **Not a pass.** The
#: conductor must treat this the way every other honesty instrument in this
#: flow treats an unknown: no evidence to refuse on, and no permission
#: granted either.
VERDICT_UNAVAILABLE = "unavailable"

#: Every verdict this module can return. Pinned by a test so a new
#: classification path cannot ship an un-enumerated string.
DELTA_PROBE_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
    VERDICT_UNAVAILABLE,
})

#: The verdicts on which rollback is AUTOMATIC (plan PR-L5: "Rollback is
#: automatic on the non-matched classes"). ``unavailable`` is deliberately
#: NOT here — an absent measurement is not evidence of a bad correction, and
#: rolling back on it would revert every session whose household closed the
#: phone before the post-apply sweep.
DELTA_PROBE_ROLLBACK_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
})

# --------------------------------------------------------------------------- #
# classification thresholds
# --------------------------------------------------------------------------- #

# Max |realized − commanded| tolerated below :data:`DELTA_PROBE_HF_SPLIT_HZ`.
#
# 1.5 dB, for two reasons that agree. (a) It is the flow's own established bar
# for "a measurement matched its prediction" — ``crossover_v2_flow.
# VERIFY_TOLERANCE_DB`` — so this probe and the tracking check do not hold the
# same chain to two different standards in two different bands. (b) It must sit
# BELOW the defect it exists to catch: the 2026-07-27 shelf-Q realization error
# peaked at 1.70 dB (FORENSICS-SYNTHESIS.md, chunk 2), deepest around 6.9 kHz —
# inside this tier. The margin is only 0.2 dB at the peak, which is why the
# exceedance-WIDTH rule below matters: that error is a wide systematic tilt
# across the whole shelf transition, so it clears a width test comfortably
# even where it barely clears the amplitude one.
DELTA_PROBE_TOLERANCE_LOW_DB: float = 1.5

# Max |realized − commanded| tolerated at/above :data:`DELTA_PROBE_HF_SPLIT_HZ`.
#
# Measurement uncertainty grows with frequency and a rollback fabricated by
# HF noise is worse than no probe at all. The fit engine's own repeat-agreement
# gate (``linearization_fit.HF_AGREEMENT_LIMIT_HIGH_DB``) ACCEPTS up to 2.0 dB
# of spread between repeat sweeps of the same driver at these frequencies, and
# the owner's per-serial UMIK-2 uncertainty research puts the stock-cal
# protocol at ~±2.3 dB @16 kHz. A tolerance at or under 2.0 would therefore be
# rejecting corrections for noise the fit engine already declared acceptable.
# 2.5 clears both, and the width rule still has to be satisfied on top.
DELTA_PROBE_TOLERANCE_HIGH_DB: float = 2.5

# Where the low tier ends and the high tier begins. Mirrors
# ``linearization_fit._HF_AGREEMENT_TIER_SPLIT_HZ`` so "high frequencies" means
# one thing across the fit and its verification.
DELTA_PROBE_HF_SPLIT_HZ: float = 10_000.0

# A tolerance exceedance must span at least this many contiguous octaves to
# count as a finding.
#
# The measured curves are ladder-smoothed at 1/6 octave below 4 kHz and 1/3
# octave from there up, so an excursion narrower than one smoothing window is
# measurement texture, not a claim about the model — the same argument
# ``linearization_fit.HF_REALIZATION_TOLERANCE_DB`` records ("an isolated
# 1.5-2.0 dB excursion at the smoothing scale is measurement texture, not a
# shape failure"). One third of an octave is the coarsest of those windows, so
# a run this wide has survived a full smoothing window everywhere in the band.
# Every realization defect this probe is built for — a mis-Q'd shelf, a
# mis-modelled slope, a compressed driver — is broad by construction; none of
# them produces a single-bin spike.
DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES: float = 1.0 / 3.0

# Below this, the correction commands nothing worth verifying at that bin.
# Mirrors ``linearization_fit._MIN_FILTER_GAIN_DB`` — the fit engine's own
# "this filter is cosmetic" floor. You can only ask "did it do what we asked"
# where something was asked.
DELTA_PROBE_MIN_COMMANDED_DB: float = 0.5

# The probe band must retain at least this many bins after masking, or there
# is not enough of a curve to regress or to measure a run width against.
DELTA_PROBE_MIN_BINS: int = 8

# Best-fit realized/commanded scale factor below which a shape-tracking map is
# called a level-dependent SHORTFALL rather than a model error.
#
# 0.85 is chosen to agree with :data:`DELTA_PROBE_TOLERANCE_LOW_DB` about what
# "material" means at the depths this fit produces: a 15% shortfall on the
# ~10 dB lift a CD-horn continuation commands is 1.5 dB, exactly the low-band
# tolerance. So a shortfall large enough to be named here is always large
# enough to have failed the amplitude test that got us into this branch, and
# the two constants cannot disagree about a correction of that size.
DELTA_PROBE_SHORTFALL_GAIN_CEILING: float = 0.85

# Widening of the across-position level spread (``BandSpread.sigma_db``, dB)
# beyond which the post-apply cloud is called spatially costly.
#
# The envelope's own ``linearization_envelope.position_stability_limit`` spends
# this spread as ``sigma_db / sqrt(n_positions)`` when deciding how much
# correction depth a band may have at all — so 1.0 dB of RAW sigma growth is
# already several times the depth the cloud terms would have licensed in that
# band. A correction that widens the room's spread by that much is not
# flattening the speaker; it is fitting one microphone position.
DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB: float = 1.0


# --------------------------------------------------------------------------- #
# spatial arm
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpatialCost:
    """Did the correction make the room LESS even? (the spatial arm)

    Built from the two position groups the flow already walks — CLOUD_MEASURE
    (pre-apply) and CLOUD_VERIFY (post-apply) — so unlike the at-the-mark arm
    this one really is measurement-minus-measurement. ``available`` is False
    when either group carries no usable spread (fewer than two positions: the
    express tier's post-apply group is the mark alone by design, and
    ``spatial_combine`` returns no ``band_spread`` below N=2).
    """

    available: bool
    widened: bool
    worst_center_hz: float
    worst_widening_db: float
    tolerance_db: float
    n_bands: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "widened": self.widened,
            "worst_center_hz": self.worst_center_hz,
            "worst_widening_db": self.worst_widening_db,
            "tolerance_db": self.tolerance_db,
            "n_bands": self.n_bands,
        }


SPATIAL_COST_UNAVAILABLE = SpatialCost(
    available=False, widened=False, worst_center_hz=0.0,
    worst_widening_db=0.0, tolerance_db=DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
    n_bands=0,
)


def evaluate_spatial_cost(
    before: Sequence[Any],
    after: Sequence[Any],
    *,
    tolerance_db: float = DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
) -> SpatialCost:
    """Compare two clouds' per-octave level spread, before vs after the apply.

    ``before``/``after`` are ``spatial_combine.BandSpread`` sequences (duck-
    typed on ``center_hz``/``sigma_db``, so a test fixture need not import the
    real class). Bands are paired by ``center_hz``; a band present in only one
    group is skipped rather than compared against nothing.

    ``sigma_db`` — the spread of each position's BAND LEVEL — is the right
    reading here, not ``max_sigma_db``. ``max_sigma_db`` rides comb nulls on
    purpose, and comb structure moves with the microphone whether or not a
    correction was applied; a level-spread comparison asks the question this
    verdict is actually about, which is whether the corrected speaker is more
    or less even across the room than the uncorrected one was.
    """
    before_by_center = {
        round(float(b.center_hz), 3): float(b.sigma_db)
        for b in before
        if math.isfinite(float(b.sigma_db))
    }
    worst_center_hz = 0.0
    worst_widening_db = -math.inf
    n_bands = 0
    for band in after:
        sigma_after = float(band.sigma_db)
        if not math.isfinite(sigma_after):
            continue
        key = round(float(band.center_hz), 3)
        if key not in before_by_center:
            continue
        n_bands += 1
        widening = sigma_after - before_by_center[key]
        if widening > worst_widening_db:
            worst_widening_db = widening
            worst_center_hz = float(band.center_hz)
    if n_bands == 0:
        return SPATIAL_COST_UNAVAILABLE
    return SpatialCost(
        available=True,
        widened=worst_widening_db > tolerance_db,
        worst_center_hz=worst_center_hz,
        worst_widening_db=float(worst_widening_db),
        tolerance_db=float(tolerance_db),
        n_bands=n_bands,
    )


# --------------------------------------------------------------------------- #
# the map
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeltaProbeMap:
    """One applied correction's realized-vs-commanded verdict and evidence.

    ``verdict`` is one of :data:`DELTA_PROBE_VERDICTS`; ``rollback`` is True
    exactly when it is in :data:`DELTA_PROBE_ROLLBACK_VERDICTS`, computed here
    so no caller can decide it differently.

    ``gain_factor`` is the least-squares realized/commanded scale through the
    origin over the probe band — 1.0 means the correction landed at full
    depth, 0.6 means it delivered 60% of what it asked for. It is reported on
    every classified map, not just the shortfall one, because it is the single
    most legible number in this record for a human reading the journal, and it
    is ``None`` on an unavailable map: 0.0 there would read as the measured
    claim "delivered nothing", which is the opposite of "not measured".
    """

    verdict: str
    reason: str
    probe_band_hz: tuple[float, float]
    n_bins: int
    max_error_db: float
    rms_error_db: float
    worst_hz: float
    exceedance_octaves: float
    gain_factor: float | None
    tolerance_low_db: float
    tolerance_high_db: float
    spatial: SpatialCost

    @property
    def matched(self) -> bool:
        return self.verdict == VERDICT_MATCHED

    @property
    def rollback(self) -> bool:
        return self.verdict in DELTA_PROBE_ROLLBACK_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "rollback": self.rollback,
            "probe_band_hz": list(self.probe_band_hz),
            "n_bins": self.n_bins,
            "max_error_db": self.max_error_db,
            "rms_error_db": self.rms_error_db,
            "worst_hz": self.worst_hz,
            "exceedance_octaves": self.exceedance_octaves,
            "gain_factor": self.gain_factor,
            "tolerance_low_db": self.tolerance_low_db,
            "tolerance_high_db": self.tolerance_high_db,
            "spatial": self.spatial.to_dict(),
        }


def _unavailable(reason: str, spatial: SpatialCost) -> DeltaProbeMap:
    return DeltaProbeMap(
        verdict=VERDICT_UNAVAILABLE, reason=reason, probe_band_hz=(0.0, 0.0),
        n_bins=0, max_error_db=0.0, rms_error_db=0.0, worst_hz=0.0,
        exceedance_octaves=0.0, gain_factor=None,
        tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
        tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
        spatial=spatial,
    )


def _tolerance_curve(freqs_hz: np.ndarray) -> np.ndarray:
    """The two-tier per-bin tolerance (see the two tolerance constants)."""
    return np.where(
        freqs_hz < DELTA_PROBE_HF_SPLIT_HZ,
        DELTA_PROBE_TOLERANCE_LOW_DB,
        DELTA_PROBE_TOLERANCE_HIGH_DB,
    )


def widest_exceedance_octaves(
    freqs_hz: np.ndarray, exceeds: np.ndarray,
) -> tuple[float, float]:
    """``(widest contiguous run in octaves, that run's low edge in Hz)``.

    A "run" is contiguous in GRID INDEX, not merely in the exceeding set — two
    exceeding bins either side of a compliant one are two runs, which is the
    whole point of a width rule. Width is measured in log2 frequency between
    the run's first and last bin, so it is the same quantity at 500 Hz and at
    15 kHz. Returns ``(0.0, 0.0)`` when nothing exceeds.
    """
    widest = 0.0
    widest_lo_hz = 0.0
    idx = np.flatnonzero(exceeds)
    if idx.size == 0:
        return 0.0, 0.0
    breaks = np.flatnonzero(np.diff(idx) != 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    for s, e in zip(starts, ends):
        lo_hz = float(freqs_hz[idx[s]])
        hi_hz = float(freqs_hz[idx[e]])
        if lo_hz <= 0.0 or hi_hz <= 0.0:
            continue
        span = math.log2(hi_hz / lo_hz) if hi_hz > lo_hz else 0.0
        if span > widest:
            widest = span
            widest_lo_hz = lo_hz
    return widest, widest_lo_hz


def _structured_exceedance(
    freqs_hz: np.ndarray,
    error_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
) -> tuple[bool, float]:
    """``(is a real finding, widest run in octaves)`` for one error curve.

    **Every array is on the FULL grid**, and ``probe_mask`` marks the bins
    inside the probe band. That is load-bearing, not a convenience: the width
    rule counts a run as contiguous in GRID INDEX, so it can only tell
    structure from texture if the grid it walks is the real one. Evaluating it
    on the compacted ``freqs[mask]`` subarray instead silently welds bins that
    are octaves apart in Hz into one "wide" run whenever the mask has a hole —
    and the commanded floor puts a hole in it on every ordinary correction that
    cuts low and boosts high. Two isolated single-bin errors either side of
    such a gap then scored 2.9 octaves of structure and rolled the correction
    back (adversarial review B1, reproduced). Masking the EXCEEDANCE instead
    keeps every removed bin as a run-breaker, which is what it physically is:
    a frequency where the correction asked for nothing, so nothing there can
    corroborate a defect on the far side of it.
    """
    exceeds = probe_mask & (np.abs(error_db) > tolerance_db)
    widest, _ = widest_exceedance_octaves(freqs_hz, exceeds)
    return widest >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES, widest


def classify_delta_probe(
    freqs_hz: np.ndarray,
    realized_delta_db: np.ndarray,
    commanded_delta_db: np.ndarray,
    *,
    band_hz: tuple[float, float],
    spatial: SpatialCost = SPATIAL_COST_UNAVAILABLE,
) -> DeltaProbeMap:
    """Classify one applied correction's realized-vs-commanded map.

    All three arrays share one frequency grid (the caller interpolates). The
    probe band is ``band_hz`` intersected with the bins where the correction
    commands at least :data:`DELTA_PROBE_MIN_COMMANDED_DB` — outside that,
    nothing was asked for and there is nothing to verify.

    Topology-agnostic by construction: this function knows about a measured
    curve, a commanded curve, and a band. It has no notion of drivers, ways,
    or crossovers, so a 1-way passive speaker's summed chain classifies
    through exactly this code path with no special case.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    realized = np.asarray(realized_delta_db, dtype=np.float64)
    commanded = np.asarray(commanded_delta_db, dtype=np.float64)
    if not (freqs.shape == realized.shape == commanded.shape):
        return _unavailable("grid_mismatch", spatial)

    lo_hz, hi_hz = float(band_hz[0]), float(band_hz[1])
    mask = (
        (freqs >= lo_hz)
        & (freqs <= hi_hz)
        & np.isfinite(realized)
        & np.isfinite(commanded)
        & (np.abs(commanded) >= DELTA_PROBE_MIN_COMMANDED_DB)
    )
    if int(mask.sum()) < DELTA_PROBE_MIN_BINS:
        return _unavailable("nothing_commanded", spatial)

    f = freqs[mask]
    r = realized[mask]
    c = commanded[mask]
    probe_band_hz = (float(f[0]), float(f[-1]))

    # Scalar statistics read the probe bins only — a bin outside the band
    # contributes to no claim. The exceedance WIDTH, by contrast, is measured
    # on the full grid with the mask applied to the exceedance itself, so grid
    # adjacency survives (see _structured_exceedance).
    error = r - c
    max_error_db = float(np.max(np.abs(error)))
    rms_error_db = float(np.sqrt(np.mean(error ** 2)))
    worst_hz = float(f[int(np.argmax(np.abs(error)))])
    # Least-squares realized/commanded scale through the origin. ``c`` carries
    # only bins at or above the commanded floor, so the denominator cannot be
    # degenerate here.
    gain_factor = float(np.dot(r, c) / np.dot(c, c))

    tolerance_full = _tolerance_curve(freqs)
    error_full = np.where(mask, realized - commanded, 0.0)
    exceeded, exceedance_octaves = _structured_exceedance(
        freqs, error_full, tolerance_full, mask,
    )

    def _map(verdict: str, reason: str) -> DeltaProbeMap:
        return DeltaProbeMap(
            verdict=verdict, reason=reason, probe_band_hz=probe_band_hz,
            n_bins=int(f.size), max_error_db=max_error_db,
            rms_error_db=rms_error_db, worst_hz=worst_hz,
            exceedance_octaves=float(exceedance_octaves),
            gain_factor=gain_factor,
            tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
            tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
            spatial=spatial,
        )

    if not exceeded:
        # The chain did what it was told at the mark. Now — and only now — is
        # the spatial question the interesting one (see the module docstring's
        # verdict-priority note).
        if spatial.available and spatial.widened:
            return _map(VERDICT_SPATIALLY_COSTLY, "cross_position_spread_widened")
        return _map(VERDICT_MATCHED, "")

    # The map does not match. Shape or scale?
    #
    # Re-measure the error against the best-fit SCALED command. If the residual
    # then passes, the correction's shape is right and only its depth is short
    # — the driver delivered a fraction of what it was asked for, uniformly.
    # If the residual still fails, the shape itself is wrong, which is a claim
    # about our model of the filters, not about the driver's headroom.
    scaled_error_full = np.where(mask, realized - gain_factor * commanded, 0.0)
    scaled_exceeded, _ = _structured_exceedance(
        freqs, scaled_error_full, tolerance_full, mask,
    )
    # A *level-dependent* shortfall is a claim about a driver failing to
    # deliver LEVEL, so it requires that level was what the correction asked
    # for. A proportional undershoot of a set of CUTS is not compression —
    # attenuation does not compress — and belongs in the model-error bucket
    # where someone will look at the filter math.
    commanded_is_lift = float(np.max(c)) >= DELTA_PROBE_MIN_COMMANDED_DB
    if (
        not scaled_exceeded
        and commanded_is_lift
        and 0.0 <= gain_factor < DELTA_PROBE_SHORTFALL_GAIN_CEILING
    ):
        return _map(VERDICT_LEVEL_DEPENDENT_SHORTFALL, "realized_short_of_commanded")
    return _map(VERDICT_MODEL_ERROR, "realized_shape_differs_from_commanded")


def spatial_cost_from_group_spreads(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
) -> SpatialCost:
    """Adapter: two cloud-group result mappings → a :class:`SpatialCost`.

    Reads the ``"band_spread"`` list each group publishes (a list of plain
    dicts after the JSON round-trip, or ``BandSpread`` objects in-process).
    Absent/short spreads degrade to :data:`SPATIAL_COST_UNAVAILABLE` rather
    than raising — an express session has no post-apply group at all, and
    "no spatial evidence" is an honest answer, not an error.
    """

    def _bands(group: Mapping[str, Any] | None) -> list[Any]:
        if not isinstance(group, Mapping):
            return []
        raw = group.get("band_spread")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []
        out: list[Any] = []
        for entry in raw:
            if isinstance(entry, Mapping):
                center = entry.get("center_hz")
                sigma = entry.get("sigma_db")
                if isinstance(center, (int, float)) and isinstance(sigma, (int, float)):
                    out.append(_PlainBand(float(center), float(sigma)))
            elif hasattr(entry, "center_hz") and hasattr(entry, "sigma_db"):
                out.append(entry)
        return out

    before_bands, after_bands = _bands(before), _bands(after)
    if not before_bands or not after_bands:
        return SPATIAL_COST_UNAVAILABLE
    return evaluate_spatial_cost(before_bands, after_bands)


@dataclass(frozen=True)
class _PlainBand:
    """The two fields :func:`evaluate_spatial_cost` reads, off a JSON dict."""

    center_hz: float
    sigma_db: float


__all__ = [
    "DELTA_PROBE_HF_SPLIT_HZ",
    "DELTA_PROBE_MIN_BINS",
    "DELTA_PROBE_MIN_COMMANDED_DB",
    "DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES",
    "DELTA_PROBE_ROLLBACK_VERDICTS",
    "DELTA_PROBE_SHORTFALL_GAIN_CEILING",
    "DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB",
    "DELTA_PROBE_TOLERANCE_HIGH_DB",
    "DELTA_PROBE_TOLERANCE_LOW_DB",
    "DELTA_PROBE_VERDICTS",
    "SPATIAL_COST_UNAVAILABLE",
    "DeltaProbeMap",
    "SpatialCost",
    "VERDICT_LEVEL_DEPENDENT_SHORTFALL",
    "VERDICT_MATCHED",
    "VERDICT_MODEL_ERROR",
    "VERDICT_SPATIALLY_COSTLY",
    "VERDICT_UNAVAILABLE",
    "classify_delta_probe",
    "evaluate_spatial_cost",
    "spatial_cost_from_group_spreads",
    "widest_exceedance_octaves",
]
