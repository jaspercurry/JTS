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

**Known incompleteness, bounded and named.** The measured branches are
composed through the CONFIGURED crossover of the current design draft
(``program_analysis._compose_configured_path_ir``'s ``S = M*C/P``), and the
previous side is modelled on those same branches. If the household changed the
crossover corner in ``/sound`` after the previous apply and before this
measurement, the previous graph ran an OLDER ``C`` than the one this model
gives it, and the difference lands in the probe's residual as an uncommanded
term. An accepted alternative Fc does not do this — that apply writes the draft
and the profile together — so the reachable case is a manual Sound edit between
rounds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.branch_chain import chain_response
from jasper.audio_measurement.program_analysis import (
    predicted_branch_sum,
    summed_model_residual_delay_us,
)

__all__ = [
    "GraphSummation",
    "commanded_delta",
    "graph_predicted_sum",
    "profile_graph_summation",
]


@dataclass(frozen=True)
class GraphSummation:
    """What one emitted Layer-A graph does to a measured branch pair.

    The four elements :func:`graph_predicted_sum` needs, and nothing else — a
    value, so the reader that extracts them from a profile and the model that
    consumes them cannot drift into two different opinions about which graph is
    being described.

    ``delay_us`` is SIGNED in the analysis frame (design §5.6.5: positive means
    the tweeter branch is delayed), never the non-negative magnitude a profile
    stores beside a delayed role. ``polarity_sign`` is the RELATIVE sign between
    the two branches, which is what a magnitude sum is sensitive to; inverting
    both branches leaves ``|W + s·T|`` untouched and is correctly a ``+1`` here.
    """

    trim_db: Mapping[str, float]
    delay_us: float
    polarity_sign: int
    linearization: Mapping[str, Sequence[Mapping[str, Any]]]


def profile_graph_summation(
    profile: Mapping[str, Any] | None,
    *,
    woofer_role: str,
    tweeter_role: str,
) -> GraphSummation | None:
    """Read one applied profile as a :class:`GraphSummation`, or ``None``.

    ``None`` — "this profile does not say what the speaker is playing" — for an
    absent profile, and for one whose authoritative ``corrections`` mapping does
    not carry BOTH roles. It is never a graph that trims nothing: a fabricated
    unity graph would make the commanded axis claim the apply commands the whole
    of the previous profile's trim, which is the same class of wrong answer this
    module exists to remove, pointing the other way.

    Both halves are read through :mod:`~jasper.active_speaker.baseline_profile`,
    which owns WHICH copy of a profile field is authoritative. This function
    owns only the translation into the summed model's own vocabulary: the
    profile stores a non-negative delay magnitude on whichever role is delayed,
    and a per-role ``inverted`` flag; the model wants one signed delay and one
    relative sign.
    """
    from jasper.active_speaker.baseline_profile import (
        profile_driver_corrections,
        profile_linearization,
    )

    corrections = profile_driver_corrections(profile)
    woofer = corrections.get(woofer_role)
    tweeter = corrections.get(tweeter_role)
    if not isinstance(woofer, Mapping) or not isinstance(tweeter, Mapping):
        return None
    try:
        trim_db = {
            woofer_role: float(woofer.get("gain_db") or 0.0),
            tweeter_role: float(tweeter.get("gain_db") or 0.0),
        }
        # The profile records a non-negative magnitude on the delayed role
        # (``measured_crossover_candidate.driver_corrections``), so the sign is
        # recovered from WHICH role carries it: a delayed tweeter is the
        # positive direction of the analysis frame, a delayed woofer the
        # negative one. Both are read rather than assuming only one is set.
        delay_us = 1000.0 * (
            float(tweeter.get("delay_ms") or 0.0)
            - float(woofer.get("delay_ms") or 0.0)
        )
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(delay_us) and all(map(math.isfinite, trim_db.values()))):
        return None
    linearization = profile_linearization(profile)
    return GraphSummation(
        trim_db=trim_db,
        delay_us=delay_us,
        polarity_sign=(
            -1 if bool(woofer.get("inverted")) != bool(tweeter.get("inverted")) else 1
        ),
        linearization={
            role: tuple(
                entry for entry in (linearization.get(role) or ())
                if isinstance(entry, Mapping)
            )
            for role in (woofer_role, tweeter_role)
        },
    )


def graph_predicted_sum(
    freqs_hz: Any,
    branch_tf: Mapping[str, Any],
    graph: GraphSummation,
    *,
    woofer_role: str,
    tweeter_role: str,
    anchor_delay_us: float | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """``(freqs_hz, magnitude_db)`` this graph would produce on these branches.

    The same three owners the applied side is built from, in the same order and
    with the same arguments, so the two curves differ by the GRAPH and by
    nothing else:

    * :func:`~jasper.active_speaker.branch_chain.chain_response` for the
      correction biquads — the repo's one biquad evaluator, and the same one
      ``complex_correction_response`` bottoms out in, so a correction read back
      off a profile is evaluated exactly as the fit's own was;
    * :func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum`
      for the trimmed, signed, delayed two-branch sum;
    * :func:`~jasper.audio_measurement.program_analysis.
      summed_model_residual_delay_us` for the residual, which is the ONLY
      correct way to enter a delay here (its docstring carries the
      double-counting hazard). The anchor is this capture's, identical on both
      sides, so it sets where the null sits and cancels out of the ratio
      between them.

    ``None`` when a branch is missing or the arithmetic cannot complete — the
    same bounded, fail-soft family as its callers, because an unbuildable model
    of the previous graph is an absent commanded axis, never a crash.
    """
    try:
        freqs = np.asarray(freqs_hz, dtype=float)
        woofer = np.asarray(branch_tf[woofer_role], dtype=np.complex128)
        tweeter = np.asarray(branch_tf[tweeter_role], dtype=np.complex128)
        summed = predicted_branch_sum(
            woofer * chain_response(graph.linearization.get(woofer_role, ()), freqs),
            tweeter * chain_response(graph.linearization.get(tweeter_role, ()), freqs),
            float(graph.trim_db.get(woofer_role, 0.0)),
            float(graph.trim_db.get(tweeter_role, 0.0)),
            int(graph.polarity_sign),
            freqs_hz=freqs,
            residual_delay_us=summed_model_residual_delay_us(
                anchor_delay_us, graph.delay_us,
            ),
        )
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        return None
    return freqs, 20.0 * np.log10(np.maximum(np.abs(summed), 1e-12))


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
