# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What one candidate build produced, as values (#2291 Phase 5a-v).

A sibling of :mod:`.programs`, :mod:`.priors` and :mod:`.spatial`.
Those three answer what a phase plays, what the analyzer is told, and what a
capture-consuming phase decides.  This one answers the question that comes
after all of them: **a build ran — what did it produce, and how does that
product travel?**

**Every type here exists because a fact tried to travel on ``self`` and could
not be trusted to.**  That is not a stylistic preference; it is the shape of
the 2026-08-10 incident.  Until #2291 Phase 2b a build's linearization left
its seven by-products on the conductor as ``_last_*`` fields, which made the
Fc sweep snapshot and restore all seven around every candidate — because a
value belonging to candidate N would otherwise be read as candidate N+1's, or
as the anchor's.  One save/restore bug away from publishing a prescription
computed for a different crossover.  Held per build and passed by hand, the
question cannot arise: a state describes exactly the candidate whose build
returned it, and a build a retake moots is dropped whole.

**Why these three moved together, and why now.**  They are the Fc sweep's
blocking dependency: ``_evaluate_fc_candidate`` consumes a build product,
and through it the linearization state and the cloud terms.  5a-iii deferred
the sweep on exactly that — "its return types are 5a-v territory" — and the
committed sweep golden's header says so in as many words.  Moving the values
ahead of the machinery that reads them is what lets the sweep follow without
a second flow-local type appearing behind it.

**No behaviour lives here.**  These are frozen values with one projection
(:meth:`LinearizationState.from_plan`) and one derived property; every
decision that reads them belongs to :mod:`.accountability` or, still, to the
session.  A value that could refuse something would be a gate wearing a
dataclass, and the gate has its own module for the reason its docstring gives.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np

from .intervention import LevelConsistency, LinearizationPlan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import RealizedLevelMatch

__all__ = [
    "CloudFitEvidence",
    "LinearizationState",
    "SpeculativeClose",
]


@dataclass(frozen=True)
class CloudFitEvidence:
    """What a closed spatial cloud contributes to the correction envelope.

    The three optional arguments of
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`,
    travelling together as one value so the fit cannot be handed a half-supplied
    pair (``compose_envelope`` raises on ``band_spread`` without
    ``n_positions``, and this makes that unreachable from the flow).

    ``excluded_bands_hz`` is the MERGED honesty mask — the power-vs-median
    screen union the identified-null registry, as
    ``assemble_cloud_group_result`` merged it. Not the screen's intervals
    and not the registry's: the wiring contract (issue #1742 item 4) is that
    the instruments are consumed together.

    ``boost_excluded_bands_hz`` does NOT go to the envelope. It is the
    boost-only bound (#1967) the fit vocabulary takes, and it rides here
    because it is derived from the same closed cloud at the same moment —
    see :func:`~jasper.active_speaker.crossover_v2.spatial.boost_excluded_bands_hz`.
    Empty is the ordinary case and means "nothing contradicted a boost", never
    "no evidence".

    **The three required fields keep no defaults, deliberately** — the
    permissive-default trap 5a-iv closed for
    :class:`~jasper.active_speaker.crossover_v2.spatial.CaptureScreens`, and
    the reason is the same one stated there: a default that is only correct
    because today's caller always supplies the field is a defect waiting for a
    caller that does not.

    **A near-twin exists and is deliberately NOT collapsed into**:
    :class:`~jasper.active_speaker.crossover_v2.intervention.CloudFitTerms` is
    structurally identical, and its :meth:`~…CloudFitTerms.from_evidence`
    adapts this one.  Collapsing them would be an SSOT win on the face of it
    and a behaviour change underneath: ``from_evidence`` COERCES every field
    (``float``/``tuple``) on the way in, and its ``isinstance`` fast path
    returns a ``CloudFitTerms`` unchanged — so a flow that constructed the
    planner's own type directly would silently stop being coerced.  Whether
    that coercion is a no-op for every value the flow builds is an evidence
    question nobody has answered, and answering it is the price of the
    collapse.  Recorded here rather than left for the next reader to
    rediscover: the twin is known, the merge is available, and it needs
    measurement rather than tidiness.
    """

    excluded_bands_hz: tuple[tuple[float, float], ...]
    band_spread: tuple[Any, ...]
    n_positions: int
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class LinearizationState:
    """What ONE candidate build's linearization produced, as a value (#2291).

    **This class is the scratch channel's replacement.** Until Phase 2b the
    same seven facts lived on the conductor as ``self._last_*`` fields written
    as a side effect of the fit. That made them a *return channel with no
    caller*: the Fc sweep had to snapshot and restore all seven around every
    candidate (``_FC_SWEEP_CONDUCTOR_FIELDS``) precisely because a value
    belonging to candidate N would otherwise be read as candidate N+1's — or as
    the anchor's. One save/restore bug away from publishing a prescription
    computed for a different crossover, which is the family the 2026-08-10
    incident belongs to.

    Held per build and passed by hand, the question cannot arise: a state
    describes exactly the candidate whose build returned it, and a build a
    retake moots is dropped whole — the same reason :class:`SpeculativeClose`
    exists, applied one layer down.

    ``outcome`` is the union of the planner's own verdict
    (``"fitted"``/``"trim_rejected"``) and the two the session decides
    without planning at all: an eligibility refusal
    (``"ineligible_mic_tier"``/``"ineligible_repeats"``) and the SF2 degrade
    (``"fit_failed"``). Empty means no build ran. It is stamped verbatim onto
    the candidate, which is then the single reader every other surface quotes.

    Every other field is ``None``/empty on all three non-planning outcomes,
    including ``linearized_predicted_sum`` — so a candidate that degraded to
    trims-only publishes the RAW two-branch prediction as its VERIFY prior,
    which is what the trims-only lane means. Legacy left that one field
    un-cleared on the SF2 path (its own comment named the gap); a fit that
    raised part-way has no linearized model, so carrying one forward was the
    fail-open direction.
    """

    outcome: str = ""
    core_level_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    trim_band_estimate_db: Mapping[str, float] = field(default_factory=dict)
    level_consistency: LevelConsistency | None = None
    """Both subordinate estimators graded against the summed level owner.

    ``None`` on every non-planning outcome and for a candidate planned with no
    summed capture in hand — the "no verdict" state, and NOT a synonym for
    "the estimators agreed". See
    :func:`~.intervention.check_level_consistency`. It flags a capture as
    retriable and never moves a number.
    """
    linearized_predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    realized_level_match: "RealizedLevelMatch | None" = None

    @classmethod
    def from_plan(cls, plan: LinearizationPlan) -> "LinearizationState":
        """Everything a planned candidate leaves behind, read off the plan.

        A straight projection — no policy, no re-derivation. The plan is the
        single owner of each of these values; this is the session naming the
        subset it consumes downstream.
        """
        return cls(
            outcome=plan.outcome,
            core_level_evidence=plan.core_level_evidence,
            trim_band_estimate_db=plan.trim_band_estimate_db,
            level_consistency=plan.level_consistency,
            linearized_predicted_sum=plan.linearized_predicted_sum,
            realized_level_match=plan.realized_level_match,
        )

    @property
    def realized_branch_level(self) -> dict[str, Any] | None:
        """The realized-level verdict serialized, or ``None`` when none ran."""
        return (
            None if self.realized_level_match is None
            else self.realized_level_match.to_dict()
        )


@dataclass(frozen=True)
class SpeculativeClose:
    """A group close that already RAN, waiting for the household to want it.

    The eager-fit rider's one carried value (owner UX direction, 2026-07-30).
    The household's last stage-1 position lands, the phone shows the confirm,
    and the several seconds the fit costs used to start only when they walked
    back to a browser and tapped Continue — dead air that read as a stalled
    screen. The fit now starts on the ACCEPT and parks its product here.

    **Why the built candidate cannot simply be stashed on the session.**
    ``_candidate`` is ``confirm_cloud_measure_group``'s fire-once guard AND,
    until this rider, the held-set predicate; writing a speculative build into
    it would have closed the retake window in the same instant it opened (both
    seams carried a comment saying exactly that). So a speculative build lands
    HERE, where nothing else reads it, and reaches ``_candidate`` only through
    the household's own confirmation.

    Everything ``_commit_measure_candidate`` needs and nothing else:
    ``analysis`` rides along for the raw ``predicted_sum`` the commanded-delta
    diff needs, and ``cloud`` for the build's log line.

    ``level_frame_finding`` is the #1866 record — present only when THIS
    build's frame gate took the finding+proceed path. It rides the built
    close rather than the session for the reason the whole class exists: a
    speculative build a retake moots is dropped whole, and a record left on
    ``self`` would survive that drop and be published against the next
    candidate, which measured something else.
    """

    candidate: Any
    predicted_sum: Any
    analysis: Any
    cloud: CloudFitEvidence | None
    level_frame_finding: Mapping[str, Any] | None = None
    linearization: LinearizationState = field(default_factory=LinearizationState)
    """What THIS build's linearization produced — see :class:`LinearizationState`.

    Rides the close for the same reason ``level_frame_finding`` does, and now
    for the whole of the fit's output rather than one record of it: a
    speculative build a retake moots is dropped whole, and anything left on
    ``self`` would survive that drop and be read against the next candidate.
    """
