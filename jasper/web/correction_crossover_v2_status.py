# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 status projection — the read side of ``status["crossover_v2"]``.

One entry point, :func:`crossover_v2_status_block`, and the per-key
projections it composes. Pure read: it loads the durable state through the
host's own owner and shapes it; it decides nothing and writes nothing.

The host (:mod:`jasper.web.correction_crossover_v2`) is reached through the
MODULE object, never by name — ``_host.load_v2_state()``, not a from-import.
That keeps this projection on the same late-bound patch surface it had while
it lived in the host: a test (or the doctor's own suite) that patches
``load_v2_state`` on the host still reaches this reader. A from-import here
would bind a second name that no such patch can reach, and the tests would go
on passing while patching nothing.

The host reaches back — ``persist_conductor_state`` reads the grade this
block projects — from inside the function, so neither file imports the other
at module scope in both directions.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2 import durable_state as _durable
from jasper.log_event import log_event
from jasper.web import correction_crossover_v2 as _host


def _phase_from_state(state: Mapping[str, Any] | None) -> str:
    from jasper.active_speaker.crossover_v2.journey import (
        CAPTURE_PHASES,
        PHASE_APPLYING,
        PHASE_CHECK,
        PHASE_CLOSING,
        PHASE_DONE,
        PHASE_MEASURE,
        PHASE_REVIEW,
        PHASE_VERIFY,
        PRE_CLOUD_CAPTURE_PHASES,
    )

    accepted = set(
        state.get("accepted_phases") or () if isinstance(state, Mapping) else ()
    )
    applied = bool(state and state.get("applied"))
    # A session runs a SUBSET of CAPTURE_PHASES (a verify-only re-arm runs just
    # VERIFY), so walk the subset the conductor recorded. State written before
    # the position groups shipped has no such field, and it came from a session
    # that ran exactly the pre-cloud three — reading it against the longer
    # tuple would report a household mid-cloud in a session that never had one.
    #
    # FILTER FIRST, then decide whether anything survived: a corrupt
    # ``["nonsense"]`` filters to the empty tuple, and treating "recorded but
    # unrecognisable" as "recorded" would walk zero phases and fall straight
    # through to PHASE_DONE — telling a household "Your speaker is tuned" on
    # the strength of a garbled state file. Fail toward the honest fallback.
    recorded = state.get("session_phases") if isinstance(state, Mapping) else None
    known = (
        tuple(str(p) for p in recorded if str(p) in CAPTURE_PHASES)
        if isinstance(recorded, (list, tuple))
        else ()
    )
    phases = known or PRE_CLOUD_CAPTURE_PHASES
    for phase in phases:
        if phase not in accepted:
            if phase == PHASE_VERIFY and PHASE_MEASURE in accepted and not applied:
                return PHASE_APPLYING
            return phase
    # Every phase this session ran is accepted. WHICH terminal state that is
    # depends on whether the session ever intended to verify (two-stage
    # commission D3/PR-T2, work order premise 6 — a verified collision).
    #
    # A MEASURE-ONLY session — stage 1 of the two-stage flow: CHECK, MEASURE,
    # CLOUD_MEASURE, no VERIFY — used to fall straight through to PHASE_DONE,
    # the RESULT screen, whose copy is "Your speaker is tuned." Nothing had
    # been applied; the household had measured a speaker and was told it was
    # tuned. The special case one line up cannot catch it (it keys on
    # PHASE_VERIFY being in the walked phases, which is exactly what a
    # measure-only session lacks), so the honest terminal for that shape is
    # the review interlude: a candidate to look at and a decision to make.
    #
    # Keyed on the WALKED tuple, not on ``known``, so the corrupt-state
    # fallback documented above keeps working unchanged: a garbled
    # ``session_phases`` filters to the empty tuple, walks
    # PRE_CLOUD_CAPTURE_PHASES — which DOES contain PHASE_VERIFY — and so can
    # never reach the review branch on the strength of an unreadable state
    # file. It resolves through the loop above to its first unaccepted phase,
    # exactly as before.
    #
    # ``applied`` still wins over the REVIEW interlude: once something is
    # genuinely on the speaker the decision has been made, and re-offering
    # "apply this?" over a speaker that already has it would be the mirror of
    # the bug being fixed.
    #
    # But an applied measure-only session is not DONE either (two-stage
    # commission D2, PR-T3): stage 1 measured, the household applied from the
    # review screen, and the post-apply check — stage 2 — has not been opened
    # yet. That is exactly PHASE_VERIFY: "the crossover is applied, put the
    # microphone back where it started", whose screen now carries the action
    # that opens stage 2. Once stage 2 IS open its own conductor records VERIFY
    # in ``session_phases``, so this branch stops firing and the ordinary walk
    # above resolves the rest of the journey — including PHASE_DONE's "applied
    # implies graded" ladder for a stage 2 that ran and could not decide.
    if PHASE_VERIFY not in phases:
        if applied:
            return PHASE_VERIFY
        # …and the measuring session's own TAIL is not the review interlude
        # either. Accepting the final cloud position marks every stage-1 phase
        # accepted, so this walk resolves the instant that capture lands —
        # while the household is still holding a phone at the confirm screen
        # (up to the runner's full between-step budget) and again while the
        # combine + fit run. Both used to render the review screen's
        # no-candidate copy: "JTS measured your speaker but has no correction
        # to propose — measure again to try afresh", with a destructive
        # "Measure again" beside it, over a measurement that was still in
        # progress. ``cloud_close`` is what tells those moments apart from a
        # session that genuinely ended with nothing (where it is ``""``, and
        # the review screen's absence copy is the honest answer).
        if str((state or {}).get("cloud_close") or ""):
            return PHASE_CLOSING
        # …and a household who has ALREADY answered this screen does not get
        # it again (#2641). The decline changed nothing on the speaker and did
        # not delete the candidate, so the honest destination is the journey's
        # own resting screen — which is what ``PHASE_CHECK`` renders. Bound to
        # the candidate the decline answered, so a newer measurement brings
        # the review back rather than inheriting a stale "no".
        if _host.review_declined(state):
            return PHASE_CHECK
        return PHASE_REVIEW
    return PHASE_DONE


def _provenance_note(measured_this_session: bool | None) -> str:
    """PR-7's household-facing provenance caption — one owner of the copy, so
    the chart never has to (or may) phrase this itself.

    A re-armed session's ``persist_conductor_state`` can carry a group's
    ``cloud`` entry forward from an EARLIER session verbatim (see
    ``_cloud_summary``'s own comment and the B1 fix above it) — so
    ``/state.crossover_v2.cloud`` and the envelope can describe a measurement
    that did not happen in the session currently open on the page. Silently
    charting it as fresh would be exactly the kind of measured-narrow-
    stated-wide claim this program exists to avoid.

    ``""`` for both "definitely current" and "unknown" (a durable state
    written before this marker existed, or the whole entry unavailable) —
    mirrors :func:`~jasper.active_speaker.crossover_v2.spatial._geometry_guidance_copy`'s
    "empty string when nothing to say" rule rather than asserting freshness
    it cannot prove. Only the one state worth interrupting the household
    for — data that is KNOWN to be stale — gets a sentence.
    """
    if measured_this_session is False:
        return (
            "This chart is from a previous session's measurement — "
            "re-measure to see this session's own result."
        )
    return ""


def _compact_cloud_status(
    cloud_state: Any, *, current_session_id: str | None = None,
) -> dict[str, Any] | None:
    """PR-4's ``/state`` projection of the durable ``cloud`` block — compact:
    per band, only ``passed``; the excluded-interval COUNT, not the
    intervals; the geometry verdict's two household-relevant bits.

    The full per-null τ/r/evidence numbers and the decimated curve live in
    the durable state's own ``pipeline`` sub-key (:func:`_cloud_summary`) and
    the bundle artifact (:func:`bind_cloud_publisher`) — this stays a
    shape-scoped projection, not a third owner of the same data: a consumer
    that reads ``cloud`` alone (the doctor) never has to parse curve-shaped
    data mixed into it. PR-7's chart feed is a fourth, separate KEY —
    :func:`_chart_cloud_status`, riding alongside this one on
    :func:`crossover_v2_status_block`'s own returned dict — for that same
    shape-scoping reason. It is **not** a separate endpoint or a smaller HTTP
    response: see that function's own docstring for the measured byte cost
    and why the actual size mitigation is its own re-decimation ceiling, not
    this key split.

    ``flatness`` (plan PR-5) is the spec-facing gauge, copied VERBATIM from
    the pipeline's own ``flatness`` key — the reduction
    :func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge` made of the
    same ``spec`` report the ``spec_bands`` above project. It rides the
    compact block because the envelope's expert disclosure
    (``crossover_envelope_v2._flatness_details_lines``) renders from THIS
    projection, and copying is what makes the gauge, the ledger line, and the
    persisted report byte-identical rather than merely consistent. ``None``
    when the pipeline never became available — the same "never a fabricated
    clean reading" rule as ``excluded_interval_count`` below.

    ``reference_db`` (PR-7) rides alongside ``flatness`` for the same reason
    ``spec_bands`` carries ``max_deviation_db``: it is the one report-level
    number a chart needs to draw the tolerance corridor
    (``reference_db ± tolerance_db`` per band) and PR-5 already computed it
    once, inside ``spec`` — copied verbatim, never re-derived. ``None`` under
    the same unavailable-pipeline rule as everything else here.

    ``validity_floor_hz`` rides alongside it for one reason: without it a
    live surface cannot tell WHY ``flatness.n_excluded`` is large. The
    interference instruments and the gate-validity clamp both remove
    spec-band bins, and only the honesty instruments' removals are counted
    by ``excluded_interval_count`` — so a reader seeing 4063 excluded bins
    and 5 excluded intervals needs the floor to separate "the room combed
    this speaker" from "one capture's gate collapsed". ``None`` means either
    no position reported a usable floor or the pipeline never ran; it never
    means zero.

    ``spec_bands`` carries each band's own ``max_deviation_db`` (N-3) AND
    ``tolerance_db`` (PR-7 — the corridor half-width a chart draws per band):
    the per-band numbers are what a chart labels, and their absence from the
    only projection a page reads is exactly the pressure that grows a second
    derivation somewhere downstream. Copied from the report like everything
    else here — this stays a projection, never an owner.

    ``carve_outs`` (plan PR-6b) rides the compact block for that same reason,
    and it is the one place this projection is deliberately NOT reduced: the
    τ/r numbers and the copy strings ARE the disclosure owner decision 1
    committed to, so summarising them to a count here would leave the only
    surface a page reads unable to say why a band lost bins — and would grow
    the second copy owner the producer
    (:func:`~jasper.active_speaker.crossover_v2.spatial.carve_outs_by_band`)
    exists to prevent. **It is the largest thing on the entry, and that is
    stated rather than glossed:** measured 2026-07-27 on the S0 ten-position
    cloud (the widest real case this program has — three identified nulls plus
    the one screened range that falls inside a graded band, four rows), the
    carve-outs are **3162 of the entry's 4056 JSON bytes**, against 291 for
    ``spec_bands``, 217 for ``flatness`` and 186 for ``geometry_guidance`` — a
    dated snapshot, since any copy edit moves the digits by tens of bytes; what
    the corpus test pins is the structural claim (four rows, and this key
    larger than every other on the entry combined), not the digits. The copy
    strings are the bulk of it. What bounds it is the instruments
    themselves: three bands, one row per carved range that lands in one, and a
    range outside every spec band produces no row at all. Copied verbatim, like
    ``flatness``. ``[]`` when the pipeline never became
    available — an empty LIST, not ``None``, is safe here because the entry it
    sits in already reports ``overall_passed``/``excluded_interval_count`` as
    ``None`` for that state, so an empty carve-out list cannot be read as "we
    looked and found nothing" without contradicting its own neighbours.

    ``excluded_interval_count`` is ``None`` — not ``0`` — when the pipeline
    never successfully became available (SF-1 review finding, 2026-07-27):
    ``0`` reads as "the honest-instrument pipeline looked and found no
    interference", a fabricated-clean claim this program forbids when the
    pipeline simply never ran (a combine or DSP-step failure — see
    :func:`~jasper.active_speaker.crossover_v2_flow.assemble_cloud_group_result`'s
    own ``available: False`` shape). ``geometry_guidance`` is computed
    directly from the ``geometry`` verdict via
    :func:`~jasper.active_speaker.crossover_v2.spatial._geometry_guidance_copy`
    rather than read out of the pipeline's own copy of it, because geometry
    locking is decided and RECORDED before the pipeline ever runs (see
    ``_close_cloud_group``) — a locked group's "spread the mic further"
    guidance must survive an unrelated downstream DSP failure, not disappear
    with it.

    ``provenance_note`` (PR-7) is the household-facing half of the same
    marker: ``current_session_id`` is the session the CALLER currently has
    open (``crossover_v2_status_block``'s own ``state["session_id"]``);
    ``_cloud_summary`` now stamps each phase's dict with the session that
    actually produced it. When the two disagree — a group carried forward
    from an earlier session (see that function's own comment) — the note
    says so via :func:`_provenance_note`; a durable state written before the
    stamp existed (no ``session_id`` on the block) reads as unknown, not
    stale, so an upgrade does not manufacture a false "this is old" warning
    for data nobody ever mis-attributed.
    """
    from jasper.active_speaker.crossover_v2.spatial import _geometry_guidance_copy

    if not isinstance(cloud_state, Mapping):
        return None
    out: dict[str, Any] = {}
    for phase, block in cloud_state.items():
        if not isinstance(block, Mapping):
            continue
        geometry = block.get("geometry")
        geometry = geometry if isinstance(geometry, Mapping) else {}
        pipeline = block.get("pipeline")
        pipeline = pipeline if isinstance(pipeline, Mapping) else {}
        produced_by = block.get("session_id")
        measured_this_session: bool | None = None
        if isinstance(produced_by, str) and produced_by and current_session_id:
            measured_this_session = produced_by == current_session_id
        entry: dict[str, Any] = {
            "geometry_locked": bool(geometry.get("locked")),
            "thin_evidence": bool(geometry.get("thin_evidence")),
            "geometry_guidance": _geometry_guidance_copy(geometry),
            "spec_bands": [],
            "overall_passed": None,
            "excluded_interval_count": None,
            "flatness": None,
            "reference_db": None,
            "validity_floor_hz": None,
            "carve_outs": [],
            "provenance_note": _provenance_note(measured_this_session),
        }
        if pipeline.get("available") is True:
            spec = pipeline.get("spec")
            spec = spec if isinstance(spec, Mapping) else {}
            bands = spec.get("bands")
            entry["spec_bands"] = [
                {
                    "f_lo_hz": b.get("f_lo_hz"),
                    "f_hi_hz": b.get("f_hi_hz"),
                    # The edges actually graded, beside the nominal ones. The
                    # top band's now follows the session's microphone-trust
                    # ceiling, so a row printing only the nominal pair states
                    # a span this evaluation did not grade.
                    "graded_lo_hz": b.get("graded_lo_hz"),
                    "graded_hi_hz": b.get("graded_hi_hz"),
                    "passed": b.get("passed"),
                    "max_deviation_db": b.get("max_deviation_db"),
                    # WHERE the worst bin sat. A dB with no frequency names
                    # no defect to fix.
                    "max_deviation_hz": b.get("max_deviation_hz"),
                    "tolerance_db": b.get("tolerance_db"),
                }
                for b in bands
                if isinstance(b, Mapping)
            ] if isinstance(bands, list) else []
            entry["overall_passed"] = spec.get("overall_passed")
            entry["reference_db"] = _durable._finite(spec.get("reference_db"))
            merged = pipeline.get("merged_excluded_bands_hz")
            entry["excluded_interval_count"] = (
                len(merged) if isinstance(merged, list) else 0
            )
            flatness = pipeline.get("flatness")
            # Copied, never re-derived — see this function's docstring.
            entry["flatness"] = dict(flatness) if isinstance(flatness, Mapping) else None
            floor = pipeline.get("validity_floor_hz")
            entry["validity_floor_hz"] = (
                float(floor) if isinstance(floor, (int, float)) else None
            )
            carve_outs = pipeline.get("carve_outs")
            # Copied, never re-derived — same rule as ``flatness`` above. A
            # durable state written by a build BETWEEN PR-4 and PR-6b has an
            # available pipeline but no ``carve_outs`` key, and keeps the empty
            # default — indistinguishable here from a group that genuinely
            # carved nothing. ``excluded_interval_count`` is the tell for a
            # reader who needs to know: > 0 alongside an empty carve-out list
            # is the pre-PR-6b era, since a group that carved nothing has a
            # count of 0. No repair is attempted from this projection: it is
            # not an owner of the pipeline's data (see the docstring).
            entry["carve_outs"] = (
                [dict(band) for band in carve_outs if isinstance(band, Mapping)]
                if isinstance(carve_outs, list)
                else []
            )
        out[str(phase)] = entry
    return out or None


# PR-7's own re-decimation ceiling for the polled chart feed — HALF of
# crossover_v2.spatial.CLOUD_CURVE_MAX_JSON_POINTS (512), the ceiling the
# pipeline's own ``curve`` key (and the persisted bundle artifact it is
# copied from) already uses. Review S-1 (2026-07-27) measured the byte cost
# of NOT re-decimating: 41,161 bytes for both phases' ``cloud_chart`` entries
# at the full 512-point resolution, on the real S0 ten-position cloud —
# roughly 82% of an otherwise-typical envelope response, repeated on every
# ~1.5 s poll while the wizard page is open. Halving to 256 points/phase
# measured 20,653 bytes on the same corpus (a 49.8% reduction) for a chart
# drawn into a ~640 px-wide canvas, where 256 points is already well under
# 1 px/point — visually identical to 512 on the one dimension that matters
# (this projection's own consumer), so nothing is lost by re-decimating
# again here rather than raising the ceiling everywhere `curve` is used.
CHART_CURVE_MAX_JSON_POINTS = 256


def _decimate_curve_for_chart(freqs: Any, mags: Any) -> dict[str, Any] | None:
    """Stride a stored curve down to at most :data:`CHART_CURVE_MAX_JSON_POINTS`.

    THE chart feed's decimation — extracted from :func:`_chart_cloud_status`'s
    body (two-stage commission D4) when the predicted curve became a second
    curve on the same block. D4 asks for the prediction to ride "the existing
    ``CHART_CURVE_MAX_JSON_POINTS`` path so the chart feed keeps one decimation
    owner"; a second inline copy of this stride would be a second owner, and
    two curves drawn in one frame at silently different densities is exactly
    the drift that costs. ``None`` for anything that is not a usable pair, so a
    caller never fabricates an empty curve out of malformed state.

    **Ceiling-division stride, not floor (gate finding on #1858, SF-1).** The
    original shape here was ``step = n // CAP`` — a *soft* ceiling, documented
    (and pinned, before this fix) as capable of overshooting by up to one
    stride: 1031 raw points strode by 4 and yielded 258, not 256. That was
    tolerable while every caller's persisted length always landed at or above
    ``CAP * 2`` (both ``_decimate_sum``'s old raw stride and
    ``_decimate_curve_for_json``'s stride always overshoot to slightly above
    their own 512-point cap). #1858's block-average fix to ``_decimate_sum``
    changed that: block-averaging *undershoots* its cap instead of
    overshooting it (a 32769-bin capture landed at 504, not 512-513), which
    put the predicted curve's persisted length just BELOW ``CAP * 2`` — where
    floor division gives ``step = 1``, i.e. no reduction at all (504 points
    rendered, not ~252), breaking the soft-ceiling promise outright and
    rendering the prediction at roughly double the cloud curves' density in
    the same chart frame. Ceiling division (``-(-n // CAP)``, this module's
    existing integer-ceiling idiom — see
    :func:`~jasper.audio_measurement.spatial_combine._decimate_to_analysis_grid`)
    makes ``len(rendered) <= CAP`` a TRUE hard bound for any input length,
    closing the whole class rather than this one instance: it guarantees
    ``step >= n / CAP`` by construction, so ``ceil(n / step) <= CAP`` always.
    Both curve families now render through the identical formula, so neither
    can silently outrun the other's density regardless of which side of any
    boundary their own persisted length lands on.

    **One deliberate behaviour delta from the inlined version this replaced.**
    A zero-length pair used to yield ``{"freqs_hz": [], "magnitude_db": []}``;
    it now yields ``None``. Reachable only from malformed durable state — a
    pipeline marked ``available: True`` whose stored curve is empty — and the
    new answer is the honest direction: an empty curve renders as "we looked
    and there is nothing there", which is the fabricated-clean-reading shape
    this module forbids, whereas ``None`` says "no curve", which is what an
    empty stored curve actually means.
    """
    if not isinstance(freqs, list) or not isinstance(mags, list):
        return None
    n = min(len(freqs), len(mags))
    if n == 0:
        return None
    step = max(1, -(-n // CHART_CURVE_MAX_JSON_POINTS))
    return {
        "freqs_hz": [_durable._finite(f) for f in freqs[:n:step]],
        "magnitude_db": [_durable._finite(m) for m in mags[:n:step]],
    }


def _chart_cloud_status(cloud_state: Any) -> dict[str, Any] | None:
    """PR-7's chart-feed projection of the durable ``cloud`` block — the ONE
    thing :func:`_compact_cloud_status` deliberately withholds: the decimated
    combined curve a before/after chart draws. Everything else the chart
    needs (the tolerance corridor's ``reference_db``/``tolerance_db``, the
    carve-out disclosure) already rides the compact block, so duplicating it
    here would be a second, driftable copy of the same numbers — this key
    carries only what genuinely has no other home.

    **What the key-level separation from ``_compact_cloud_status`` does and
    does not buy (review S-1 correction, 2026-07-27).** Both projections ride
    the SAME returned dict (:func:`crossover_v2_status_block`) and therefore
    the same HTTP response — ``/correction/crossover/status`` and this
    module's envelope both carry ``cloud`` AND ``cloud_chart`` together, so
    splitting the KEY does **not** shrink that response's byte count (an
    earlier version of this and two sibling comments overclaimed exactly
    that — "never pay" was wrong for this endpoint, which does carry both).
    What the split buys is narrower: a consumer that reads ONLY ``cloud`` —
    the doctor (:func:`~jasper.cli.doctor.correction.check_crossover_v2_cloud_pipeline`),
    and any future reader of the compact projection alone — never has to
    parse or skip over curve-shaped data mixed into that key's own shape.
    See :data:`CHART_CURVE_MAX_JSON_POINTS` above for the actual byte-cost
    mitigation (halving this key's own resolution), which is the fix that
    matters for the endpoint's total size.

    Same per-phase presence and ``None``-means-unavailable rules as
    :func:`_compact_cloud_status` (mirrored rather than shared because the two
    projections serve different consumers and have no other logic in common):
    a phase key is present whenever the durable block has one, ``curve`` is
    ``None`` until the pipeline becomes available, and it is never a
    fabricated empty curve.
    """
    if not isinstance(cloud_state, Mapping):
        return None
    out: dict[str, Any] = {}
    for phase, block in cloud_state.items():
        if not isinstance(block, Mapping):
            continue
        pipeline = block.get("pipeline")
        pipeline = pipeline if isinstance(pipeline, Mapping) else {}
        curve = None
        if pipeline.get("available") is True:
            raw_curve = pipeline.get("curve")
            if isinstance(raw_curve, Mapping):
                curve = _decimate_curve_for_chart(
                    raw_curve.get("freqs_hz"), raw_curve.get("magnitude_db"),
                )
        out[str(phase)] = {"curve": curve}
    return out or None


def _prediction_status(state: Any) -> dict[str, Any] | None:
    """The PREDICTED post-apply response and its stored spec verdict, or
    ``None`` (two-stage commission D4).

    Rides :func:`crossover_v2_status_block`'s returned dict beside ``cloud`` /
    ``cloud_chart``. Both halves were already computed — the curve by
    ``_decimate_sum`` at persist time, the verdict by the conductor's
    accountability seam against the FULL-RESOLUTION tuple — and neither reached
    any surface. This projects; it never grades.

    **Nothing renders it yet.** It is the wire half of the two-stage flow's
    review screen (PR-T2's "what we predict" panel and the chart's third
    curve), landed on its own rung so that screen is built against data already
    proven on the wire rather than against a shape invented alongside it.

    **``curve`` and ``spec`` are independently absent, and all four
    combinations are reachable.** Enumerated because a consumer — PR-T2's
    review screen above all — has to render each one differently:

    1. *Both present* — the ordinary closed session. Draw the curve, state the
       verdict.
    2. *Curve, no report* — a state written before D4, or a prediction the
       evaluator refused (:func:`~jasper.active_speaker.crossover_v2_flow
       .spec_report_for_predicted_sum` returned ``None``). Draw the curve, say
       the verdict is unknown; **do not** infer one from the picture.
    3. *Neither* — no session has closed a candidate. This function returns
       ``None`` outright rather than an empty shell.
    4. *Report, no curve* — **the least obvious of the four.**
       ``_assert_accountable`` stashes the verdict BEFORE the improvement gate
       runs and ``_measure_predicted_sum`` only after it returns, so a refusal
       between the two persists a report with ``predicted_sum`` still ``None``
       — honest, not a leak: the spec verdict did evaluate that prediction. The
       refusal that produced this shape is retired (``accountability``'s item
       2); a pre-retirement state still carries it, and a consumer shows the
       verdict with no curve to draw.

    So ``overall_passed`` is ``None`` — not ``False`` — whenever no report was
    stored, under the same never-fabricate-a-clean-reading rule
    :func:`_compact_cloud_status` states at length. ``None`` here means
    "unknown", and a consumer must not read it as permission. ``False`` is the
    opposite: a real graded verdict that the prediction misses the spec, which
    is exactly what state 4 carries.

    ``spec_bands`` / ``reference_db`` mirror the compact cloud block's own
    vocabulary key-for-key on purpose: the review screen draws the measured
    curve and this one in ONE deviation frame with one tolerance corridor, and
    a second spelling of the same five per-band numbers is how the two frames
    would drift apart.
    """
    priors = (state or {}).get("verify_priors")
    if not isinstance(priors, Mapping):
        return None
    raw_curve = priors.get("predicted_sum")
    curve = (
        _decimate_curve_for_chart(
            raw_curve.get("freqs_hz"), raw_curve.get("magnitude_db"),
        )
        if isinstance(raw_curve, Mapping)
        else None
    )
    spec = priors.get("predicted_spec")
    spec = spec if isinstance(spec, Mapping) else {}
    if curve is None and not spec:
        return None
    bands = spec.get("bands")
    return {
        "curve": curve,
        "spec_bands": [
            {
                "f_lo_hz": b.get("f_lo_hz"),
                "f_hi_hz": b.get("f_hi_hz"),
                "passed": b.get("passed"),
                "max_deviation_db": b.get("max_deviation_db"),
                "tolerance_db": b.get("tolerance_db"),
            }
            for b in bands
            if isinstance(b, Mapping)
        ] if isinstance(bands, list) else [],
        "overall_passed": (
            spec.get("overall_passed")
            if isinstance(spec.get("overall_passed"), bool)
            else None
        ),
        "reference_db": _durable._finite(spec.get("reference_db")),
        "comparison": (
            dict(spec["comparison"])
            if isinstance(spec.get("comparison"), Mapping) else None
        ),
    }


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    state = _host.load_v2_state()
    session_id = (state or {}).get("session_id")
    attempts = (state or {}).get("attempts_loop")
    attempts = attempts if isinstance(attempts, Mapping) else {}
    last_attempt_decision = attempts.get("last_decision")
    # Count is derived from its persistence owner on every state read. Keeping
    # a second copy in journey state made crash recovery and offline store
    # repair observable as two contradictory counts.
    store_count = _host._attempt_loop_store_snapshot().model_error_count
    try:
        needs_recovery = bool(_host.session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    block: dict[str, Any] = {
        "phase": _phase_from_state(state),
        # The commission tier behind whatever this block reports, or ``None``
        # when the durable state does not say (pre-tier state, or a session
        # that declared none). Never defaulted to "full" — see
        # ``persist_conductor_state``.
        "tier": (str((state or {}).get("tier") or "") or None),
        # Which sub-moment of the measuring session's tail this is, when
        # ``phase`` is ``closing`` (two-stage D1). ``""`` everywhere else.
        "cloud_close": str((state or {}).get("cloud_close") or ""),
        "candidate": (state or {}).get("candidate"),
        "accepted_sound_revision": (state or {}).get("accepted_sound_revision"),
        # MEASURE's own verdict-time disclosures — today just G1's ripple
        # reservation (#2087). Copied through unvalidated, exactly like
        # ``candidate`` and ``verify`` beside it: the envelope's own accessor
        # is the validating reader, so a state file written by another build
        # cannot 500 this poll path.
        "measure": (state or {}).get("measure"),
        # The last graded round's adoption receipt — what it decided, which row
        # decided it, and where it sat in the series (#2537, #2602).
        #
        # **This projection was missing**, and the envelope has read ``None``
        # here on every real box since #2537: ``persist_conductor_state`` wrote
        # ``state["round_receipt"]`` and this block never forwarded it, so the
        # done screen's round key and its keep-for-iteration caveat existed only
        # in unit tests that hand-built the status dict. #2602 makes that gap
        # load-bearing rather than merely wasteful — a series that cannot tell a
        # household another round is coming has not delivered the ruling — so it
        # is fixed here rather than filed. Copied through unvalidated, exactly
        # like ``candidate`` and ``verify`` beside it: the envelope's own
        # accessor is the validating reader.
        "round_receipt": (state or {}).get("round_receipt"),
        "verify": (state or {}).get("verify"),
        "failure": (state or {}).get("failure"),
        "apply_blocked": (state or {}).get("apply_blocked"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        # Issue #1863: whether the v2-aware Undo (handle_v2_restore) actually
        # has something to restore, so the envelope layer can stop offering a
        # button the endpoint is guaranteed to refuse. ASKED of the rule's
        # owner rather than transcribed here — see
        # :func:`restore_anchor_static_prefix_refusal` for why this reader
        # takes the static prefix and not the full five-gate resolver.
        "can_undo": _host.restore_anchor_static_prefix_refusal(state) is None,
        "session_id": session_id,
        # Minimal live-loop observability: no attempt curves/history on the
        # household polling path, only the kernel output the envelope formats
        # and the durable model-error record count.
        "attempts_loop": {
            "last_decision": (
                dict(last_attempt_decision)
                if isinstance(last_attempt_decision, Mapping) else None
            ),
            "store_count": store_count,
        },
        # Flat-linearization plan PR-4: the compact per-group honesty
        # verdict. ``None`` when no group has closed yet — never a fabricated
        # "clean" reading (mirrors every other honesty-instrument field's own
        # "not yet run" rule). ``current_session_id`` lets the compact block
        # tell a fresh group apart from one carried forward from an earlier
        # session (PR-7's provenance marker) — see ``_cloud_summary``'s and
        # ``_compact_cloud_status``'s own comments.
        "cloud": _compact_cloud_status(
            (state or {}).get("cloud"), current_session_id=session_id,
        ),
        # PR-7: the chart-only curve feed, kept off the ``cloud`` key above
        # so the doctor (which reads only ``cloud``) never has to parse
        # curve-shaped data mixed into it. This DOES ride the same returned
        # dict — and so the same HTTP response — as ``cloud``; see
        # _chart_cloud_status's own docstring for the measured byte cost and
        # its own re-decimation ceiling, which is the actual size mitigation.
        "cloud_chart": _chart_cloud_status((state or {}).get("cloud")),
        # Two-stage commission D4: the PREDICTED post-apply response and the
        # spec verdict the accountability seam graded it with. It rides beside
        # ``cloud``/``cloud_chart`` because the review screen (PR-T2 — no
        # consumer yet) will draw all three in one frame — measured, proposed,
        # predicted — and a household
        # deciding whether to apply is comparing exactly those. Kept as ONE key
        # rather than split compact/chart the way ``cloud`` is: that split
        # exists because the doctor reads ``cloud`` alone and should not have
        # to skip curve-shaped data, and nothing reads the prediction that way.
        # ``None`` until a candidate's close has stored a prediction.
        "prediction": _prediction_status(state),
        # WO-1's read half (CC1): what this speaker's measurement LEARNED, in
        # the one register a household may read. ``[]`` means "nothing was
        # banked" — never "nothing was looked for", which is the store's own
        # absent-vs-empty distinction and stays where it belongs, in the
        # bundle artifact.
        "findings": _household_findings_status(state),
        # The across-rounds view no single receipt can carry: per spec band,
        # how much of what was commanded arrived, over how many banked rounds,
        # and how much those rounds disagreed. Read from the banked receipts
        # rather than this state file because it is HISTORY — the durable
        # state holds only the last round, and the whole claim here is about
        # the several before it.
        #
        # Disclosure. Nothing reads it back: no adoption row, refusal or
        # prescription consumes a confidence label, and this module writes
        # nothing.
        "controllability": _controllability_status(),
    }
    block["post_apply_grade"] = _host._post_apply_grade(block)
    return block


def _controllability_status() -> dict[str, Any] | None:
    """The per-band controllability ledger, or ``None`` when it cannot be read.

    ``None`` rather than an empty document, and the distinction is the usual
    one: a box with banked rounds that measured nothing still publishes a full
    band axis of ``unobserved`` rows, so ``None`` here means the LEDGER was
    unavailable, never that the speaker is uncontrollable.

    Guarded because this is the one entry on this block that touches the
    bundle store. The caller's own handler turns any raise into a dropped
    ``crossover_v2`` key for the whole poll, and a history view is never worth
    that: an unreadable bundle root costs this key alone.
    """

    from jasper.active_speaker.controllability_ledger import (
        read_controllability_ledger,
    )

    try:
        return read_controllability_ledger().to_dict()
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(
            _host.logger,
            "correction.controllability_ledger_unavailable",
            level=logging.WARNING,
            exc_info=True,
        )
        return None


def _household_findings_status(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The banked findings a household may read, from the durable projection.

    Reads what :func:`_bank_household_findings` wrote — never the bundle, and
    never ``os``-anything: this runs on every wizard poll, and the read that
    costs (reopen + re-hash the artifact and its citation) already happened
    once, at publish.

    **Validated, not trusted.** The state file is JSON written by some build,
    possibly an older or newer one, so every row is checked rather than passed
    through: a row without usable copy is DROPPED (an empty or non-string
    sentence is not a finding a household can read), and an unusable ``at``
    becomes ``None`` — which the envelope renders as "we cannot say when",
    exactly as an undated failure record does. Fabricating neither a sentence
    nor a date is the whole contract here, and it is pinned at THIS layer
    (``tests/test_correction_crossover_v2_endpoints.py``'s projection-contract
    tests) rather than only through the envelope: a weakened copy check here —
    ``str(row.get("household_copy") or "")`` — renders a fabricated ``"42"`` on
    the done screen end to end, and every screen-level assertion stays green
    while it does.

    Never raises. ``float`` on an unbounded JSON integer raises
    ``OverflowError`` rather than returning ``inf``, and this runs on the
    wizard's 1.5 s poll path, so an escaping conversion would be a 500 on a
    plain page load — the same failure ``_record_when_phrase`` catches one
    module over for the same reason.
    """
    evidence = (state or {}).get("evidence")
    rows = (
        evidence.get(_durable.FINDING_HOUSEHOLD_REFS_KEY)
        if isinstance(evidence, Mapping)
        else None
    )
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        copy = row.get("household_copy")
        if not isinstance(copy, str) or not copy.strip():
            continue
        at = row.get("at")
        try:
            stamp = (
                float(at)
                if isinstance(at, (int, float)) and not isinstance(at, bool)
                else None
            )
        except (TypeError, ValueError, OverflowError):
            stamp = None
        if stamp is not None and not math.isfinite(stamp):
            stamp = None
        out.append({"household_copy": copy, "at": stamp})
    return out
