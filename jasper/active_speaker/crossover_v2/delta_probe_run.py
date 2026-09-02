# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Running the delta probe on one round: its axes, its terms, its journal.

:mod:`jasper.active_speaker.delta_probe` classifies; this module is what a
commissioning round hands it. It assembles the CHANGE and STATE axes and the
four optional accounting terms (the apply's declared level move, the graph's
declared transfer on the probe's grid, the entry-capture anchor and the
spatial cost), chooses which axis to grade on, and writes the journal line the
verdict is read off.

Every session value arrives as an argument, and the verdict is returned rather
than stamped: the session owns its own state, and this module owns none.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

import numpy as np

from jasper.active_speaker.crossover_v2.contracts import (
    REFERENCE_MARK_DESIGN_AXIS,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_VERIFY
from jasper.active_speaker.crossover_v2.verification import identity_mismatch
from jasper.active_speaker.delta_probe import (
    VERDICT_FRAME_MISMATCH,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_SAFETY_ONLY,
    DeltaProbeMap,
    classify_delta_probe,
    spatial_cost_from_group_spreads,
)
from jasper.log_event import log_event


def applied_offset_db(seam: Callable[[], float] | None) -> float:
    """The apply's own declared whole-band level move, dB (#1811).

    Read through the optional seam, fail-soft to ``0.0``: an unbound seam,
    an unreadable durable state, or a non-finite value all mean "nothing
    known", and ``classify_delta_probe`` then leaves the entire shift
    visible as ``residual_offset_db`` rather than absorbing it. Claiming an
    offset we cannot read would be the one dishonest option here.
    """
    if seam is None:
        return 0.0
    try:
        value = float(seam())
    except (OSError, RuntimeError, TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def declared_transfer_db(
    logger: logging.Logger,
    freqs: Any,
    declared: Any,
    *,
    session_id: str,
) -> Any | None:
    """The applied graph's own declared transfer, on the VERIFY grid (#2614).

    The delta probe's STATE axis — what the graph on the speaker declares it
    does against the uncorrected crossover — interpolated onto the probe's
    grid exactly as ``commanded_db`` is beside the call, so the two masks the
    probe builds from them are bin-for-bin comparable.

    Returns ``None`` when the axis never crossed into this stage or cannot
    be put on the grid, and says so on the journal rather than degrading
    quietly: the probe then falls back to the CHANGE axis alone for its two
    directional safety rules, which on a repeat round stops watching every
    band the apply left alone. That is a real narrowing of a hearing-safety
    finding, so it belongs in front of whoever reads the round.
    """
    if declared is None:
        log_event(
            logger, "correction.crossover_v2_declared_transfer_unavailable",
            level=logging.WARNING, session_id=session_id,
            reason="no_declared_transfer",
        )
        return None
    try:
        return np.interp(
            np.asarray(freqs, dtype=float),
            np.asarray(declared[0], dtype=float),
            np.asarray(declared[1], dtype=float),
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_declared_transfer_unavailable",
            level=logging.WARNING, session_id=session_id,
            reason="declared_transfer_ungriddable", error=str(exc),
        )
        return None


def entry_delta_db(
    logger: logging.Logger,
    freqs: Any,
    predicted_s: Any,
    commanded_db: Any,
    *,
    baseline: Any | None,
    session_id: str,
    program_for_phase: Callable[[str], Any],
) -> Any | None:
    """This round's PRE-apply capture, in the realized curve's frame (#2533).

    ``measured_pre − predicted_previous``, on the VERIFY grid — the exact
    counterpart of the ``realized_db`` reconstruction beside the call, with
    the entry baseline's magnitude in place of the post-apply one, so
    ``classify_delta_probe`` can measure its residual as a CHANGE across the
    apply instead of as an absolute disagreement with the model. The
    previous-graph prediction is recovered the same way that reconstruction
    recovers it: ``predicted_previous == predicted_post − commanded``.

    Since #2611 that recovered curve is a model of the graph the entry
    capture ACTUALLY went through, so this term and ``commanded`` share one
    reference and the probe's residual is a measurement-minus-measurement
    difference. It used to be the raw crossover, which is what put an
    unbounded chained-round contaminant in the residual (see
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe`).

    The curve is #2291's ``verify_priors.entry_baseline``, already retained
    and already rehydrated into stage 2 — nothing new is captured, persisted,
    or asked of the household for this.

    **Bins the entry capture EXCLUDED become NaN, not values.** They are the
    bins that capture could not trust, and the probe drops non-finite anchor
    bins rather than anchoring a level claim on them. Interpolation spreads
    each NaN to its two neighbours, which is the conservative direction.

    Returns ``None`` — "no comparable before", which leaves the probe
    measuring exactly what it measured before this existed — for a round with
    no entry baseline, and for any arithmetic this cannot complete. Fail-soft
    on the same terms as :func:`applied_offset_db`: an unusable optional
    accounting term is nothing known, never a lost verdict.
    """
    if baseline is None:
        # NAMED, not silent (series-2 D1 fix round). Since D1 this arm
        # decides whether the realized-energy half of the safety axis runs,
        # so a reader asking "why did nothing check the driver" must find an
        # answer here rather than infer it from an absent field.
        #
        # **Not a first-ever round** — that one never arrives here at all.
        # It has no nameable previous graph, so its commanded axis is
        # absent, and the caller takes the ``state_axis_only`` branch below
        # without ever calling this function. What reaches this arm is a round
        # that HAS a commanded axis and no usable baseline record: the three
        # cases ``correction_crossover_v2.entry_baseline_prior_from_state``
        # enumerates — a state file written before that key shipped, a
        # stage 1 whose baseline capture never landed, and a truncated or
        # hand-edited record. All three are exceptional, which is why this
        # is WARNING.
        log_event(
            logger, "correction.crossover_v2_delta_probe_no_entry_anchor",
            level=logging.WARNING, session_id=session_id,
            reason="no_entry_baseline",
        )
        return None
    try:
        # COMPARABLE, or it is not an anchor. An anchor is a subtraction, so
        # a curve measured through another program, or from another mic
        # position, cancels a real finding as readily as a phantom — and
        # since D1 the subtrahend feeds a hard stop rather than only the
        # disclosed residual.
        #
        # BOTH identity fields, asked through the rule's own owner:
        # ``verification.identity_mismatch`` is the identity half of
        # ``_comparability_mismatch``, so the order and the two reason
        # constants live in one place rather than in a parallel spelling
        # here. The MARK is the field that earned the second clause — a
        # program mismatch usually changes the grid and surfaces in the
        # arithmetic below, while a baseline captured at another position is
        # the same program on the same grid and subtracts a different room
        # bin by bin, which nothing else on this path would catch.
        #
        # Unknown on either side is "nothing known" and does not refuse —
        # the module's rule everywhere. A MAGNITUDE bound is deliberately
        # not added: identity is answerable from the record, plausibility is
        # not, and a curve that is comparable and merely wrong is what
        # capture integrity screens for.
        want_program = str(
            getattr(
                program_for_phase(PHASE_VERIFY), "program_id", "",
            ) or ""
        )
        got_program = str(getattr(baseline, "program_id", "") or "")
        got_mark = str(getattr(baseline, "reference_mark", "") or "")
        mismatch = (
            identity_mismatch(
                program_id=got_program,
                reference_mark=got_mark,
                other_program_id=want_program,
                other_reference_mark=REFERENCE_MARK_DESIGN_AXIS,
            )
            if want_program and got_program and got_mark
            else None
        )
        if mismatch is not None:
            log_event(
                logger, "correction.crossover_v2_delta_probe_no_entry_anchor",
                level=logging.WARNING, session_id=session_id,
                reason=mismatch,
                baseline_program_id=got_program,
                baseline_reference_mark=got_mark,
                verify_program_id=want_program,
            )
            return None
        curve = baseline.curve
        entry_hz = np.asarray(curve.hz, dtype=float)
        entry_db = np.asarray(curve.db, dtype=float)
        excluded = np.asarray(baseline.excluded, dtype=bool)
        if entry_hz.size == 0 or entry_db.size != entry_hz.size:
            return None
        if excluded.size == entry_hz.size:
            entry_db = np.where(excluded, np.nan, entry_db)
        measured_pre = np.interp(freqs, entry_hz, entry_db)
        return (measured_pre - predicted_s) + commanded_db
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        # One arm for the whole body, and the identity read is INSIDE it on
        # purpose: ``program_for_phase`` is bound at construction and cannot
        # raise today, but a fail-soft function that computes anything outside
        # its own guard is one refactor from losing a verdict to an
        # accounting term. The reason names the record, not the curve,
        # because the arm now covers both.
        log_event(
            logger, "correction.crossover_v2_delta_probe_no_entry_anchor",
            level=logging.WARNING, session_id=session_id,
            reason="unusable_record", error=str(exc),
        )
        return None


def run_delta_probe(
    logger: logging.Logger,
    *,
    session_id: str,
    tracked: Any,
    commanded: Any,
    band_hz: tuple[float, float] | None,
    declared: Any,
    entry_baseline: Any | None,
    measure_band_spread: tuple[Any, ...],
    verify_band_spread: tuple[Any, ...],
    trust_ceiling: Callable[[Any], float | None],
    applied_offset_seam: Callable[[], float] | None,
    program_for_phase: Callable[[str], Any],
) -> DeltaProbeMap | None:
    """Classify what the speaker actually did against what was commanded.

    The arithmetic, stated plainly because it matters (see
    :mod:`jasper.active_speaker.delta_probe`): the ERROR this classifies is
    ``measured − predicted``, exactly the residual VERIFY's tracking check
    already computes — and it is read off
    ``ProgramAnalysis.verify_tracking_curve``, the very smoothed pair the
    tracking scalars were reduced from, rather than re-derived here. One
    comparison, two consumers. What the delta framing adds is the commanded
    curve — the axis the shortfall-vs-model-error discriminator needs — and
    a band. The band is the one the correction actually commands something
    in, which on this speaker reaches an octave and a half above the
    ``[Fc/2, 2·Fc]`` window tracking looks at, and is where the 2026-07-27
    shelf-realization defect lived.

    **The band is the capture's own trusted band, and there is no
    fallback** (#2521). It comes from the gate disclosure
    (:func:`_gate_trusted_band_hz`) — this capture's gate-derived trusted
    floor intersected with the band its stimulus actually radiated — and
    the raw grid edges this used to pass instead were wider at BOTH ends:
    on the first remote JTS3 session the disclosure said 357-20,000 Hz and
    the probe graded 325-22,480, then rolled the correction back with its
    whole headline — ``worst_hz=21,266``, ``max_error_db=23.4`` — sitting
    above that ceiling, at a frequency nothing had measured. A capture with no
    trusted band leaves the probe unavailable, which is the same answer
    ``_verify_absolute_result`` gives (``no_trusted_crossover_region``) for
    the same missing fact.

    **A missing CHANGE axis leaves the STATE axis, and only the MODEL's own
    departure is graded on it** (#2614, narrowed by series-2 D1). A round
    whose branches are composed through a crossover the applied graph never
    ran has no nameable previous graph — an operator's topology pin is the
    live producer of that shape, and a first-ever round reaches it with no
    applied graph at all — so the commanded delta is absent, and before
    #2614 the whole probe was absent with it. The STATE axis needs no corner
    match, so when it is present the probe reports
    :data:`~jasper.active_speaker.delta_probe.VERDICT_SAFETY_ONLY` — not a
    pass, no shape grade, and no directional safety finding either: those
    are differenced against a pre-apply capture this path has none of, so
    ``safety_anchored`` is False and the record says which half did not run.

    Returns ``None`` when the tracking curve, the trusted band, or BOTH
    axes are missing. ``None`` is the same thing
    :data:`~jasper.active_speaker.delta_probe.VERDICT_UNAVAILABLE` is: no
    evidence to refuse on, and no permission granted either.
    """
    if tracked is None:
        return None
    if band_hz is None:
        log_event(
            logger, "correction.crossover_v2_delta_probe_no_trusted_band",
            level=logging.WARNING, session_id=session_id,
        )
        return None
    try:
        freqs, measured_s, predicted_s = tracked
        freqs = np.asarray(freqs, dtype=float)
        measured_s = np.asarray(measured_s, dtype=float)
        predicted_s = np.asarray(predicted_s, dtype=float)
        declared_db = declared_transfer_db(
            logger, freqs, declared, session_id=session_id,
        )
        commanded_db = None if commanded is None else np.interp(
            freqs,
            np.asarray(commanded[0], dtype=float),
            np.asarray(commanded[1], dtype=float),
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log_event(
            logger, "correction.crossover_v2_delta_probe_failed",
            level=logging.WARNING, session_id=session_id, error=str(exc),
        )
        return None

    spatial = spatial_cost_from_group_spreads(
        {"band_spread": measure_band_spread},
        {"band_spread": verify_band_spread},
    )
    if commanded_db is None:
        if declared_db is None:
            # Neither axis. Unchanged behaviour, and
            # ``declared_transfer_db`` has already named the reason.
            return None
        # The STATE axis in the commanded slot, and the classifier told so.
        # ``realized − commanded`` is still ``measured − predicted``, which
        # grades the MODEL's departure; the two directional findings are not
        # measured here at all, and no entry anchor goes with it — that is a
        # change measurement, sharing no reference with a state axis.
        probe = classify_delta_probe(
            freqs, (measured_s - predicted_s) + declared_db, declared_db,
            band_hz=band_hz, spatial=spatial,
            trust_ceiling_hz=trust_ceiling(freqs),
            expected_offset_db=applied_offset_db(applied_offset_seam),
            state_axis_only=True,
        )
    else:
        # realized − commanded == measured − predicted (the previous-graph
        # prediction cancels), so the realized curve is reconstructed from
        # the three quantities this session actually holds.
        realized_db = (measured_s - predicted_s) + commanded_db
        probe = classify_delta_probe(
            freqs, realized_db, commanded_db, band_hz=band_hz,
            declared_transfer_db=declared_db,
            trust_ceiling_hz=trust_ceiling(freqs),
            spatial=spatial,
            expected_offset_db=applied_offset_db(applied_offset_seam),
            entry_delta_db=entry_delta_db(
                logger, freqs, predicted_s, commanded_db,
                baseline=entry_baseline,
                session_id=session_id,
                program_for_phase=program_for_phase,
            ),
        )
    log_event(
        logger, "correction.crossover_v2_delta_probe",
        # The two non-rollback findings produce no refusal by design, so
        # WARNING is the only thing that puts them in front of anyone
        # reading the journal for a session that otherwise "passed"
        # (#1811, #2521). ``safety_only`` joins them for the same reason
        # one layer over (#2614): the round passed, and the shape check
        # never ran.
        level=(
            logging.WARNING
            if probe.rollback
            or probe.verdict in (
                VERDICT_LEVEL_MISMATCH, VERDICT_FRAME_MISMATCH,
                VERDICT_SAFETY_ONLY,
            )
            else logging.INFO
        ),
        session_id=session_id,
        verdict=probe.verdict,
        reason=probe.reason,
        rollback=probe.rollback,
        # Both bands, because they answer different questions: the trusted
        # one is what this capture supports, the probe one is what cleared
        # the commanded floor inside it. A disputed verdict should not need
        # a second session to establish which bins it was reached over
        # (#2521).
        trusted_band_hz=tuple(round(v, 1) for v in probe.requested_band_hz),
        probe_band_hz=tuple(round(v, 1) for v in probe.probe_band_hz),
        n_bins=probe.n_bins,
        max_error_db=round(probe.max_error_db, 3),
        rms_error_db=round(probe.rms_error_db, 3),
        worst_hz=round(probe.worst_hz, 1),
        exceedance_octaves=round(probe.exceedance_octaves, 3),
        # Was the frame removed at all, and if so what it was and what the
        # grade became without it — the demotion in #2521's policy turns on
        # exactly these, so they travel with the verdict that used them.
        frame_removed=probe.frame.fitted,
        frame_offset_db=(
            None if probe.frame.offset_db is None
            else round(probe.frame.offset_db, 3)
        ),
        frame_tilt_db_per_octave=(
            None if probe.frame.tilt_db_per_octave is None
            else round(probe.frame.tilt_db_per_octave, 4)
        ),
        # ``frame_fit``'s own ill-conditioning defence, and it has to travel
        # with the two terms it qualifies: a tilt fitted over a narrow quiet
        # span is free to be large and mean nothing (measured over 200 seeds
        # of a 10-bin quiet region, p95 |tilt| 10.5 dB/octave). It cannot
        # wrongly demote — the gate only narrows — but it does set the
        # ``frame_removed_*`` numbers a reader quotes, so the span they were
        # taken over belongs beside them.
        frame_n_bins=probe.frame.n_bins,
        frame_band_hz=(
            None if probe.frame.band_hz is None
            else tuple(round(v, 1) for v in probe.frame.band_hz)
        ),
        # Whether the realized-energy half ran at all (series-2 D1). The
        # forensic surface an operator greps, beside the numbers it governs:
        # ``boost_over_declared_bound=false`` reads as "measured, nothing
        # found" and is "not measured" whenever this is false. Two routes
        # reach that, and a first-ever round takes the FIRST: no nameable
        # previous graph, so no commanded axis, so the state-axis branch
        # above — which has no change reference to anchor against by
        # construction. The second is an ordinary round whose banked entry
        # baseline is missing or incomparable, which
        # ``…delta_probe_no_entry_anchor`` names on its own line.
        safety_anchored=probe.safety_anchored,
        frame_removed_max_db=(
            None if probe.frame_removed_max_db is None
            else round(probe.frame_removed_max_db, 3)
        ),
        frame_removed_rms_db=(
            None if probe.frame_removed_rms_db is None
            else round(probe.frame_removed_rms_db, 3)
        ),
        frame_removed_exceedance_octaves=(
            None if probe.frame_removed_exceedance_octaves is None
            else round(probe.frame_removed_exceedance_octaves, 3)
        ),
        gain_factor=(
            round(probe.gain_factor, 4)
            if probe.gain_factor is not None else None
        ),
        gain_intercept_db=(
            round(probe.gain_intercept_db, 3)
            if probe.gain_intercept_db is not None else None
        ),
        expected_offset_db=round(probe.expected_offset_db, 3),
        residual_offset_db=(
            None if probe.residual_offset_db is None
            else round(probe.residual_offset_db, 3)
        ),
        # WHAT the residual removed and WHERE it was measured (#2533). The
        # anchor is the standing pre-apply disagreement the residual is no
        # longer reporting as a level move, and ``None`` means it was not
        # measured and therefore not removed. The quiet terms bound the
        # residual's claim exactly as ``frame_n_bins``/``frame_band_hz``
        # bound the frame's: ``quiet_core_band_hz`` is the INTERQUARTILE span
        # (``frame_band_hz`` is the min/max, which two stray bins defeat) and
        # the coverage is what decides between a whole-band reason and a
        # band-scoped one.
        entry_anchor_offset_db=(
            None if probe.entry_anchor_offset_db is None
            else round(probe.entry_anchor_offset_db, 3)
        ),
        quiet_n_bins=probe.quiet_n_bins,
        quiet_core_band_hz=(
            None if probe.quiet_core_band_hz is None
            else tuple(round(v, 1) for v in probe.quiet_core_band_hz)
        ),
        quiet_probe_coverage=(
            None if probe.quiet_probe_coverage is None
            else round(probe.quiet_probe_coverage, 3)
        ),
        spatial_available=probe.spatial.available,
        spatial_widened=probe.spatial.widened,
        spatial_worst_center_hz=round(probe.spatial.worst_center_hz, 1),
        spatial_worst_widening_db=round(probe.spatial.worst_widening_db, 3),
    )
    return probe
