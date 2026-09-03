# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commanded axis: what one apply asks the summed response to CHANGE.

The delta probe grades a measured change against a commanded one; this module
builds the commanded one, and its whole contract is that every element the apply
commands must appear in it (#2611). Both sides are evaluated on the SAME
measured branch pair through the SAME summation model, so only the two graphs
differ: APPLIED is the candidate about to go live, PREVIOUS is the graph it
replaces. The pre-split common attenuation is deliberately NOT on this axis —
``baseline_profile.applied_program_level_delta_db`` removes it at probe time.
The two graphs' crossover corner must match and is CHECKED: a mismatch omits a
term measured at up to 5.88 dB against this probe's 1.5 dB tolerance (#2614).
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

    ``trim_db`` is keyed by this speaker's branches LOWEST FIRST;
    :func:`graph_predicted_sum` reads the graph's own roles off it.

    ``delay_us`` is SIGNED in the analysis frame (design §5.6.5: positive means
    the tweeter branch is delayed), never the non-negative magnitude a profile
    stores beside a delayed role.

    ``polarity_sign`` is the relative sign in the measured branches' OWN frame —
    a flip RELATIVE TO the design draft, not an absolute reading of how the
    drivers are wired, because the branches arrive already carrying the draft's
    declared per-role polarity. A profile records ABSOLUTE per-role ``inverted``
    flags instead, so :func:`profile_graph_summation` must convert (#2614).
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

    ``roles`` is this speaker's branches LOWEST FIRST — one on a 1-way main, two
    otherwise. More than two is refused: ONE delay and ONE relative sign.

    ``None`` — "this profile does not say what the speaker is playing" — for an
    absent profile, for one whose authoritative ``corrections`` mapping does not
    carry EVERY role, and for one that names a role without naming its
    ``gain_db``. Never a fabricated unity graph, which would make the commanded
    axis claim the apply commands the whole of the previous profile's trim. An
    absent ``delay_ms`` IS a zero: the profile records a magnitude only on
    whichever role is delayed, so its absence on the other is a statement.

    ``draft_inverted_by_role`` is REQUIRED, not defaulted, because there is no
    safe guess. It is the design draft's own per-role declared polarity
    (``camilla_yaml.role_polarity`` of the preset the capture was composed
    through), and the returned ``polarity_sign`` is the profile's relative
    polarity stated RELATIVE TO it — the frame the measured branches are already
    in (#2614).
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
        # The profile records a non-negative magnitude on the delayed role, so
        # the sign is recovered from WHICH role carries it: a delayed upper
        # branch is the positive direction of the analysis frame.
        delay_us = 1000.0 * (
            float(upper.get("delay_ms") or 0.0)
            - float(lower.get("delay_ms") or 0.0)
        )
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(delay_us) and all(map(math.isfinite, trim_db.values()))):
        return None
    linearization = profile_linearization(profile)
    # The frame conversion, in one place: the profile's flags and the draft's
    # are both absolute, and the model wants the difference between the two
    # relative polarities.
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

    The one owner of "which ``C`` did this profile run": the previous side of the
    commanded axis only models the graph the speaker played while that corner
    matches the corner the capture was composed at.

    Read off ``recomposition_snapshot["preset"]`` through the same
    :class:`~jasper.active_speaker.profile.ActiveSpeakerPreset` parse every other
    snapshot reader uses, reduced through
    :class:`~.contracts.CandidateAcousticContext`, which fails closed on a split
    or empty section set rather than picking a region.

    ``None`` for a profile with no snapshot, an unparseable preset, or a section
    set that names no one corner: the corner cannot be checked, so the previous
    graph cannot be affirmed and the probe declines to grade.
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
    function's own, through ``program_analysis.summed_model_residual_delay_us``
    — the only correct way to enter a delay here, since its docstring carries
    the double-counting hazard. The branches are the GRAPH's own, lowest first.

    The anchor is THIS capture's and does NOT cancel: it sets where the blend
    null sits, and re-anchoring one side moves its null somewhere the other side
    has none, where the two disagree by 7-33 dB (measured, #2614). Both graphs
    have to be stated in ONE phasing frame — the frame the branch pair was
    measured and aligned in.

    ``None`` when a branch is missing or the arithmetic cannot complete: an
    unbuildable model of the previous graph is an absent commanded axis, never a
    crash.
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
    grid. A missing PREVIOUS curve is the load-bearing case: nothing here can
    then say what the speaker is playing right now, and grading against a graph
    nobody ran is not the honest answer.

    There is deliberately no trims-only special case: in THIS frame a trims-only
    candidate commands its whole trim, polarity and delay step. A candidate that
    genuinely commands nothing produces a flat-zero delta and is refused one
    layer down by ``classify_delta_probe``'s own commanded floor
    (``nothing_commanded``), which owns that question.
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
    the delta probe's 1.5 dB tolerance (#2614). Both arms of the one
    disagreement: a profile naming a DIFFERENT corner from this capture's, and a
    profile naming one at all when the capture ran none. A relative tolerance
    because both numbers have been through a JSON round trip.
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
        # The SAME gate the applied side's residual is derived through: an
        # anchor the aligner refused is no anchor, and both sides then model the
        # frame the independently-aligned branch pair is already in.
        anchor_delay_us=(
            alignment.anchor_delay_us
            if alignment is not None and alignment.status == ALIGNMENT_OK
            else None
        ),
    )
    if predicted is None:
        return "previous_graph_model_failed"
    return PreviousGraph(graph, predicted)
