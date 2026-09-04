# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What one candidate build produced, as frozen values (#2291).

Each value is held per build and passed by hand, never parked on the session:
a build a retake moots is dropped whole, so no fact can be read against the
next candidate. No behaviour lives here — decisions belong to
:mod:`.accountability` or the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np

from .contracts import TrimStrategy
from .plan_assembly import LevelConsistency, LinearizationPlan

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
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`
    travel together because it raises on ``band_spread`` without ``n_positions``.
    ``excluded_bands_hz`` is the MERGED honesty mask (power-vs-median screen
    union the identified-null registry), never one instrument alone — #1742
    item 4. ``boost_excluded_bands_hz`` does NOT go to the envelope; it is the
    boost-only bound (#1967) the fit vocabulary takes, and empty means
    "nothing contradicted a boost", not "no evidence".
    """

    excluded_bands_hz: tuple[tuple[float, float], ...]
    band_spread: tuple[Any, ...]
    n_positions: int
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class LinearizationState:
    """What ONE candidate build's linearization produced, as a value (#2291).

    ``outcome`` is one of ``"fitted"``/``"trim_rejected"`` (the planner's
    verdict), ``"ineligible_mic_tier"``/``"ineligible_repeats"`` (eligibility
    refusals) or ``"fit_failed"`` (the SF2 degrade); empty means no build ran.
    Every other field is ``None``/empty on the three non-planning outcomes,
    ``linearized_predicted_sum`` included — a trims-only candidate publishes
    the RAW two-branch prediction as its VERIFY prior.
    """

    outcome: str = ""
    core_level_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    trim_band_estimate_db: Mapping[str, float] = field(default_factory=dict)
    polish_delta_db: Mapping[str, float] = field(default_factory=dict)
    """Per role, dB: the MEASURE ripple polish's trim excursion off the
    band-average solve. Empty is "not measured", never "the polish moved
    nothing" (that is an all-zero mapping)."""
    level_consistency: LevelConsistency | None = None
    """The two per-driver level estimates graded against each other
    (:func:`~.intervention.compare_level_definitions`). ``None`` is "no
    verdict", never "the estimators agreed"."""
    linearized_predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    realized_level_match: "RealizedLevelMatch | None" = None
    trim_strategy: TrimStrategy | None = None
    """Which pair :func:`~.intervention.decide_trim` committed, carried across
    the commit seam so the proposal states it instead of re-deriving it from
    ``outcome`` (which cannot tell an anchored commit from a resolved one).
    ``None`` where no pair was committed, and where a trim pin displaced the
    one that was."""
    anchor_drift_db: float | None = None
    """dB between the ripple scan's tweeter trim and the anchor's — the
    quantity the strategy turned on. Set and cleared with the strategy."""

    @classmethod
    def from_plan(cls, plan: LinearizationPlan) -> "LinearizationState":
        """Everything a planned candidate leaves behind, read off the plan."""
        return cls(
            outcome=plan.outcome,
            core_level_evidence=plan.core_level_evidence,
            trim_band_estimate_db=plan.trim_band_estimate_db,
            polish_delta_db=plan.polish_delta_db,
            level_consistency=plan.level_consistency,
            linearized_predicted_sum=plan.linearized_predicted_sum,
            realized_level_match=plan.realized_level_match,
            trim_strategy=None if plan.trim is None else plan.trim.strategy,
            anchor_drift_db=None if plan.trim is None else plan.trim.anchor_drift_db,
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

    A speculative build must land here and never in ``_candidate``, which is
    ``confirm_cloud_measure_group``'s fire-once guard: writing it there closes
    the retake window in the instant it opens. It reaches ``_candidate`` only
    through the household's own confirmation. ``level_frame_finding`` (#1866)
    is present only when THIS build's frame gate took the finding+proceed path.
    """

    candidate: Any
    predicted_sum: Any
    analysis: Any
    cloud: CloudFitEvidence | None
    level_frame_finding: Mapping[str, Any] | None = None
    linearization: LinearizationState = field(default_factory=LinearizationState)
    """What THIS build's linearization produced — see :class:`LinearizationState`."""
