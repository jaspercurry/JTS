# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commanded axis: what one apply asks the summed response to CHANGE.

The delta probe (:mod:`jasper.active_speaker.delta_probe`) grades a measured
change against a commanded one. This module builds the commanded one, and its
whole contract is a single sentence: **every element the apply commands must
appear in it, so that only genuinely uncommanded behaviour can surprise the
probe.**

**Why this module exists** (issue #2611). Until it did, the commanded curve was
``predicted_branch_sum(linearized branches) − predicted_branch_sum(raw
branches)`` with BOTH sides evaluated at the *applied* candidate's polarity,
delay and role gains. Those three therefore cancelled by construction, and the
curve carried the correction filters and nothing else. The probe's measured
side, meanwhile, is a change across the apply (``measured_post −
measured_pre`` once :func:`~jasper.active_speaker.delta_probe.
classify_delta_probe`'s entry anchor is removed), so it carries every element —
and on the 2026-08-16 jts3 round 1 the two disagreed by exactly the elements the
model had dropped:

* a per-role gain step of **+3.3209 dB** (tweeter −10.2141 → −6.8932) landed in
  the probe's tweeter-only quiet band (interquartile 7999-8285 Hz) and was read
  as an uncommanded level shift, ``residual_offset_db = +3.2198``;
* a commanded polarity flip (inverted → keep) and a 96 → 59.6 µs delay change
  moved the blend null, and were read as an uncommanded SHAPE change —
  ``verdict=model_error reason=realized_shape_differs_from_commanded``.

The round was rolled back. The tune it rolled back had measured BETTER on the
one capture taken (blend −1.80 dB at 1003 Hz against a 2.0 dB limit, level
error 0.39, tracking 0.07). The instrument was not too strict; its expectation
was incomplete.

**The construction.** Both sides are evaluated on the SAME measured branch pair
from the SAME capture, through the SAME summation model
(:func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum`), so
the branch measurements and the summation model divide out exactly as they did
before. What changed is only which two graphs the two sides describe:

* the APPLIED side is the candidate about to be applied — its correction
  filters, its role gains, its polarity, its delay. This is
  ``predicted_sum``, unchanged and shared with VERIFY's tracking reference.
* the PREVIOUS side is **the graph the apply replaces** — the currently-applied
  Layer-A profile's own correction filters, role gains, polarity and delay.

Their difference is what the speaker is being asked to do, relative to what it
is doing now, which is the same frame the measured side is in.

**What is deliberately NOT on this axis, and where it is accounted instead.**
The pre-split common attenuation each graph charges for its own boost
(``camilla_yaml.linearization_headroom_db``, emitted as
``active_baseline_headroom``) is applied BEFORE the branch split, so
``predicted_branch_sum`` — a model of the two branches — structurally cannot
carry it. It stays where it already was: the scalar
:func:`~jasper.active_speaker.baseline_profile.applied_program_level_delta_db`,
read at PROBE time off the two profiles that actually went live and removed by
``classify_delta_probe`` before anything is measured. The two accounts are
disjoint by construction — per-role gains here, the common pre-split gain there
— and together they cover every level term the apply moves. (#2611's offline
analysis proposed folding the per-role step into that scalar as an interim
patch; with this axis complete that patch is unnecessary and was never
written, so there is nothing to retire.)

**The corner has to match, and it is CHECKED rather than assumed.** The
measured branches are composed through a CONFIGURED crossover
(``program_analysis._compose_configured_path_ir``'s ``S = M*C/P``), and the
previous side is modelled on those same branches — so the model only describes
the graph the speaker actually ran while that ``C`` is the ``C`` the previous
apply ran. A mismatch omits a term measured at up to 5.88 dB against this
probe's 1.5 dB tolerance (adversarial panel, PR #2614), and **two live doors
move it, not one** — an earlier revision of this module named only the first
and called the second benign:

* the household edits the crossover corner in ``/sound`` between rounds, so the
  capture composes at the new corner while the applied profile ran the old one;
* **an operator's topology pin**, which opens the session AT the pinned corner
  (``topology_prescription.apply_topology_pin``), so the round's branches carry
  a ``C`` the applied profile never ran.

(A third door, the alternative-Fc sweep, closed with the corner hunt on
2026-08-21 — ``docs/tuning-master-plan.md`` ticket 2.3. The check below is
unchanged: it was never counting doors, it compares corners.)

:func:`profile_crossover_fc_hz` reads the corner the applied graph was built at
off its own snapshot, and the caller refuses the previous side outright when it
does not match the corner the capture was composed at. The probe then reports
``unavailable`` with the reason on the journal — no rollback and no pass, which
is the same answer every other unnameable previous graph gets here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Sequence

import numpy as np

from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    summed_model_residual_delay_us,
)

from .plan_assembly import SummationFrame, compose_linearized_prediction

__all__ = [
    "CornerDisagreement",
    "GraphSummation",
    "PreviousGraph",
    "commanded_delta",
    "corner_disagreement",
    "graph_predicted_sum",
    "previous_graph_prediction",
    "profile_crossover_fc_hz",
    "profile_graph_summation",
]


@dataclass(frozen=True)
class GraphSummation:
    """What one emitted Layer-A graph does to a measured branch pair.

    The four elements :func:`graph_predicted_sum` needs, and nothing else — a
    value, so the reader that extracts them from a profile and the model that
    consumes them cannot drift into two different opinions about which graph is
    being described.

    ``trim_db`` is keyed by this speaker's branches LOWEST FIRST;
    :func:`graph_predicted_sum` reads the graph's own roles off it.

    ``delay_us`` is SIGNED in the analysis frame (design §5.6.5: positive means
    the tweeter branch is delayed), never the non-negative magnitude a profile
    stores beside a delayed role.

    ``polarity_sign`` is the relative sign **in the measured branches' own
    frame**, which is a flip RELATIVE TO the design draft rather than an
    absolute reading of how the drivers are wired. The branches arrive already
    carrying the draft's declared per-role polarity
    (``program_analysis._compose_configured_path_ir`` multiplies it in), so
    ``+1`` means "the relative polarity the preset declares" — exactly what the
    applied side's ``alignment.polarity_sign`` means. A profile records ABSOLUTE
    per-role ``inverted`` flags instead, so :func:`profile_graph_summation` must
    convert; taking those flags as this field is what makes the two sides of the
    subtraction disagree whenever the draft declares an inverted branch
    (adversarial panel, PR #2614). Inverting BOTH branches leaves ``|W + s·T|``
    untouched and is correctly a ``+1`` in either frame.
    """

    trim_db: Mapping[str, float]
    delay_us: float
    polarity_sign: int
    linearization: Mapping[str, Sequence[Mapping[str, Any]]]


def profile_graph_summation(
    profile: Mapping[str, Any] | None,
    *,
    roles: Sequence[str],
    draft_inverted_by_role: Mapping[str, bool],
) -> GraphSummation | None:
    """Read one applied profile as a :class:`GraphSummation`, or ``None``.

    ``roles`` is this speaker's branches LOWEST FIRST — one on a 1-way main,
    two otherwise. The frame conversions below are stated between ``roles[0]``
    and ``roles[-1]``, the same declaration on a lone branch (zero delay, ``+1``
    sign). More than two is refused: ONE delay and ONE relative sign.

    ``None`` — "this profile does not say what the speaker is playing" — for an
    absent profile, for one whose authoritative ``corrections`` mapping does not
    carry EVERY role, and for one that names a role without naming its
    ``gain_db``. It is never a graph that trims nothing: a fabricated unity
    graph would make the commanded axis claim the apply commands the whole of
    the previous profile's trim, which is the same class of wrong answer this
    module exists to remove, pointing the other way. (An absent ``delay_ms`` is
    different and IS a zero: the profile records a magnitude only on whichever
    role is delayed, so its absence on the other role is a statement, not a
    gap.)

    Both halves are read through :mod:`~jasper.active_speaker.baseline_profile`,
    which owns WHICH copy of a profile field is authoritative. This function
    owns only the translation into the summed model's own vocabulary: the
    profile stores a non-negative delay magnitude on whichever role is delayed,
    and a per-role ``inverted`` flag; the model wants one signed delay and one
    relative sign.

    ``draft_inverted_by_role`` is that translation's second half and it is
    REQUIRED, not defaulted, because there is no safe guess. It is the design
    draft's own per-role declared polarity — ``camilla_yaml.role_polarity`` of
    the preset the capture was composed through — and the returned
    ``polarity_sign`` is the profile's relative polarity stated RELATIVE TO it,
    which is the frame the measured branches are already in (see
    :class:`GraphSummation`). Reading the profile's absolute flags as the sign
    instead put the two sides of the subtraction in different frames on any
    speaker whose draft declares an inverted branch — reachable from ``/sound``
    Alignment, and measured to disagree (adversarial panel, PR #2614).
    """
    from jasper.active_speaker.baseline_profile import (
        profile_driver_corrections,
        profile_linearization,
    )

    if not 1 <= len(roles) <= 2:
        return None
    corrections = profile_driver_corrections(profile)
    entries: list[Mapping[str, Any]] = [
        entry for role in roles
        if isinstance(entry := corrections.get(role), Mapping)
    ]
    if len(entries) != len(roles):
        return None
    gains: list[Any] = [entry.get("gain_db") for entry in entries]
    if any(gain is None for gain in gains):
        return None
    lower, upper = entries[0], entries[-1]
    try:
        trim_db = {role: float(gain) for role, gain in zip(roles, gains)}
        # The profile records a non-negative magnitude on the delayed role
        # (``measured_crossover_candidate.driver_corrections``), so the sign is
        # recovered from WHICH role carries it: a delayed upper branch is the
        # positive direction of the analysis frame, a delayed lower one the
        # negative. Both are read rather than assuming only one is set.
        delay_us = 1000.0 * (
            float(upper.get("delay_ms") or 0.0)
            - float(lower.get("delay_ms") or 0.0)
        )
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(delay_us) and all(map(math.isfinite, trim_db.values()))):
        return None
    linearization = profile_linearization(profile)
    # The frame conversion, in one place. The profile's flags are absolute and
    # the draft's are absolute; the model wants the difference between the two
    # relative polarities, because the branches already carry the draft's.
    profile_flip = bool(lower.get("inverted")) != bool(upper.get("inverted"))
    draft_flip = (
        bool(draft_inverted_by_role.get(roles[0]))
        != bool(draft_inverted_by_role.get(roles[-1]))
    )
    return GraphSummation(
        trim_db=trim_db,
        delay_us=delay_us,
        polarity_sign=-1 if profile_flip != draft_flip else 1,
        linearization={
            role: tuple(
                entry for entry in (linearization.get(role) or ())
                if isinstance(entry, Mapping)
            )
            for role in roles
        },
    )


def profile_crossover_fc_hz(profile: Mapping[str, Any] | None) -> float | None:
    """The crossover corner one applied profile's graph was built at, or ``None``.

    The one owner of "which ``C`` did this profile run", because the previous
    side of the commanded axis is only a model of the graph the speaker played
    while that corner matches the corner the capture was composed at (see the
    module docstring). The caller compares the two and refuses on a mismatch.

    Read off ``recomposition_snapshot["preset"]`` — the immutable design draft
    the profile was emitted from — through the same
    :class:`~jasper.active_speaker.profile.ActiveSpeakerPreset` parse every
    other snapshot reader uses, and reduced through
    :class:`~jasper.active_speaker.crossover_v2.contracts.CandidateAcousticContext`,
    which is v2's single derivation of a corner from a section set and fails
    closed on a split or empty one rather than picking a region.

    ``None`` — "this profile does not say which corner it ran" — for a profile
    with no snapshot, an unparseable preset, or a section set that names no one
    corner. An era-older profile written before the snapshot existed reaches
    that, and it is the honest answer: the corner cannot be checked, so the
    previous graph cannot be affirmed, and the probe declines to grade rather
    than grading against a ``C`` nobody can confirm.
    """
    from jasper.active_speaker.branch_chain import sections_by_role
    from jasper.active_speaker.crossover_v2.contracts import (
        CandidateAcousticContext,
        CrossoverV2ContractError,
    )
    from jasper.active_speaker.profile import (
        ActiveSpeakerConfigError,
        ActiveSpeakerPreset,
    )

    if not isinstance(profile, Mapping):
        return None
    snapshot = profile.get("recomposition_snapshot")
    raw = snapshot.get("preset") if isinstance(snapshot, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(raw))
        fc_hz = float(
            CandidateAcousticContext.from_sections(
                sections_by_role(preset.crossover_regions),
            ).fc_hz
        )
    except (
        ActiveSpeakerConfigError,
        CrossoverV2ContractError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    return fc_hz if math.isfinite(fc_hz) and fc_hz > 0.0 else None


def graph_predicted_sum(
    freqs_hz: Any,
    branch_tf: Mapping[str, Any],
    graph: GraphSummation,
    *,
    anchor_delay_us: float | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """``(freqs_hz, magnitude_db)`` this graph would produce on these branches.

    THE SAME composition the applied side is built from,
    :func:`~.plan_assembly.compose_linearized_prediction`, so the two curves
    differ by the GRAPH and by nothing else. Only the residual is this
    function's own, through :func:`~jasper.audio_measurement.program_analysis.
    summed_model_residual_delay_us` — the ONLY correct way to enter a delay
    here (its docstring carries the double-counting hazard). The branches are
    the GRAPH's own, lowest first.

    **The anchor is this capture's, and it does NOT cancel** — that is why it
    has to be this capture's. It sets where the blend null sits, and a null is
    a place the two curves are enormous and opposite: re-anchoring one side
    moves its null somewhere the other side has none, and the two disagree by
    7-33 dB there (measured, adversarial panel PR #2614). What makes the
    subtraction meaningful is not a term dividing out but both graphs being
    stated in ONE phasing frame — the frame the branch pair was actually
    measured and aligned in.

    ``None`` when a branch is missing or the arithmetic cannot complete — the
    same bounded, fail-soft family as its callers, because an unbuildable model
    of the previous graph is an absent commanded axis, never a crash.
    """
    try:
        freqs = np.asarray(freqs_hz, dtype=float)
        return compose_linearized_prediction(
            SummationFrame(
                freqs_hz=freqs,
                branch_tf={
                    role: np.asarray(branch_tf[role], dtype=np.complex128)
                    for role in graph.trim_db
                },
                polarity_sign=int(graph.polarity_sign),
                residual_delay_us=summed_model_residual_delay_us(
                    anchor_delay_us, graph.delay_us,
                ),
            ),
            filters_by_role=graph.linearization,
            role_attenuations_db=graph.trim_db,
        )
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        return None


def commanded_delta(
    previous_predicted_sum: Any, predicted_sum: Any,
) -> tuple[np.ndarray, np.ndarray] | None:
    """``(freqs_hz, delta_db)`` — the applied graph minus the one it replaces.

    ``None`` — the probe reports ``unavailable``, which is not a pass and not a
    permission — when either curve is missing or the two cannot be put on one
    grid. A missing PREVIOUS curve is the load-bearing case: it means nothing
    here can say what the speaker is playing right now, and an expectation built
    without that fact is the incompleteness #2611 is about. Refusing to grade is
    the honest answer; grading against a graph nobody ran is not.

    There is deliberately no trims-only special case. One existed while the
    previous side was the raw crossover at the applied candidate's own
    parameters — a candidate that emitted no filters then produced a
    byte-identical pair of curves and had, in that frame, commanded nothing. In
    THIS frame a trims-only candidate commands its whole trim, polarity and
    delay step, which is a real and gradeable change. A candidate that genuinely
    commands nothing now produces a flat-zero delta and is refused one layer
    down by ``classify_delta_probe``'s own commanded floor (``nothing_commanded``),
    which is the owner of that question.
    """
    if previous_predicted_sum is None or predicted_sum is None:
        return None
    try:
        previous_freqs, previous_db = previous_predicted_sum
        freqs, db = predicted_sum
        grid = np.asarray(freqs, dtype=float)
        delta = np.asarray(db, dtype=float) - np.interp(
            grid,
            np.asarray(previous_freqs, dtype=float),
            np.asarray(previous_db, dtype=float),
        )
    except (ValueError, TypeError, IndexError, AttributeError):
        return None
    return grid, delta


class CornerDisagreement(NamedTuple):
    """Why an applied profile's corner and a capture's disagree."""

    reason: str
    fields: dict[str, Any]


def corner_disagreement(
    profile: Mapping[str, Any] | None, capture_fc_hz: float | None,
) -> CornerDisagreement | None:
    """Why the applied profile's corner and this capture's disagree, or ``None``.

    Asked BEFORE the model is built: a previous graph modelled on branches
    composed through a crossover it never ran is wrong by up to 5.88 dB against
    the delta probe's 1.5 dB tolerance (adversarial panel, PR #2614). Both arms
    of the one disagreement: a profile naming a DIFFERENT corner from this
    capture's, and a profile naming one at all when the capture ran none. A
    relative tolerance because both numbers have been through a JSON round trip.
    """
    applied_fc_hz = profile_crossover_fc_hz(profile)
    if applied_fc_hz is None:
        if capture_fc_hz is None:
            return None
        return CornerDisagreement("applied_profile_names_no_corner", {})
    if capture_fc_hz is not None and math.isclose(
        applied_fc_hz, float(capture_fc_hz), rel_tol=1e-6
    ):
        return None
    return CornerDisagreement("crossover_corner_moved", {
        "applied_fc_hz": round(applied_fc_hz, 3),
        "capture_fc_hz": (
            None if capture_fc_hz is None else round(float(capture_fc_hz), 3)
        ),
    })


class PreviousGraph(NamedTuple):
    """One applied profile's graph, and what it predicts on this capture."""

    graph: GraphSummation
    predicted: tuple[np.ndarray, np.ndarray]


def previous_graph_prediction(
    profile: Mapping[str, Any] | None,
    *,
    roles: Sequence[str],
    draft_inverted_by_role: Mapping[str, bool],
    responses: Mapping[str, Any],
    alignment: Any,
) -> PreviousGraph | str:
    """The previous graph on these branches, or the code refusing it.

    The graph comes back beside its prediction; the caller journals both.
    """
    graph = profile_graph_summation(
        profile, roles=roles, draft_inverted_by_role=draft_inverted_by_role,
    )
    if graph is None:
        return "applied_profile_names_no_graph"
    if any(role not in responses for role in roles):
        return "capture_missing_a_declared_branch"
    predicted = graph_predicted_sum(
        # The LOWEST branch's grid, the one ``plan_linearization`` builds the
        # applied side on, so both sides land on one grid.
        responses[roles[0]].freqs_hz,
        {role: response.complex_tf for role, response in responses.items()},
        graph,
        # The SAME gate the applied side's residual is derived through
        # (``program_analysis._build_candidate``): an anchor the aligner refused
        # is no anchor, and both sides then model the frame the
        # independently-aligned branch pair is already in.
        anchor_delay_us=(
            alignment.anchor_delay_us
            if alignment is not None and alignment.status == ALIGNMENT_OK
            else None
        ),
    )
    if predicted is None:
        return "previous_graph_model_failed"
    return PreviousGraph(graph, predicted)
