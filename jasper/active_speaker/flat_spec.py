# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The flat-linearization spec evaluator (flat-linearization plan, stage S2).

Pure computation only: numpy, plus one shared helper
(:func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`)
imported so the exclusion-interval merge rule has exactly one owner rather
than a near-copy on each side of the seam. No I/O, no logging, no product
policy, no CamillaDSP/emission imports. This module answers exactly one
question -- "does this spatially-combined, 1/3-oct-smoothed magnitude curve
meet the flat-linearization spec?" -- and nothing more.
:func:`spec_convergence_residual` is a second reading of that same
evaluation, not a second question: it pools the report's own per-band
numbers into the one scalar the plan's S3 closed loop converges on, and
holds no threshold or loop policy of its own. :func:`spec_flatness_gauge`
is a third reading of the same kind — the household-facing "how flat is
it" figures, every one of them lifted from the report rather than
recomputed (plan PR-5, the spec-curve SSOT). :func:`spec_band_tilt` is a
fourth, and the only one whose answer no reference-frame choice can move:
the largest level step between two graded bands, which is what a household
reading a worst-band pointer was actually asking (issue #1857).
`spec_flatness_gauge` pools its RMS through `spec_convergence_residual` rather
than owning a second pooling rule.

See docs/historical/linearization-campaign-2026-07.md, section "The spec -- what 'flat' means
here," for the definition this module implements: deviation = curve -
reference, evaluated per band at the tolerances in :data:`SPEC_BANDS`, with
interference-flagged bins excluded from both the reference and every band's
deviation metric. The reference is a power mean over
:data:`REFERENCE_BAND_HZ`, the LOW-MID band alone -- see that constant for
why it is no longer the campaign's original 250 Hz-8 kHz span.

**The table's edges are nominal; the graded edges are their intersection
with the session's trusted floor and ceiling** (issue #2551 for the floor).
:data:`SPEC_BANDS` and :data:`REFERENCE_BAND_HZ` are room-agnostic hand-set
constants, but a gated measurement is only trustworthy above ``2.5/T`` for
its own reflection-free window ``T``
(:func:`jasper.audio_measurement.gating.f_trusted_floor_hz`, and the E4
sweep behind it), and only up to the frequency its microphone is trusted at
(:func:`jasper.active_speaker.linearization_envelope.mic_trust_limit`).
``evaluate_flat_spec``'s ``trusted_floor_hz`` raises every band's lower edge
-- and the reference band's -- to ``max(f_lo, trusted_floor_hz)``, so no
verdict rests on a bin the capture's own gate cannot support; its
``trusted_ceiling_hz`` moves the top band's upper edge, and with it where
best-effort begins, so the graded span ENDS where the microphone stops being
trusted rather than at a hand-set 16 kHz. A band left entirely below the floor is
``evaluable=False`` with its ``graded_lo_hz`` sitting above its own top
edge, never ``passed=False``: there is no evidence there, which is not the
same as failing. This module still holds no gate policy -- it takes the
floor as a number from a caller that measured it. A band the floor cut but
did not swallow reports :attr:`BandResult.max_at_graded_edge` when its
extremum landed on the cut edge, which is the case where the reported number
is a LOWER BOUND on the band's real worst deviation rather than the thing
itself -- told to the reader rather than left to be derived.

**The tolerances are S0-contingent, not final.** The plan doc is explicit
that this table is provisional pending the S0 validation session's hardware
data (mic-move-only captures, no DSP changes) -- see the plan's "Rationale,
briefly" paragraph and "Open questions" items 1-2, 5 for exactly what S0/S3
may revise (achievable N/spread, the 250-vs-300 Hz lower edge, and whether
8-16 kHz can tighten from +/-2.5 to +/-2.0 dB once realization headroom is
measured bounce-free). "If S0 contradicts a tolerance, the table is revised
with the S0 data attached -- the spec serves the measurement, not the
reverse" (plan wording). This module encodes whichever table is currently
adopted; it does not itself track provisional-vs-final status.

Input contract: the caller supplies an already spatially-combined, 1/3-oct-
smoothed magnitude curve (`spec_smoothed_db`) on a matching frequency axis
(`freqs_hz`), plus an optional per-bin exclusion mask (the plan's
"interference honesty screen" -- cepstral power-vs-median disagreement
flags). This module does not combine captures, smooth curves, or detect
interference; it only evaluates a curve that has already been through those
upstream steps. It deliberately consumes plain arrays rather than a
specific upstream result type, so it stays decoupled from however the curve
was produced.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.room_boundary import GATED_SPEC_LOWER_EDGE_HZ
from jasper.audio_measurement.spatial_combine import merged_true_intervals

# Above this frequency the plan's table reads "best-effort, disclosed, never
# specced" -- never evaluated against a tolerance, never counted toward
# overall_passed. A bin at exactly this frequency is best-effort, not the top
# of SPEC_BANDS[-1] (which is this same value, by reference below) -- the two
# partitions meet with no gap or overlap.
#
# NOMINAL, the same way GATED_SPEC_LOWER_EDGE_HZ is: a `trusted_ceiling_hz`
# MOVES it, up on a microphone trusted past 16 kHz and down on one trusted
# below (See ADR-0194). `FlatSpecReport.best_effort_above_hz` publishes where
# it actually landed, and SPEC_BANDS[-1] reads it rather than repeating the
# literal so the graded top and the best-effort boundary cannot drift apart.
BEST_EFFORT_ABOVE_HZ: float = 16000.0

# The adopted spec table -- docs/historical/linearization-campaign-2026-07.md, "The spec --
# what 'flat' means here." Each entry is (f_lo_hz, f_hi_hz, tolerance_db);
# band membership is f_lo <= f < f_hi (inclusive-lower, exclusive-upper --
# see `evaluate_flat_spec`'s docstring for why and where this matters at
# the 2 kHz / 8 kHz seams). S0-CONTINGENT (see module docstring): revise
# only with S0/S3 data attached, per the plan's own "the spec serves the
# measurement, not the reverse" rule.
#
# Neither OUTER EDGE is a literal here. The lower one is the seam with the
# room-correction layer, owned by jasper.audio_measurement.room_boundary so
# the spec's floor and the room ceiling's clamp floor cannot drift apart
# (issue #1787, plan D3); the upper one is BEST_EFFORT_ABOVE_HZ above.
# Revising 250 -> 300 stays an S0-contingent decision; it just happens in that
# one module now. The tolerances and the inner edges remain this module's own.
#
# These edges are NOMINAL (issue #2551). What a given evaluation actually
# grades is this table intersected with that session's trusted floor and
# ceiling -- see `evaluate_flat_spec`'s `trusted_floor_hz`/
# `trusted_ceiling_hz` and `BandResult.graded_lo_hz`/`graded_hi_hz`.
SPEC_BANDS: tuple[tuple[float, float, float], ...] = (
    (GATED_SPEC_LOWER_EDGE_HZ, 2000.0, 1.5),
    (2000.0, 8000.0, 2.0),
    (8000.0, BEST_EFFORT_ABOVE_HZ, 2.5),
)

# The reference band is SPEC_BANDS[0] -- the LOW-MID band alone, the region a
# listener anchors tonality on and the tightest-toleranced row in the table.
# Uses the same inclusive-lower/exclusive-upper edge rule as SPEC_BANDS, so it
# spans exactly that band with no gap or overlap at the 2 kHz seam. Its lower
# edge is SPEC_BANDS[0]'s by construction, so it reads the same owner.
#
# It was the campaign's 250 Hz-8 kHz (SPEC_BANDS[0] union SPEC_BANDS[1]), which
# put the 2-8 kHz band INSIDE the frame its own deviation is stated against, so
# an elevation there read back at half its size while two untouched bands were
# charged the difference. #1857's Q-E anchor question, decided -- See ADR-0194
# for the measurement and what it supersedes.
#
# Nominal, like SPEC_BANDS: a `trusted_floor_hz` raises this lower edge too
# (issue #2551). It has to. The reference is a power mean, so leaving
# sub-floor bins in the frame while removing them from every band would let
# untrustworthy energy re-centre the zero that each surviving deviation is
# stated against -- the same "a contaminated bin must not re-centre the
# target" argument, applied to the one pooled quantity that can.
# `FlatSpecReport.reference_band_hz` publishes the span actually pooled.
REFERENCE_BAND_HZ: tuple[float, float] = (GATED_SPEC_LOWER_EDGE_HZ, 2000.0)


@dataclass(frozen=True)
class BandResult:
    """One :data:`SPEC_BANDS` entry's evaluation outcome.

    ``n_bins`` is the total number of ``freqs_hz`` bins landing in
    ``[f_lo_hz, f_hi_hz)``, regardless of exclusion; ``n_excluded`` is how
    many of those were interference-flagged and therefore excluded from the
    deviation metrics. ``n_bins - n_excluded`` is the number of bins those
    metrics were actually computed from.

    **Unevaluable is a first-class outcome, not a failure and not a pass.**
    When a band is left with zero non-excluded bins -- because the frequency
    axis never reached it, because the trusted floor left nothing of it, or
    because the interference screen flagged every bin in it -- there is no
    evidence, so ``evaluable`` is ``False``, every metric below is ``None``,
    and ``passed`` is ``None`` rather than a fabricated verdict. It is the
    honesty screen's job to remove interference-dominated bins; it must not
    be able to remove a whole band from scrutiny by *silently passing* it,
    nor to destroy the entire report by raising.
    :attr:`FlatSpecReport.overall_passed` treats an unevaluable band as
    not-passed, so an unevaluable band can never be mistaken for a clean
    one.

    Args:
      f_lo_hz: the band's NOMINAL lower edge (inclusive) -- its
        :data:`SPEC_BANDS` row, so a reader can tell which tolerance row
        this result answers for even when nothing in it was graded. Read
        ``graded_lo_hz`` for the edge the metrics were actually taken from.
      f_hi_hz: the band's NOMINAL upper edge (exclusive) -- its
        :data:`SPEC_BANDS` row, read like ``f_lo_hz``. Read ``graded_hi_hz``
        for the edge the metrics were actually taken to.
      tolerance_db: the band's +/- tolerance from :data:`SPEC_BANDS`.
      max_deviation_db: the **signed** deviation at the worst non-excluded
        bin -- the bin with the largest *absolute* deviation, reported with
        its sign kept, because "2.4 dB too loud" and "2.4 dB too quiet" call
        for opposite corrections and a bare magnitude hides which one it is.
        ``None`` when the band is unevaluable.
      max_deviation_hz: the frequency of that worst bin, so the number can
        be located on a chart without re-deriving it. ``None`` when the band
        is unevaluable.
      rms_deviation_db: RMS deviation over the band's non-excluded bins.
        ``None`` when the band is unevaluable.
      n_bins: total bins in the band, excluded or not.
      n_excluded: how many of those were interference-flagged.
      evaluable: whether any non-excluded bin survived to be measured.
      passed: ``abs(max_deviation_db) <= tolerance_db``, or ``None`` when
        the band is unevaluable.
      level_deviation_db: the band's OWN power-mean level (over the same
        non-excluded bins) minus :attr:`FlatSpecReport.reference_db` -- how
        far the whole band sits from the shared frame, with no reference to
        what happens inside it. See :func:`spec_band_tilt` for why this is
        split out (issue #1857). ``None`` when the band is unevaluable, or
        on a report built before this field existed.
      max_ripple_db: the **signed** worst deviation of a non-excluded bin
        from **the band's own level**, not from
        :attr:`FlatSpecReport.reference_db`. This is the half of
        ``max_deviation_db`` that is *inside* the band, and it is
        **invariant to the reference frame entirely**: change
        :data:`REFERENCE_BAND_HZ` to anything and this number does not
        move. ``None`` when unevaluable / absent.
      max_ripple_hz: the frequency of that worst-ripple bin. Deliberately
        NOT assumed equal to ``max_deviation_hz``: the bin furthest from
        the shared frame and the bin furthest from the band's own level are
        the same only when the band's level offset is zero. ``None`` when
        unevaluable / absent.
      graded_lo_hz: the lower edge the metrics above were **actually** taken
        from -- ``max(f_lo_hz, trusted_floor_hz)`` (issue #2551), the same
        nominal-vs-honest pair the gate's own delta probe publishes as
        ``literal_band_hz`` beside ``eval_band_hz``
        (:func:`jasper.audio_measurement.gate_disclosure.pre_post_gate_delta`).
        Equal to ``f_lo_hz`` when no floor was supplied or the floor sits
        below the band, and **greater than or equal to** ``f_hi_hz`` when
        the floor swallowed the band whole -- which is the tell that
        ``evaluable=False`` here means "below this session's trusted floor"
        rather than "the axis never reached it" or "the screen took every
        bin". ``None`` on a report built before this field existed; read
        ``f_lo_hz`` then, since nothing clamped in that era.
      graded_hi_hz: the upper edge the metrics were **actually** taken to,
        the mirror of ``graded_lo_hz``. Equal to ``f_hi_hz`` when no
        ceiling was supplied. The TOP band's edge follows the ceiling in
        both directions -- 8-20 kHz on a ``reference`` microphone, 8-12 kHz
        on a ``consumer`` one -- because that edge and
        :data:`BEST_EFFORT_ABOVE_HZ` are one number; a lower band's edge is
        only ever lowered. ``<= graded_lo_hz`` when the ceiling swallowed
        the band whole, which is the tell that ``evaluable=False`` here
        means "above this session's trusted ceiling". ``None`` on a report
        built before this field existed.
      max_at_graded_edge: whether this band was floor-truncated
        (``graded_lo_hz > f_lo_hz``) **and** ``max_deviation_hz`` is its
        LOWEST graded bin. Read it as: *extremum at the graded edge -- the
        band continues below the floor, ungraded.* ``False`` on a band whose
        worst bin sits inside the graded span, and on an untruncated band
        (there is no ungraded remainder to warn about). ``None`` when the
        band is unevaluable, or on a report built before this field existed.

        **What it does and does not claim.** The flag is exactly its two
        conjuncts: the floor cut this band, and the worst graded bin is the
        lowest one. It tests no SLOPE and makes no claim that the curve keeps
        rising below the floor -- what follows from it is weaker and provable,
        namely that ``max_deviation_db`` is a maximum over a SUBSET of the
        band and so a LOWER BOUND on the band's real worst deviation. "It may
        well be worse below" is the licensed reading; "it is still rising" is
        not, and the two are easy to conflate.

        **Why it earns a field rather than a reader's inference.** On
        the 2026-08-16 round-3 jts3 session the 250-2000 Hz band was graded
        from 357.14 Hz and reported ``+4.49 dB @ 358``, its first graded bin,
        while the ungraded region below it continued to ``+5.08 dB @ 329``.
        Both numbers are honest; only one was reported, and nothing in the
        report said the reported one sat on an edge. A reader COULD derive it
        from ``graded_lo_hz`` and ``max_deviation_hz`` -- but deriving a
        caveat is not being told one, and a derivable caveat is exactly the
        kind that goes underived.

        **Disclosure only**, like the #1857 split: ``passed`` is unchanged by
        its presence and is not computed from it. An edge extremum is still a
        real graded bin and still grades. What this bounds is the INFERENCE
        drawn from the number, not the verdict taken on it.

    **The split, and the identity that makes it exact.** For every
    non-excluded bin ``i`` in the band::

        deviation_i = curve_i - reference_db
                    = (curve_i - band_level) + (band_level - reference_db)
                    = ripple_i               + level_deviation_db

    so a band's distance from the frame is exactly "where the whole band
    sits" plus "what the curve does inside it" -- one term that a *different*
    band's level can move (the frame is pooled across bands) and one that it
    structurally cannot. Note the identity is PER BIN: ``max_deviation_db``
    and ``max_ripple_db`` are taken at bins chosen by different criteria, so
    they do not add. What always holds is
    ``abs(max_deviation_db) >= abs(level_deviation_db)`` -- a power mean lies
    between its inputs' min and max, so some bin always has ripple of each
    sign.
    """

    f_lo_hz: float
    f_hi_hz: float
    tolerance_db: float
    max_deviation_db: float | None
    max_deviation_hz: float | None
    rms_deviation_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool
    passed: bool | None
    # Defaulted, unlike every field above, for the same reason
    # `spec_convergence_residual` guards a state `evaluate_flat_spec` cannot
    # produce: a report can be hand-built or rehydrated from persistence
    # written before this split existed. `None` there is honest ("this
    # report does not carry the split"), and `spec_band_tilt` treats it as
    # such rather than fabricating a level.
    level_deviation_db: float | None = None
    max_ripple_db: float | None = None
    max_ripple_hz: float | None = None
    graded_lo_hz: float | None = None
    graded_hi_hz: float | None = None
    max_at_graded_edge: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "tolerance_db": self.tolerance_db,
            "max_deviation_db": self.max_deviation_db,
            "max_deviation_hz": self.max_deviation_hz,
            "rms_deviation_db": self.rms_deviation_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
            "passed": self.passed,
            # The #1857 attribution split. Disclosure only -- `passed` above
            # is unchanged by their presence and is not computed from them.
            "level_deviation_db": self.level_deviation_db,
            "max_ripple_db": self.max_ripple_db,
            "max_ripple_hz": self.max_ripple_hz,
            # #2551: the edge these numbers came from, beside the nominal
            # one above. Disclosure of a clamp that DID move the graded
            # numbers -- unlike the split, which moves none.
            "graded_lo_hz": self.graded_lo_hz,
            "graded_hi_hz": self.graded_hi_hz,
            # ...and whether that clamp left the reported extremum sitting on
            # its own edge, making it a lower bound on the band's real worst
            # deviation. Disclosure only; `passed` does not read it.
            "max_at_graded_edge": self.max_at_graded_edge,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BandResult":
        """The exact inverse of :meth:`to_dict` -- a rehydration, never a
        re-derivation. No band edge, floor, or tolerance is recomputed;
        every field is read back verbatim.

        Two vintages, two read rules, and conflating them is a silent-wrong
        trap. ``f_lo_hz`` through ``passed`` (ten fields, including
        ``max_deviation_db``/``max_deviation_hz``/``rms_deviation_db``) have
        been in :meth:`to_dict` since #1741 -- there has never been a
        persisted document that could lack one, so they are read with hard
        indexing (``raw["..."]``): a document missing one is CORRUPT and
        must raise ``KeyError``, not silently rehydrate a
        plausible-looking ``None``. (Several of these fields are themselves
        legitimately ``None`` on an unevaluable band -- ``raw["..."]``
        preserves that; the hardening is against the KEY being absent, not
        against the value being ``None``.) Only the six fields defaulted
        on the dataclass itself (``level_deviation_db`` through
        ``max_at_graded_edge`` -- the later additions) are read with
        :meth:`dict.get`, so a report persisted before that split existed
        rehydrates with the same ``None`` the dataclass default would give
        a hand-built one. An earlier version of this method read
        ``max_deviation_db``/``max_deviation_hz``/``rms_deviation_db``/
        ``passed`` the same lenient way as the five truly-optional fields
        below; a mutation dropping ``passed`` (paired with dropping
        :class:`FlatSpecReport`'s own ``excluded_intervals``) from a real
        document then rehydrated silently instead of raising, and the
        round-views grader built on top of it produced a WRONG number
        (+2.40 dB off) at exit 0 rather than failing loudly.
        """
        return cls(
            f_lo_hz=float(raw["f_lo_hz"]),
            f_hi_hz=float(raw["f_hi_hz"]),
            tolerance_db=float(raw["tolerance_db"]),
            max_deviation_db=raw["max_deviation_db"],
            max_deviation_hz=raw["max_deviation_hz"],
            rms_deviation_db=raw["rms_deviation_db"],
            n_bins=int(raw["n_bins"]),
            n_excluded=int(raw["n_excluded"]),
            evaluable=bool(raw["evaluable"]),
            passed=raw["passed"],
            level_deviation_db=raw.get("level_deviation_db"),
            max_ripple_db=raw.get("max_ripple_db"),
            max_ripple_hz=raw.get("max_ripple_hz"),
            graded_lo_hz=raw.get("graded_lo_hz"),
            graded_hi_hz=raw.get("graded_hi_hz"),
            max_at_graded_edge=raw.get("max_at_graded_edge"),
        )


@dataclass(frozen=True)
class FlatSpecReport:
    """The full flat-spec evaluation for one combined+smoothed curve.

    ``excluded_intervals`` collapses contiguous (by array index, on the
    strictly-ascending ``freqs_hz`` :func:`evaluate_flat_spec` requires)
    runs of the exclusion mask into merged ``(f_lo_hz, f_hi_hz)`` tuples
    spanning each run's first and last excluded bin, via the shared
    :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`.
    Diagnostic disclosure only -- pass/fail itself reads the exclusion mask
    directly, not this derived field. ``best_effort_above_hz`` echoes
    :data:`BEST_EFFORT_ABOVE_HZ` so a consumer rendering this report doesn't
    need a second import to know where the disclosed-only region begins.

    ``overall_passed`` is ``True`` only when **every** band is both
    evaluable and passing. An unevaluable band (see :class:`BandResult`)
    therefore drags the overall verdict to ``False``: the evaluator will not
    report a clean bill of health for a spectrum it could not fully measure.

    ``smoothing_fraction`` is **caller attestation**, not a measurement. The
    plan evaluates pass/fail at 1/3-octave, but a bare magnitude array
    carries no evidence of how it was smoothed, and this module deliberately
    does not smooth. The field records what the caller says it handed over,
    so a stored report can be audited later; nothing here validates it.

    ``trusted_floor_hz`` is the floor this evaluation was intersected at
    (issue #2551), echoed so a stored report says on its face which honesty
    floor produced its numbers rather than leaving a reader to infer it from
    the band edges. ``None`` means no floor was supplied -- **"not stated",
    never "zero"**, the same unknown-vs-zero rule the wiring layer's own
    floor fields follow. ``trusted_ceiling_hz`` is its mirror at the top,
    read the same way. ``reference_band_hz`` is the span whose power mean
    IS ``reference_db``, after that same clamp: it is
    :data:`REFERENCE_BAND_HZ` raised to the floor, and it is what
    :func:`spec_flatness_gauge` publishes rather than the module constant,
    so a surface naming the frame names the frame that was used.

    ``graded_band_hz`` is the whole span the table graded --
    ``(lowest band's graded_lo_hz, best_effort_above_hz)``. It is NOT
    ``reference_band_hz``: the frame the deviations are stated FROM is the
    low-mid band alone, while the span they are stated OVER runs to the
    trusted ceiling, and a consumer that needs "which bins did this
    evaluation grade" must read this one.
    """

    reference_db: float
    bands: tuple[BandResult, ...]
    overall_passed: bool
    excluded_intervals: tuple[tuple[float, float], ...]
    best_effort_above_hz: float
    smoothing_fraction: int
    # Defaulted for the same reason `BandResult`'s split fields are: a report
    # can be hand-built or rehydrated from persistence written before the
    # clamp existed. The defaults are that era's truth -- nothing clamped,
    # and the reference band was the module constant.
    trusted_floor_hz: float | None = None
    reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ
    trusted_ceiling_hz: float | None = None
    graded_band_hz: tuple[float, float] = (
        GATED_SPEC_LOWER_EDGE_HZ, BEST_EFFORT_ABOVE_HZ,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_db": self.reference_db,
            "bands": [band.to_dict() for band in self.bands],
            "overall_passed": self.overall_passed,
            "excluded_intervals": [list(interval) for interval in self.excluded_intervals],
            "best_effort_above_hz": self.best_effort_above_hz,
            "smoothing_fraction": self.smoothing_fraction,
            # #2551: the honesty floor these numbers were graded at, and the
            # span `reference_db` was pooled over once that floor applied.
            "trusted_floor_hz": self.trusted_floor_hz,
            "reference_band_hz": list(self.reference_band_hz),
            # The ceiling's mirror of the pair above: the microphone-trust
            # limit these numbers were graded to, and the whole span graded.
            "trusted_ceiling_hz": self.trusted_ceiling_hz,
            "graded_band_hz": list(self.graded_band_hz),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FlatSpecReport":
        """The exact inverse of :meth:`to_dict` -- a rehydration, never a
        re-derivation. No band edge, floor, or reference is recomputed;
        every field is read back verbatim through :meth:`BandResult.from_dict`
        and this report's own fields, under the same two-vintage read rule
        :meth:`BandResult.from_dict` documents: ``excluded_intervals`` has
        been in :meth:`to_dict` since #1741 and is read with hard indexing
        (``raw["excluded_intervals"]``) -- a document missing it is corrupt
        and must raise, not silently rehydrate an empty tuple that reads as
        "nothing was excluded" when the truth is "the field was lost."
        ``trusted_floor_hz`` (#2551) and ``reference_band_hz`` (#2551) are
        genuinely later additions and are read with :meth:`dict.get`, so a
        report persisted before either existed rehydrates with the
        dataclass's own default (``None``, :data:`REFERENCE_BAND_HZ``)
        rather than raising.
        """
        kwargs: dict[str, Any] = {}
        reference_band = raw.get("reference_band_hz")
        if reference_band is not None:
            kwargs["reference_band_hz"] = (float(reference_band[0]), float(reference_band[1]))
        graded_band = raw.get("graded_band_hz")
        if graded_band is not None:
            kwargs["graded_band_hz"] = (float(graded_band[0]), float(graded_band[1]))
        return cls(
            reference_db=float(raw["reference_db"]),
            bands=tuple(BandResult.from_dict(b) for b in raw["bands"]),
            overall_passed=bool(raw["overall_passed"]),
            excluded_intervals=tuple(
                (float(lo), float(hi)) for lo, hi in raw["excluded_intervals"]
            ),
            best_effort_above_hz=float(raw["best_effort_above_hz"]),
            smoothing_fraction=int(raw["smoothing_fraction"]),
            trusted_floor_hz=raw.get("trusted_floor_hz"),
            trusted_ceiling_hz=raw.get("trusted_ceiling_hz"),
            **kwargs,
        )


@dataclass(frozen=True)
class GradedSpec:
    """One :func:`evaluate_flat_spec` call: its inputs and its verdict, together.

    A consumer that needs to read the graded CURVE — not just the verdict about
    it — needs three arrays and a report that provably describe the same
    evaluation. Handing those over as four loose arguments makes it possible to
    pair a curve with someone else's mask, or a report with a re-derived one;
    the crossover blend correction
    (:mod:`jasper.active_speaker.crossover_v2.blend_correction`) reads all four
    and prescribes a filter from them, so "these came from one evaluation" has
    to be a property of the type rather than a convention at the call site.

    ``excluded`` is the mask as it was HANDED to the evaluator — for the
    spatial cloud that is the merged honesty mask (the combiner's
    power-vs-median screen unioned with the identified-null registry), which is
    the whole reason a consumer wants it rather than re-deriving one.

    Not persisted, and not a wire shape: :meth:`FlatSpecReport.to_dict` remains
    the durable copy of the half that is durable. This is a live in-process
    handoff, which is why it may hold arrays at all.
    """

    freqs_hz: np.ndarray
    curve_db: np.ndarray
    excluded: np.ndarray
    report: FlatSpecReport


def _power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` -- the power (energy) mean the plan's
    combiner and this evaluator both use, NOT a linear average of dB
    values. This exact linear-dB-mean-vs-power-mean confusion has shipped
    wrong at least three times in this repo, so this helper is the single
    place the conversion happens; :func:`evaluate_flat_spec` never inlines
    it.
    """
    linear = np.power(10.0, values_db / 10.0)
    return float(10.0 * np.log10(np.mean(linear)))


def _graded_lo_hz(f_lo_hz: float, trusted_floor_hz: float | None) -> float:
    """``max(f_lo_hz, trusted_floor_hz)`` -- one band edge, intersected with
    the session's trusted floor (issue #2551).

    The single place the intersection happens, so the bands and the
    reference band cannot drift apart on it. A ``None`` or non-finite floor
    clamps nothing and returns ``f_lo_hz`` unchanged; the finiteness guard
    is load-bearing rather than defensive, because :func:`max` with a NaN is
    order-dependent and would silently return whichever argument came
    first.
    """
    if trusted_floor_hz is None or not math.isfinite(trusted_floor_hz):
        return float(f_lo_hz)
    return max(float(f_lo_hz), float(trusted_floor_hz))


def _graded_hi_hz(f_hi_hz: float, trusted_ceiling_hz: float | None) -> float:
    """One band's upper edge, intersected with the session's trusted ceiling.

    The mirror of :func:`_graded_lo_hz`, with the one asymmetry that is the
    whole point of it: the floor only ever RAISES an edge, while the TOP
    band's edge follows the ceiling in both directions. That edge and
    :data:`BEST_EFFORT_ABOVE_HZ` are one number -- where grading stops --
    so a microphone trusted to 20 kHz scores to 20 kHz and one trusted to
    12 kHz scores only to 12 kHz. Every lower band's edge is only ever
    lowered; nothing here widens 250-2000 Hz.

    The finiteness guard is load-bearing for the same reason
    :func:`_graded_lo_hz`'s is: :func:`min` with a NaN is order-dependent.
    """
    if trusted_ceiling_hz is None or not math.isfinite(trusted_ceiling_hz):
        return float(f_hi_hz)
    if f_hi_hz >= BEST_EFFORT_ABOVE_HZ:
        return float(trusted_ceiling_hz)
    return min(float(f_hi_hz), float(trusted_ceiling_hz))


def evaluate_flat_spec(
    freqs_hz: np.ndarray,
    spec_smoothed_db: np.ndarray,
    exclusion_mask: np.ndarray | None = None,
    *,
    smoothing_fraction: int = 3,
    trusted_floor_hz: float | None = None,
    trusted_ceiling_hz: float | None = None,
) -> FlatSpecReport:
    """Evaluate the flat-linearization spec against one combined, 1/3-oct-
    smoothed magnitude curve (docs/historical/linearization-campaign-2026-07.md, "The spec --
    what 'flat' means here").

    Args:
        freqs_hz: 1-D **strictly ascending** frequency axis, Hz. Required,
            not assumed: band membership is masked by value (so a shuffled
            axis would still "work"), but the merged exclusion intervals
            use index adjacency as a proxy for frequency adjacency, which
            is only true on a sorted axis -- and a caller handing over a
            descending or duplicated axis has a bug worth hearing about
            rather than a plausible-looking report.
        spec_smoothed_db: 1-D magnitude curve, dB, same length as
            ``freqs_hz`` -- the spatially-combined, 1/3-oct-smoothed curve
            the plan's Instrument stage (S1) produces. This module does
            not smooth or combine; it consumes the result verbatim.
        exclusion_mask: optional 1-D bool array, same length as
            ``freqs_hz``. ``True`` marks an interference-flagged bin (the
            plan's cepstral power-vs-median disagreement screen) --
            excluded from the reference-level computation AND from every
            band's deviation metrics. ``None`` (the default) excludes
            nothing.
        smoothing_fraction: the 1/N-octave fraction the caller attests
            ``spec_smoothed_db`` was smoothed at, recorded verbatim on the
            report. Provenance only: an array cannot prove how it was
            smoothed, this module does not smooth, and nothing here
            validates or uses the value.
        trusted_floor_hz: the session's gate-derived trusted floor in Hz --
            ``2.5/T`` for the capture's own reflection-free window
            (:func:`jasper.audio_measurement.gating.f_trusted_floor_hz`).
            Every band's lower edge, **and the reference band's**, is raised
            to ``max(f_lo, trusted_floor_hz)`` before anything is measured
            (issue #2551), so no graded number rests on a bin the gate
            cannot support. ``None`` (the default) or a non-finite value
            clamps nothing, which is the pre-#2551 behaviour and what a
            caller with no measured floor gets: "unknown" must not silently
            become a floor of zero, and it must not withhold the evidence
            above an unverified edge either. This module takes the number;
            it does not derive, validate, or second-guess it.
        trusted_ceiling_hz: the frequency above which this session's
            microphone is not trusted -- the taper zero of
            :func:`jasper.active_speaker.linearization_envelope.mic_trust_limit`
            for the tier that measured, which is also where the fitter was
            allowed to command and where the delta probe was allowed to
            grade. The TOP band's upper edge, and with it
            :attr:`FlatSpecReport.best_effort_above_hz`, moves to this
            value; every lower band's is lowered to it if it sits above.
            ``None`` or non-finite clamps nothing and grades the nominal
            table, byte-identically to before this argument existed. Same
            take-the-number rule as ``trusted_floor_hz``.

    Reference level: the power mean (:func:`_power_mean_db`) over
    non-excluded bins inside :data:`REFERENCE_BAND_HZ`, its lower edge
    raised to ``trusted_floor_hz`` like every band's. Deliberately spans
    only the low-mid band, so no band above 2 kHz is pooled into the zero
    it is measured from (see that constant) -- and clamped for the same
    reason at the bottom, so an untrustworthy low end cannot re-centre it
    either. :attr:`FlatSpecReport.reference_band_hz` reports the span
    actually pooled.

    **Clamping is not free, and its direction does not generalize.** The
    reference is a power mean, so removing the sub-floor region moves the
    zero that every surviving deviation is stated against, and the headline
    number moves one-for-one with it whenever the worst bin survives. On
    the S0 corpus that shift is +1.0676 dB in the FLATTERING direction
    (``tests/test_flat_spec_ssot.test_the_trusted_floor_clamp_costs_the_low_band``
    pins it); on a speaker whose sub-floor region is quiet it would go the
    other way. None of it is the speaker improving -- it is the same
    speaker graded on fewer bins, which is what
    :attr:`BandResult.n_bins`/:attr:`ConvergenceResidual.n_bins` exist to
    keep visible.

    Deviation: ``spec_smoothed_db - reference_db``, evaluated per
    :data:`SPEC_BANDS` entry over that band's non-excluded bins:
    ``max_deviation_db`` is the signed deviation at the largest-absolute
    bin (with ``max_deviation_hz`` naming that bin), ``rms_deviation_db`` is
    the RMS deviation, and ``passed`` is
    ``abs(max_deviation_db) <= tolerance_db``.

    Each band additionally carries the **attribution split** (issue #1857):
    ``level_deviation_db`` (where the whole band sits relative to the shared
    frame) and ``max_ripple_db``/``max_ripple_hz`` (what the curve does
    relative to *that band's own* level, which no reference choice can
    move). Disclosure only -- see :class:`BandResult` for the per-bin
    identity and :func:`spec_band_tilt` for the frame-free reading built on
    them. **No verdict reads them**: ``passed`` and ``overall_passed`` are
    computed exactly as they were before the split existed, pinned against a
    frozen pre-change corpus by ``tests/test_flat_spec_attribution.py``.

    A band with **zero non-excluded bins** -- no coverage on the axis, every
    bin interference-flagged, or nothing left above ``trusted_floor_hz`` --
    is reported as ``evaluable=False`` with ``passed=None`` and ``None``
    metrics, not raised on: one band losing its evidence must not destroy
    the report for the other two, and a band entirely outside the session's
    trusted range has no evidence rather than a failure.
    ``graded_lo_hz >= graded_hi_hz`` is what distinguishes that third case
    from the first two. :attr:`FlatSpecReport.overall_passed` is ``True``
    only when every band is evaluable *and* passed, so an unevaluable band
    cannot be mistaken for a clean one. The **reference band** is different
    and still raises: with no reference level there is nothing to compute a
    deviation against anywhere, so no band could be evaluated at all. A
    ``trusted_floor_hz`` at or above the reference band's top edge is
    therefore a raise, not a report -- it says the whole spec is ungradeable
    on this capture, which is the honest answer and is what the wiring
    layer's own fail-soft turns into an "unavailable" cloud block.

    **A consequence of the reference band being SPEC_BANDS[0] exactly: a
    FLOOR can no longer leave a band unevaluable.** One high enough to empty
    the low band empties the frame with it and raises above instead. The
    ceiling is the clamp that reaches that state now, at the top band. The
    rule is unchanged; which clamp can produce it is not.

    Band membership is ``graded_lo <= f < graded_hi`` -- inclusive-lower,
    exclusive-upper -- for both :data:`SPEC_BANDS` entries and
    :data:`REFERENCE_BAND_HZ` (the same rule, applied uniformly, is what
    keeps the reference band's span exactly equal to ``SPEC_BANDS[0]`` with
    no gap or overlap at the 2 kHz seam, and keeps the best-effort boundary
    exactly adjacent to ``SPEC_BANDS[-1]``'s exclusive upper edge, at
    whatever value the ceiling put them). A bin at exactly 2000 Hz
    therefore lands in the 2-8 kHz band, not 250 Hz-2 kHz; a bin at exactly
    8000 Hz lands in the top band; a bin at exactly
    :attr:`FlatSpecReport.best_effort_above_hz` is best-effort, not the top
    of it.

    Bins at or above :attr:`FlatSpecReport.best_effort_above_hz` are never
    evaluated and never fail -- they simply do not appear in any
    :class:`BandResult`.
    The plan calls this region "best-effort, disclosed, never specced,"
    which this module satisfies by omission (plus naming where the region
    starts via :attr:`FlatSpecReport.best_effort_above_hz`), not by
    computing anything for it.

    Raises:
        ValueError: for any degenerate input -- empty or non-1-D arrays,
            mismatched array lengths, a ``freqs_hz`` that is not strictly
            ascending, :data:`REFERENCE_BAND_HZ` left with zero
            non-excluded bins (no reference level is computable), or any
            non-finite (NaN/Inf) value in ``freqs_hz`` or
            ``spec_smoothed_db``.
    """
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    spec_smoothed_db = np.asarray(spec_smoothed_db, dtype=np.float64)

    if freqs_hz.ndim != 1 or spec_smoothed_db.ndim != 1:
        raise ValueError(
            "freqs_hz and spec_smoothed_db must be 1-D arrays "
            f"(got ndim={freqs_hz.ndim} and ndim={spec_smoothed_db.ndim})"
        )
    if freqs_hz.size == 0 or spec_smoothed_db.size == 0:
        raise ValueError("freqs_hz and spec_smoothed_db must not be empty")
    if freqs_hz.shape != spec_smoothed_db.shape:
        raise ValueError(
            f"freqs_hz shape {freqs_hz.shape} does not match "
            f"spec_smoothed_db shape {spec_smoothed_db.shape}"
        )

    if exclusion_mask is None:
        resolved_exclusion_mask = np.zeros_like(freqs_hz, dtype=bool)
    else:
        resolved_exclusion_mask = np.asarray(exclusion_mask, dtype=bool)
        if resolved_exclusion_mask.shape != freqs_hz.shape:
            raise ValueError(
                f"exclusion_mask shape {resolved_exclusion_mask.shape} does not "
                f"match freqs_hz shape {freqs_hz.shape}"
            )

    if not np.all(np.isfinite(freqs_hz)) or not np.all(np.isfinite(spec_smoothed_db)):
        raise ValueError(
            "freqs_hz and spec_smoothed_db must contain only finite values "
            "(found NaN or Inf)"
        )

    # Checked after finiteness, so a NaN axis is reported as non-finite
    # rather than as a spurious ordering failure (NaN comparisons are all
    # False, so np.diff would not catch it).
    if np.any(np.diff(freqs_hz) <= 0.0):
        raise ValueError(
            "freqs_hz must be strictly increasing (the merged exclusion "
            "intervals treat index adjacency as frequency adjacency)"
        )

    included_mask = ~resolved_exclusion_mask

    # #2551: the trusted-floor intersection, applied to the reference band
    # first because every band's deviation is stated against it.
    nominal_ref_lo_hz, nominal_ref_hi_hz = REFERENCE_BAND_HZ
    ref_lo_hz = _graded_lo_hz(nominal_ref_lo_hz, trusted_floor_hz)
    ref_hi_hz = _graded_hi_hz(nominal_ref_hi_hz, trusted_ceiling_hz)
    ref_band_mask = (freqs_hz >= ref_lo_hz) & (freqs_hz < ref_hi_hz) & included_mask
    if not ref_band_mask.any():
        raise ValueError(
            f"reference band {ref_lo_hz}-{ref_hi_hz} Hz has zero non-excluded "
            "bins; cannot compute reference level"
        )
    reference_db = _power_mean_db(spec_smoothed_db[ref_band_mask])

    deviation_db = spec_smoothed_db - reference_db

    band_results: list[BandResult] = []
    for nominal_lo_hz, nominal_hi_hz, tolerance_db in SPEC_BANDS:
        f_lo_hz = _graded_lo_hz(nominal_lo_hz, trusted_floor_hz)
        f_hi_hz = _graded_hi_hz(nominal_hi_hz, trusted_ceiling_hz)
        # The clamped edge is what defines membership, so a bin below the
        # trusted floor is not in the band at all rather than in it and
        # excluded: `n_excluded` stays the interference screen's own count,
        # and `graded_lo_hz` below carries the clamp instead.
        band_mask = (freqs_hz >= f_lo_hz) & (freqs_hz < f_hi_hz)
        included_band_mask = band_mask & included_mask
        n_bins = int(band_mask.sum())
        n_excluded = int((band_mask & resolved_exclusion_mask).sum())
        if not included_band_mask.any():
            band_results.append(
                BandResult(
                    f_lo_hz=float(nominal_lo_hz),
                    f_hi_hz=float(nominal_hi_hz),
                    tolerance_db=float(tolerance_db),
                    max_deviation_db=None,
                    max_deviation_hz=None,
                    rms_deviation_db=None,
                    n_bins=n_bins,
                    n_excluded=n_excluded,
                    evaluable=False,
                    passed=None,
                    graded_lo_hz=f_lo_hz,
                    graded_hi_hz=f_hi_hz,
                )
            )
            continue
        band_indices = np.flatnonzero(included_band_mask)
        band_deviation_db = deviation_db[band_indices]
        worst = int(band_indices[np.argmax(np.abs(band_deviation_db))])
        max_deviation_db = float(deviation_db[worst])
        # Did the trusted floor cut this band, and did the extremum land on
        # the cut edge? Both halves are needed: an untruncated band's first
        # bin IS the band's own start, with nothing ungraded below it to
        # warn about, so `worst == band_indices[0]` alone would fire on
        # every band whose worst bin happens to be its lowest. The
        # comparison is against the LOWEST INCLUDED bin, not `f_lo_hz`:
        # exclusion can take the graded edge itself, and what matters is
        # where the evidence actually starts.
        max_at_graded_edge = bool(
            f_lo_hz > nominal_lo_hz and worst == int(band_indices[0])
        )
        rms_deviation_db = float(np.sqrt(np.mean(np.square(band_deviation_db))))
        # The #1857 attribution split -- the band's own level, and what the
        # curve does relative to THAT rather than to the pooled frame. Same
        # `_power_mean_db` as the reference, so there is one averaging
        # convention in this module, not two. Nothing below feeds `passed`.
        band_level_db = _power_mean_db(spec_smoothed_db[band_indices])
        band_ripple_db = spec_smoothed_db[band_indices] - band_level_db
        worst_ripple = int(band_indices[np.argmax(np.abs(band_ripple_db))])
        band_results.append(
            BandResult(
                f_lo_hz=float(nominal_lo_hz),
                f_hi_hz=float(nominal_hi_hz),
                tolerance_db=float(tolerance_db),
                max_deviation_db=max_deviation_db,
                max_deviation_hz=float(freqs_hz[worst]),
                rms_deviation_db=rms_deviation_db,
                n_bins=n_bins,
                n_excluded=n_excluded,
                evaluable=True,
                passed=bool(abs(max_deviation_db) <= tolerance_db),
                level_deviation_db=float(band_level_db - reference_db),
                max_ripple_db=float(spec_smoothed_db[worst_ripple] - band_level_db),
                max_ripple_hz=float(freqs_hz[worst_ripple]),
                graded_lo_hz=f_lo_hz,
                graded_hi_hz=f_hi_hz,
                max_at_graded_edge=max_at_graded_edge,
            )
        )

    overall_passed = all(band.evaluable and band.passed for band in band_results)
    excluded_intervals = merged_true_intervals(freqs_hz, resolved_exclusion_mask)
    # Where grading stops, and SPEC_BANDS[-1]'s own upper edge, are one number
    # by construction — so this is that edge, not a second reading of it.
    graded_top_hz = _graded_hi_hz(BEST_EFFORT_ABOVE_HZ, trusted_ceiling_hz)

    return FlatSpecReport(
        reference_db=reference_db,
        bands=tuple(band_results),
        overall_passed=overall_passed,
        excluded_intervals=excluded_intervals,
        best_effort_above_hz=graded_top_hz,
        smoothing_fraction=int(smoothing_fraction),
        trusted_floor_hz=(
            float(trusted_floor_hz)
            if trusted_floor_hz is not None and math.isfinite(trusted_floor_hz)
            else None
        ),
        reference_band_hz=(ref_lo_hz, ref_hi_hz),
        trusted_ceiling_hz=(
            float(trusted_ceiling_hz)
            if trusted_ceiling_hz is not None and math.isfinite(trusted_ceiling_hz)
            else None
        ),
        graded_band_hz=(
            _graded_lo_hz(SPEC_BANDS[0][0], trusted_floor_hz), graded_top_hz,
        ),
    )


@dataclass(frozen=True)
class ConvergenceResidual:
    """The S3 closed loop's residual metric for one evaluation.

    One number plus the two counts that make it interpretable. See
    :func:`spec_convergence_residual` for the definition and for why the
    counts ride along.

    Args:
      rms_db: RMS deviation over every non-excluded bin of every
        :data:`SPEC_BANDS` band, as one pooled figure. ``None`` when no
        band was evaluable.
      n_bins: how many bins that RMS was computed from — the pooled
        non-excluded spec-band bin count.
      n_excluded: how many spec-band bins were dropped by the exclusion
        mask. Counted across ALL bands, including any band the exclusion
        left unevaluable.
      evaluable: ``n_bins > 0``. False means there is no residual, not a
        residual of zero.
    """

    rms_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
        }


def spec_convergence_residual(report: FlatSpecReport) -> ConvergenceResidual:
    """The residual the flat-linearization plan's S3 closed loop converges
    on: **RMS deviation over the non-excluded bins of the spec bands**,
    pooled across all three.

    An instrument landing ahead of its consumer. S3's loop policy — how
    much improvement counts, how many iterations, when to stop — is not
    here and must not be: this function holds no threshold and makes no
    verdict. It reports one measurement.

    **Derived from the report, not recomputed from the curve.** Every
    question about *which bins count* — band membership and its edge rule,
    what the exclusion mask removed, which reference level the deviation is
    measured against — is already answered, once, by
    :func:`evaluate_flat_spec`. Re-deriving any of it here would create a
    second owner of the same decision and a second thing to drift, so the
    pooled figure is reassembled from each band's own
    :attr:`BandResult.rms_deviation_db` and its included-bin count:

        rms = sqrt( sum_b n_b * rms_b**2 / sum_b n_b ),
        n_b = band.n_bins - band.n_excluded

    which is exactly the RMS over the union of those bins, because
    ``n_b * rms_b**2`` is that band's sum of squared deviations. Pinned
    against a direct from-the-arrays recomputation by a test.

    Pooled rather than per-band because the loop needs one scalar to
    converge on, and because per-band figures are already on the report for
    a consumer that wants to see *where* the residual sits. Bins at or above
    :attr:`FlatSpecReport.best_effort_above_hz` never enter it — the plan
    never specs them, so a top octave the speaker cannot reach must not be
    able to stall the loop. That edge follows the session's trusted ceiling,
    so a 20 kHz-trusted evaluation pools the 16-20 kHz bins this pooled
    residual used to omit; reading the module constant here instead would
    name a boundary the numbers were not taken at.

    **Why the counts are part of the answer.** A residual that fell because
    the honesty mask grew is not convergence — it is the same speaker,
    graded on fewer bins. ``n_bins`` and ``n_excluded`` make that visible in
    the same record as the number, so a loop (or a reader) comparing two
    iterations can tell an improvement from a smaller denominator. Nothing
    here enforces that reading; it just refuses to hide it.

    Returns:
      A :class:`ConvergenceResidual`. When no band is evaluable,
      ``rms_db`` is ``None`` and ``evaluable`` is ``False``, mirroring
      :class:`BandResult`'s "unevaluable is a first-class outcome, not a
      fabricated verdict" rule rather than reporting a residual of 0.0 for
      a spectrum nothing was measured in.

      **That state is unreachable from :func:`evaluate_flat_spec` today**,
      and the guard is deliberate anyway. :data:`REFERENCE_BAND_HZ` is
      exactly ``SPEC_BANDS[0]``, so an evaluation that did not raise on an
      empty reference band necessarily left at least one non-excluded
      spec-band bin behind -- the reference band IS band 0, so a non-empty
      one is a non-empty band. That is the whole argument, and it holds
      whatever either clamp does: the ceiling does NOT move the two edges
      together (it RAISES the top band's to 20 kHz on a reference mic while
      the reference band's stays at 2 kHz), so an argument resting on them
      moving together would be false. A report
      reaching this function from anywhere else — hand-built, or rehydrated
      from the persistence the plan's PR-6b adds — carries no such
      guarantee, and the alternative there is a ZeroDivisionError.
    """
    n_excluded = sum(band.n_excluded for band in report.bands)
    # One pass, so the denominator and the numerator can never be assembled
    # from different band sets. On a report from `evaluate_flat_spec` the
    # `rms_deviation_db is None` filter and a zero included-bin count are the
    # same condition; on a hand-built one they need not be, and `n_bins` must
    # keep meaning "bins this RMS was computed from".
    measured = [
        (band.n_bins - band.n_excluded, band.rms_deviation_db)
        for band in report.bands
        if band.rms_deviation_db is not None
    ]
    n_bins = sum(count for count, _rms_db in measured)
    if n_bins <= 0:
        return ConvergenceResidual(
            rms_db=None, n_bins=0, n_excluded=n_excluded, evaluable=False,
        )
    sum_squares = sum(count * rms_db ** 2 for count, rms_db in measured)
    return ConvergenceResidual(
        rms_db=float(np.sqrt(sum_squares / n_bins)),
        n_bins=n_bins,
        n_excluded=n_excluded,
        evaluable=True,
    )


@dataclass(frozen=True)
class BandTilt:
    """How far the graded bands' own levels sit from EACH OTHER (issue #1857).

    The one figure on this report that **no reference-frame choice can
    move**, and that is the whole point of it.

    :attr:`FlatSpecReport.reference_db` is a power mean pooled over
    :data:`REFERENCE_BAND_HZ`, so a band INSIDE that span that is uniformly
    off drags the shared zero toward itself and inflates *every other*
    band's deviation. With the frame at its original 250 Hz-8 kHz that was
    not a rounding concern: on the 2026-07-29 corpus session a tweeter
    sitting ~5 dB dark across its own passband pulled the frame ~3 dB down,
    a woofer flat to +/-0.1 dB read "+4.84 dB @ 1339.6 Hz", the gauge named
    the woofer as the worst band, and a household acting on it would have
    EQ'd the wrong driver. The low-mid frame (See ADR-0194) takes the two
    upper bands out of the pool and so out of that failure mode; a defect
    inside 250 Hz-2 kHz still drags its own frame, which is why this reading
    is still the one to trust when the two disagree.

    A **step between two band levels** cannot suffer that, by construction:
    each level is stated as :attr:`BandResult.level_deviation_db`, so the
    shared reference appears in both terms and cancels in the subtraction.
    Re-anchor the spec on the woofer passband, on the full range, or on
    anything else, and ``step_db`` does not move -- which is precisely what
    let it ship while WHICH anchor the spec should use was still open
    (#1857's Q-E, ``docs/historical/attribution-stage-plan.md`` section 9;
    :data:`REFERENCE_BAND_HZ` records how it was decided). This class does
    not pick a side; it states the relationship both sides agree on.

    The cancellation is exact in arithmetic and not quite exact in floating
    point -- ``(L_a - ref) - (L_b - ref)`` rounds differently from
    ``L_a - L_b``. Measured, not assumed: across the 15-shape corpus in
    ``tests/test_flat_spec_attribution.py`` and five candidate reference
    bands (250-2000, 250-8000, 2000-8000, 250-16000, 300-6000 Hz) the worst
    spread in ``step_db`` is **8.882e-16 dB**, two ULPs at a 5 dB step.
    :attr:`BandResult.max_ripple_db` is the stronger case and is
    bit-identical across all five, because it never touches the reference
    at all.

    **It is not a verdict and holds no threshold.** Nothing here is compared
    against a tolerance, nothing here feeds :attr:`BandResult.passed` or
    :attr:`FlatSpecReport.overall_passed`, and adding it moved no graded
    number (pinned by ``tests/test_flat_spec_attribution.py``'s frozen
    pre-change corpus). It answers "which bands disagree, and by how much",
    which is the question a household reading a worst-band pointer was
    already trying to answer.

    Args:
      step_db: the largest absolute level difference between any two
        evaluable bands, as a **non-negative magnitude** -- the direction is
        carried by the two band fields rather than by a sign, because
        "4.98 dB" plus "which one is higher" is unambiguous where a signed
        step needs a convention the reader has to remember. ``None`` when
        fewer than two bands carry a level.
      high_band_hz: ``(f_lo_hz, f_hi_hz)`` of the higher-sitting band of
        that pair. ``None`` when unevaluable.
      low_band_hz: ``(f_lo_hz, f_hi_hz)`` of the lower-sitting one. ``None``
        when unevaluable. When ``step_db`` is exactly ``0.0`` the two bands
        are level and the high/low labels carry no information -- read the
        number first.
      n_bands: how many bands carried a level to compare. With three graded
        bands this is normally 3; an unevaluable band, or a report from
        before the split existed, lowers it. Rides along for the same reason
        :class:`ConvergenceResidual` carries its counts: a step chosen among
        two bands is a weaker statement than one chosen among three, and
        that must be visible in the same record as the number.
      evaluable: ``n_bands >= 2``. ``False`` means there is no step, not a
        step of zero -- one band cannot tilt against itself.
    """

    step_db: float | None
    high_band_hz: tuple[float, float] | None
    low_band_hz: tuple[float, float] | None
    n_bands: int
    evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_db": self.step_db,
            "high_band_hz": (
                list(self.high_band_hz) if self.high_band_hz is not None else None
            ),
            "low_band_hz": (
                list(self.low_band_hz) if self.low_band_hz is not None else None
            ),
            "n_bands": self.n_bands,
            "evaluable": self.evaluable,
        }


#: The "no step is knowable" :class:`BandTilt` -- a null object, not a
#: fabricated reading. Only :class:`SpecFlatness` uses it, as the default for
#: a gauge built by hand or rehydrated from before the tilt existed;
#: :func:`spec_band_tilt` builds its own unevaluable result with the real
#: ``n_bands`` it counted.
NO_BAND_TILT = BandTilt(
    step_db=None, high_band_hz=None, low_band_hz=None, n_bands=0, evaluable=False,
)


def spec_band_tilt(report: FlatSpecReport) -> BandTilt:
    """The largest level step between two of ``report``'s graded bands --
    issue #1857's frame-free attribution reading.

    **Derived from the report, not recomputed from the curve**, the same
    rule and the same reason as :func:`spec_convergence_residual` and
    :func:`spec_flatness_gauge`: band membership, the exclusion mask's
    effect, and each band's own level are answered exactly once, by
    :func:`evaluate_flat_spec`.

    The pair is chosen by largest absolute difference of
    :attr:`BandResult.level_deviation_db`; ties go to the lowest pair in
    :data:`SPEC_BANDS` order, matching :func:`spec_flatness_gauge`'s own tie
    rule so the two reductions never disagree about which band came first.
    (:func:`max` returns the FIRST maximal element, and the pairs are built
    in band order, so that rule is the builtin's own — not a comparison
    written here that could drift from the gauge's.)

    Bands that are unevaluable, or that carry no ``level_deviation_db`` (a
    hand-built or older-persistence report -- see :class:`BandResult`), are
    skipped rather than defaulted to zero: a band with no measured level has
    no level, and inventing 0 dB for it would manufacture the exact kind of
    false step this function exists to expose.
    """
    levelled = [
        (band, band.level_deviation_db)
        for band in report.bands
        if band.evaluable and band.level_deviation_db is not None
    ]
    if len(levelled) < 2:
        return BandTilt(
            step_db=None,
            high_band_hz=None,
            low_band_hz=None,
            n_bands=len(levelled),
            evaluable=False,
        )
    pairs: list[tuple[float, BandResult, BandResult]] = []
    for index, (band_a, level_a) in enumerate(levelled):
        for band_b, level_b in levelled[index + 1:]:
            high, low = (band_a, band_b) if level_a >= level_b else (band_b, band_a)
            pairs.append((abs(level_a - level_b), high, low))
    # `len(levelled) >= 2` above guarantees at least one pair, so this cannot
    # be an empty max() -- and building the list means no sentinel to seed and
    # no unreachable None branch for a reader to reason about.
    step_db, high_band, low_band = max(pairs, key=lambda pair: pair[0])
    return BandTilt(
        step_db=step_db,
        high_band_hz=(high_band.f_lo_hz, high_band.f_hi_hz),
        low_band_hz=(low_band.f_lo_hz, low_band.f_hi_hz),
        n_bands=len(levelled),
        evaluable=True,
    )


@dataclass(frozen=True)
class SpecFlatness:
    """The household-facing "how flat is the speaker" figures for one report.

    The flat-linearization plan's PR-5 (the spec-curve SSOT) makes
    :func:`evaluate_flat_spec`'s report the ONE construction every
    spec-facing surface reads. This is the reduction those surfaces
    actually render: the worst deviation and where it sits, the band's own
    tolerance, the pooled average error, and how much of the spectrum the
    number was computed from.

    Every field is **lifted from the report**, never recomputed from a
    curve: ``max_*``/``tolerance_db`` are one :class:`BandResult`'s own
    values verbatim, and ``rms_db``/``n_bins``/``n_excluded`` come from
    :func:`spec_convergence_residual` (itself derived from the report). So
    a gauge, a ledger line, and the report shown for one session are the
    same numbers by construction, not by two code paths agreeing — the
    MEASURE-vs-VERIFY frame-discrepancy class the plan's "S0 executed"
    § c documents.

    Args:
      max_db: the **signed** deviation at the worst bin of the worst
        evaluable band — the same signed convention (and the same number)
        as :attr:`BandResult.max_deviation_db`, because "2.4 dB too loud"
        and "2.4 dB too quiet" call for opposite corrections. ``None``
        when no band was evaluable.
      max_hz: that bin's frequency. ``None`` when unevaluable.
      max_band_hz: ``(f_lo_hz, f_hi_hz)`` of the band that worst bin lives
        in — which tolerance row the number is being judged against.
        ``None`` when unevaluable. **Not the frame the deviation is measured
        FROM** — that is ``reference_band_hz``, and conflating the two is
        the mistake issue #1857 was filed about. **Nor is it "the band to
        fix"**: this band is the one furthest from the shared frame, and a
        band that is uniformly off drags that frame toward itself, so the
        band NAMED here can be a flat one made to look proud by a different
        band's deficit. ``tilt`` below is the reading that cannot do that,
        and a surface rendering this pointer should render that one beside
        it.
      reference_band_hz: :attr:`FlatSpecReport.reference_band_hz` — the span
        whose power mean over non-excluded bins IS the zero that
        every ``max_db`` here is stated against (:func:`evaluate_flat_spec`
        computes it once as ``reference_db``; this names the span it was
        pooled over). That is :data:`REFERENCE_BAND_HZ` with its lower edge
        raised to the session's trusted floor (issue #2551), so on a clamped
        evaluation this reads the CLAMPED span and not the module constant
        — the frame moved, and printing the constant would misstate it.
        Carried because the frame is not a detail of the
        number, it is half of it: on the 2026-07-30 corpus a dark tweeter
        pulled the then-full-range mean ~2.7 dB below a woofer-anchored one,
        and the same persisted curve's worst-band pointer moves +5.44 dB @
        428 Hz → −5.86 dB @ 1901 Hz between the two frames — a sign flip
        and a different driver blamed. (That corpus is why the frame is now
        the low-mid band; it is not why the field travels.) A surface that prints a worst band
        without naming its frame is stating half a measurement. Always
        populated, including when ``evaluable`` is ``False``: which frame
        WOULD have been used is knowable even when no band could be graded,
        and a reader comparing two sessions needs it either way.
      tolerance_db: that band's tolerance. ``None`` when unevaluable.
      max_band_level_deviation_db: that same band's
        :attr:`BandResult.level_deviation_db` — how much of ``max_db`` is
        just "where this whole band sits relative to the pooled frame"
        rather than anything happening at ``max_hz``. ``None`` when
        unevaluable, or when the report predates the split.
      max_band_ripple_db: that same band's
        :attr:`BandResult.max_ripple_db` — the worst the curve gets
        *inside* the band, measured from the band's own level, which no
        reference choice can move. The two together are what disarm the
        pointer at the point of use: on the #1857 shape under the original
        250 Hz-8 kHz frame ``max_db`` read ``+3.36 dB @ 703 Hz`` while these
        read ``+3.26 dB`` and ``−0.10 dB`` — i.e. the band was flat to a
        tenth of a dB and merely sat high, so there was no 703 Hz peak to
        EQ. The low-mid frame now charges that shape to the dark band
        instead, and this pair is what would say so again on a shape the new
        frame can still mis-point. ``None`` when unevaluable, or when the
        report predates the split.
      rms_db: :attr:`ConvergenceResidual.rms_db` — RMS deviation pooled
        over every non-excluded spec-band bin. ``None`` when unevaluable.
      n_bins: how many bins the RMS was computed from.
      n_excluded: how many spec-band bins the exclusion mask removed. With
        ``n_bins`` this is the "graded on how much of the spectrum" pair
        :class:`ConvergenceResidual` exists to keep visible: a deviation
        that fell because the mask grew is the same speaker on fewer bins,
        not an improvement. Deliberately a BIN count, not a count of
        :attr:`FlatSpecReport.excluded_intervals`: that field spans the
        whole axis including regions no spec band covers, so quoting it as
        "regions excluded from grading" would count a sub-250 Hz interval
        that was never graded in the first place.
      evaluable: whether ANY band survived to be measured. ``False`` means
        there is no flatness number, not a flatness of zero.
      passed: :attr:`FlatSpecReport.overall_passed`, verbatim. **Read it
        with** ``evaluable``: that field is ``False`` for an unmeasurable
        spectrum too (by its own "will not report a clean bill of health
        for a spectrum it could not fully measure" rule), so
        ``passed=False, evaluable=False`` means "could not be measured",
        not "failed".
      tilt: :func:`spec_band_tilt` of the same report — the largest level
        step between two graded bands, and which of them sits higher. The
        ONE figure here that no reference-frame choice can move, and the
        answer to the question ``max_db``/``max_band_hz`` cannot answer:
        *which* bands disagree. A pointer alone made a household read a
        flat woofer as the defect (#1857); this is what stops that, and a
        surface rendering the pointer should render this beside it.
        Defaults to :data:`NO_BAND_TILT` (unevaluable — no step known, not
        a step of zero) for a gauge built by hand or rehydrated from
        persistence written before the tilt existed.
    """

    max_db: float | None
    max_hz: float | None
    max_band_hz: tuple[float, float] | None
    tolerance_db: float | None
    rms_db: float | None
    n_bins: int
    n_excluded: int
    evaluable: bool
    passed: bool
    reference_band_hz: tuple[float, float] = REFERENCE_BAND_HZ
    tilt: BandTilt = NO_BAND_TILT
    max_band_level_deviation_db: float | None = None
    max_band_ripple_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_db": self.max_db,
            "max_hz": self.max_hz,
            "max_band_hz": (
                list(self.max_band_hz) if self.max_band_hz is not None else None
            ),
            # The frame every ``max_db``/``rms_db`` above is stated against
            # (issue #1857) — see the field's docstring for why it travels
            # beside the pointer rather than being left implicit.
            "reference_band_hz": list(self.reference_band_hz),
            "tolerance_db": self.tolerance_db,
            # The pointed-at band's own attribution split (issue #1857) —
            # how much of ``max_db`` is the band's level rather than
            # anything at ``max_hz``. See the fields' docstrings.
            "max_band_level_deviation_db": self.max_band_level_deviation_db,
            "max_band_ripple_db": self.max_band_ripple_db,
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
            "n_excluded": self.n_excluded,
            "evaluable": self.evaluable,
            "passed": self.passed,
            # The frame-free half of the same disclosure (issue #1857): the
            # pointer above says how far the worst band sits from a pooled
            # zero; this says how far the bands sit from each other, which
            # no anchor choice can move.
            "tilt": self.tilt.to_dict(),
        }


def spec_flatness_gauge(report: FlatSpecReport) -> SpecFlatness:
    """Reduce one :class:`FlatSpecReport` to the figures a household-facing
    flatness gauge renders (plan PR-5).

    **Derived from the report, not recomputed from the curve** — the same
    rule, and the same reason, as :func:`spec_convergence_residual`: band
    membership, the exclusion mask's effect, and the reference level are
    each answered exactly once, by :func:`evaluate_flat_spec`. A gauge that
    re-derived any of them would be a second owner of the same decision,
    which is the very failure mode this function exists to remove.

    The worst band is the one whose :attr:`BandResult.max_deviation_db` has
    the largest **absolute** value among evaluable bands; ties go to the
    lowest band, so the choice is deterministic rather than dict-order
    dependent. Deliberately NOT "the band that failed by the widest margin
    relative to its own tolerance": the rendered claim is "this is how far
    from flat the speaker measured", a dB reading, and re-ranking bands by
    tolerance headroom would silently answer a different question.

    **Which band that picks is frame-dependent, and deliberately left so.**
    The reference is a power mean over :data:`REFERENCE_BAND_HZ`, so a
    uniformly-off band inside that span drags it and this walk can name a
    flat band as the worst one — issue #1857, reproduced. Narrowing the
    frame to the low-mid band shrank which bands can do that; it did not
    abolish the effect, and re-ranking here would not either — it would
    only move the anchor question somewhere less visible. What this
    function does instead is carry :func:`spec_band_tilt` beside the
    pointer, so the frame-free reading travels with the frame-dependent one
    and a reader is never handed the pointer alone.
    """
    residual = spec_convergence_residual(report)
    tilt = spec_band_tilt(report)
    worst: BandResult | None = None
    worst_magnitude_db = -1.0
    for band in report.bands:
        if not band.evaluable or band.max_deviation_db is None:
            continue
        magnitude_db = abs(band.max_deviation_db)
        # Strict `>` is what makes ties go to the LOWEST band: SPEC_BANDS is
        # ordered low-to-high and this walks it in order.
        if magnitude_db > worst_magnitude_db:
            worst, worst_magnitude_db = band, magnitude_db
    if worst is None:
        return SpecFlatness(
            max_db=None,
            max_hz=None,
            max_band_hz=None,
            tolerance_db=None,
            rms_db=residual.rms_db,
            n_bins=residual.n_bins,
            n_excluded=residual.n_excluded,
            evaluable=False,
            passed=report.overall_passed,
            reference_band_hz=report.reference_band_hz,
            tilt=tilt,
        )
    return SpecFlatness(
        max_db=worst.max_deviation_db,
        max_hz=worst.max_deviation_hz,
        max_band_hz=(worst.f_lo_hz, worst.f_hi_hz),
        tolerance_db=worst.tolerance_db,
        rms_db=residual.rms_db,
        n_bins=residual.n_bins,
        n_excluded=residual.n_excluded,
        evaluable=True,
        passed=report.overall_passed,
        # #2551: the frame that was USED, read off the report, not the module
        # constant. A clamped reference band re-centres every number above,
        # so a surface printing the pointer beside `REFERENCE_BAND_HZ` would
        # be naming a frame the numbers were never stated against.
        reference_band_hz=report.reference_band_hz,
        tilt=tilt,
        max_band_level_deviation_db=worst.level_deviation_db,
        max_band_ripple_db=worst.max_ripple_db,
    )
