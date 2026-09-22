# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cloud evidence binding in the banked linearization fit."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.branch_chain import chain_response
from jasper.active_speaker.branch_target import SIGNIFICANT_GAIN_DB
from jasper.active_speaker.crossover_v2 import position_cycle
from jasper.active_speaker.crossover_v2.contracts import DESIGN_AXIS_DEG
from jasper.active_speaker.crossover_v2.evidence_packet import _mapping
from jasper.active_speaker.crossover_v2.intervention import (
    CloudFitTerms,
    DriverEvidence,
    fit_branches,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
from jasper.active_speaker.linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    MIC_TIERS,
)
from jasper.active_speaker.linearization_fit import FitVocabulary
from jasper.audio_measurement.spatial_combine import BandSpread, octave_bands_hz

from .banked import BankedRound, _round_candidate, response_from_banked_curve

#: The round banked no linearization fit to run a counterfactual against.
CLOUD_BINDING_NO_FIT = "round_banked_no_linearization_fit"

#: The round banked a fit but no cloud exclusion evidence, so there is no
#: input to sever.
CLOUD_BINDING_NO_CLOUD_EVIDENCE = "round_banked_no_cloud_exclusion_evidence"

#: The MEASURE take carries no curve able to rebuild the fit's own inputs —
#: a round banked before the per-occurrence ``validity_floor_hz`` and
#: ``repeat_curves`` rode on it. Nothing here can be reconstructed from what
#: such a round holds; re-run the round to ask this question of it.
CLOUD_BINDING_FIT_INPUTS_NOT_BANKED = "fit_inputs_not_banked"

#: The fit is per-branch and the sibling's occurrence count gates the sigma
#: term, so the counterfactual is stated for a PAIR. A 1-way main is a
#: different question, not a broken round.
CLOUD_BINDING_NOT_A_PAIR = "fit_roles_not_a_pair"

#: The round's ``linearization`` entries are the PRESCRIBED shape, not the
#: fitted one — a candidate may read ``fit_failed`` and still carry prescribed
#: filters, and the entry's ``prescribed_by`` is what tells them apart. There
#: is no fit to run a counterfactual against, which is a different answer from
#: a reconstruction that drifted.
CLOUD_BINDING_NOT_FITTED = "round_linearization_was_prescribed_not_fitted"

#: A fitted entry that names no mic tier the envelope knows, or no driver
#: class. The refit cannot be composed without inventing one of them.
CLOUD_BINDING_ENTRY_INCOMPLETE = "banked_fit_entry_names_no_tier_or_driver_class"

#: The cloud block is present and malformed — a band row that is not a pair,
#: or a missing ``n_positions``. Not the same as a round that banked none.
CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE = "cloud_exclusion_evidence_unreadable"

#: The refit with every input wired did not reproduce the banked fit, so the
#: severed arm cannot be read as the cloud's doing.
CLOUD_BINDING_REFIT_DRIFTED = "refit_does_not_reproduce_the_banked_fit"

#: How far the all-inputs-wired refit may sit from the banked correction curve
#: before this view refuses to report a counterfactual, dB.
#:
#: The residue being allowed for is the BANKED GRID: a take banks its curves at
#: ``spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE`` (12/octave, 121 points),
#: while the session fitted the analysis rfft grid. Measured at 0.142 dB on a
#: synthetic two-bump driver with every other input identical; 0.5 dB is that
#: with margin, and is far below the 7.975 dB a missing fit input costs. Not a
#: quality bar — a reconstruction check. Raise it only against a re-measured
#: decimation residue, never to make a drifted refit report.
REFIT_TOLERANCE_DB = 0.5

#: The smallest wired-vs-severed move this view will call BINDING, dB, under
#: the per-role reconstruction error. ``branch_target.SIGNIFICANT_GAIN_DB`` is
#: the gain below which a realized cascade is already treated as putting
#: nothing into a band, so a difference under it cannot correspond to a filter
#: the fit placed or removed. Without a floor, a round whose refit reproduces
#: EXACTLY — the good case — would report a 1e-13 dB cascade difference as
#: bound.
BOUND_FLOOR_DB = SIGNIFICANT_GAIN_DB

#: What the severed arm cuts: the three cloud terms ``compose_envelope`` takes
#: together. Boost permission is deliberately NOT severed — production also
#: withholds it when no cloud reached the envelope, but that is the conductor's
#: policy rather than the cloud's evidence, and cutting both at once would
#: credit the exclusion term with a change the permission flag caused.
SEVERED_CLOUD_INPUTS: tuple[str, ...] = (
    "excluded_bands_hz", "band_spread", "n_positions",
)


@dataclass(frozen=True)
class CloudBindingBand:
    """One octave band's answer to "did severing the cloud move the fit here"."""

    center_hz: float
    f_lo_hz: float
    f_hi_hz: float
    delta_db: float
    cloud_excluded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_hz": self.center_hz,
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "delta_db": self.delta_db,
            "cloud_excluded": self.cloud_excluded,
        }


@dataclass(frozen=True)
class CloudBindingRole:
    """One branch's wired-vs-severed comparison.

    ``refit_vs_banked_db`` is this role's own reconstruction check — the max
    absolute difference between the all-inputs-wired refit and the correction
    curve the round banked. ``bound`` is ``max_delta_db`` clearing that same
    number: a severed-arm move smaller than the reconstruction's own error is
    not evidence the cloud did anything.
    """

    role: str
    bound: bool
    max_delta_db: float
    refit_vs_banked_db: float
    n_filters_wired: int
    n_filters_severed: int
    bands: tuple[CloudBindingBand, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "bound": self.bound,
            "max_delta_db": self.max_delta_db,
            "refit_vs_banked_db": self.refit_vs_banked_db,
            "n_filters_wired": self.n_filters_wired,
            "n_filters_severed": self.n_filters_severed,
            "bands": [band.to_dict() for band in self.bands],
        }


@dataclass(frozen=True)
class CloudBindingView:
    """Whether this round's cloud null evidence actually BOUND its fit.

    ``not_evaluated_reason`` is empty exactly when ``evaluable`` is true.
    ``roles`` is empty on every refusal EXCEPT a drifted reconstruction, which
    keeps its per-role numbers because they are what says which branch failed
    to reproduce and by how much. **Observed only:
    no grade moves, and this answers only whether the evidence CHANGED the
    fitted prescription — never whether the wired answer was right.** The
    corpus carries no ground truth for that.
    """

    round_dir: str
    evaluable: bool
    not_evaluated_reason: str
    bound: bool | None
    refit_matches_banked: bool | None
    refit_vs_banked_db: float | None
    tolerance_db: float
    severed_inputs: tuple[str, ...]
    roles: tuple[CloudBindingRole, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_dir": self.round_dir,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
            "bound": self.bound,
            "refit_matches_banked": self.refit_matches_banked,
            "refit_vs_banked_db": self.refit_vs_banked_db,
            "tolerance_db": self.tolerance_db,
            "severed_inputs": list(self.severed_inputs),
            "roles": [role.to_dict() for role in self.roles],
        }


def _not_evaluated(round_dir: Path, reason: str) -> CloudBindingView:
    return CloudBindingView(
        round_dir=str(round_dir),
        evaluable=False,
        not_evaluated_reason=reason,
        bound=None,
        refit_matches_banked=None,
        refit_vs_banked_db=None,
        tolerance_db=REFIT_TOLERANCE_DB,
        severed_inputs=SEVERED_CLOUD_INPUTS,
        roles=(),
    )


def _correction_db(
    filters: Sequence[Mapping[str, Any]], grid_hz: np.ndarray,
) -> np.ndarray:
    """A filter set's realized cascade magnitude on ``grid_hz``, dB.

    ``filters`` are the plain ``{biquad_type, freq, q, gain}`` records both a
    banked ``linearization[role].filters`` row and ``LinearizationFilter.
    to_dict`` are, so the refit and the fit it is compared against reach the
    one biquad evaluator by the same door.
    """
    if not filters:
        return np.zeros_like(grid_hz)
    return 20.0 * np.log10(
        np.maximum(np.abs(chain_response(list(filters), grid_hz)), 1e-12)
    )


def _cloud_inputs(evidence: Mapping[str, Any]):
    """``compose_envelope``'s three cloud arguments off the banked block, or
    ``None`` when the block is present and cannot supply them.

    The three travel together — ``compose_envelope`` requires them as a set —
    so they are validated as a set rather than read one at a time.
    """
    positions = evidence.get("n_positions")
    if isinstance(positions, bool) or not isinstance(positions, (int, float)):
        return None
    bands = []
    for row in evidence.get("excluded_bands_hz") or ():
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return None
        try:
            bands.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError):
            return None
    try:
        spread = tuple(
            BandSpread(**dict(row)) for row in evidence.get("band_spread") or ()
        )
    except TypeError:
        return None
    return tuple(bands), spread, int(positions)


def cloud_binding_view(banked: BankedRound) -> CloudBindingView:
    """Re-fit this round's linearization with the cloud's null evidence cut,
    and report whether that evidence BOUND the fit.

    The counterfactual the corpus can answer: the production ``cloud is None``
    branch of
    :func:`~.intervention.plan_linearization` hands ``compose_envelope`` its
    three cloud arguments as ``None`` together, so re-composing each branch's
    envelope that way and re-fitting shows what the null evidence contributed —
    where, and how much. It cannot show the wired answer was RIGHT: nothing
    banked is ground truth for the prescription.

    **It refuses rather than reporting a drifted reconstruction.** The wired
    arm is refitted from the round's own banked inputs first and compared with
    the correction curve the round banked; past :data:`REFIT_TOLERANCE_DB` the
    view is not evaluable, because a severed arm is only readable against a
    wired arm that reproduces. That check is also what makes the two inputs
    this refit cannot recover from the bank safe to leave at their defaults:
    a session whose ``boost_excluded_bands_hz`` was non-empty, or which
    withheld boost, fails it instead of quietly reporting.

    Observed only; no grade moves and ``round_receipt.json`` is untouched.
    """
    round_dir = banked.round_dir
    candidate = _round_candidate(banked)
    linearization = _mapping(candidate.get("linearization"))
    if not linearization:
        return _not_evaluated(round_dir, CLOUD_BINDING_NO_FIT)
    evidence = _mapping(candidate.get("exclusion_evidence"))
    if not evidence.get("excluded_bands_hz"):
        return _not_evaluated(round_dir, CLOUD_BINDING_NO_CLOUD_EVIDENCE)
    roles = tuple(sorted(linearization))
    if len(roles) != 2:
        return _not_evaluated(round_dir, CLOUD_BINDING_NOT_A_PAIR)
    entries = {role: _mapping(linearization[role]) for role in roles}
    # A candidate may read ``fit_failed`` and still carry PRESCRIBED filters;
    # the entry's own ``prescribed_by`` is the discriminator, per
    # ``MeasuredCrossoverCandidate``'s two-shape rule.
    if any(entry.get("prescribed_by") for entry in entries.values()):
        return _not_evaluated(round_dir, CLOUD_BINDING_NOT_FITTED)
    tiers = {role: str(entries[role].get("mic_tier") or "") for role in roles}
    classes = {
        role: str(entries[role].get("driver_class") or "") for role in roles
    }
    if any(tiers[role] not in MIC_TIERS or not classes[role] for role in roles):
        return _not_evaluated(round_dir, CLOUD_BINDING_ENTRY_INCOMPLETE)
    cloud = _cloud_inputs(evidence)
    if cloud is None:
        return _not_evaluated(round_dir, CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE)
    cloud_bands, band_spread, n_positions = cloud

    pair = position_cycle.read_pose_curve_pair(
        banked.session_dir,
        phase=PHASE_MEASURE,
        position_deg=DESIGN_AXIS_DEG,
        roles=roles,
    )
    if pair is None:
        return _not_evaluated(round_dir, CLOUD_BINDING_FIT_INPUTS_NOT_BANKED)
    read = {
        role: response_from_banked_curve(curve)
        for role, curve in zip(roles, pair[:2])
    }
    if any(entry is None for entry in read.values()):
        return _not_evaluated(round_dir, CLOUD_BINDING_FIT_INPUTS_NOT_BANKED)
    responses = {role: entry[0] for role, entry in read.items()}
    # The band each role was DRIVEN over, as the curve recorded it — so the
    # envelope is composed over the span the session composed it over.
    excited = {role: entry[1] for role, entry in read.items()}
    drivers = tuple(DriverEvidence(role, responses[role], excited[role], classes[role]) for role in roles)
    wired = fit_branches(
        drivers, source_preset=_mapping(candidate.get("source_preset")), mic_tiers=tiers,
        vocabulary=FitVocabulary(allow_boost=True),
        cloud=CloudFitTerms(cloud_bands, band_spread, n_positions),
    )
    severed = fit_branches(
        drivers, source_preset=_mapping(candidate.get("source_preset")), mic_tiers=tiers,
        vocabulary=FitVocabulary(allow_boost=True),
    )

    grid_hz = DEFAULT_ENVELOPE_GRID_HZ
    octaves = octave_bands_hz(float(grid_hz[0]), float(grid_hz[-1]))
    results: list[CloudBindingRole] = []
    worst_reconstruction = 0.0
    for role in roles:
        wired_db = _correction_db(
            [f.to_dict() for f in wired.fits[role].filters], grid_hz
        )
        severed_db = _correction_db(
            [f.to_dict() for f in severed.fits[role].filters], grid_hz
        )
        banked_db = _correction_db(entries[role].get("filters") or (), grid_hz)
        reconstruction = float(np.max(np.abs(wired_db - banked_db)))
        worst_reconstruction = max(worst_reconstruction, reconstruction)
        delta = np.abs(wired_db - severed_db)
        bands = tuple(
            CloudBindingBand(
                center_hz=center,
                f_lo_hz=lo,
                f_hi_hz=hi,
                delta_db=float(np.max(delta[(grid_hz >= lo) & (grid_hz <= hi)])),
                cloud_excluded=any(
                    lo <= hi_null and lo_null <= hi for lo_null, hi_null in cloud_bands
                ),
            )
            for center, lo, hi in octaves
        )
        max_delta = float(np.max(delta))
        results.append(CloudBindingRole(
            role=role,
            # Against this role's OWN reconstruction error, floored: a move
            # smaller than what the refit already mis-states, or than the
            # smallest gain a realized cascade puts anywhere, is not evidence
            # the cloud did anything.
            bound=max_delta > max(reconstruction, BOUND_FLOOR_DB),
            max_delta_db=max_delta,
            refit_vs_banked_db=reconstruction,
            n_filters_wired=len(wired.fits[role].filters),
            n_filters_severed=len(severed.fits[role].filters),
            bands=bands,
        ))

    if worst_reconstruction > REFIT_TOLERANCE_DB:
        return replace(
            _not_evaluated(round_dir, CLOUD_BINDING_REFIT_DRIFTED),
            refit_matches_banked=False,
            refit_vs_banked_db=worst_reconstruction,
            # WHICH branch failed to reproduce, and by how much: this refusal
            # is the view's own self-check, so the numbers behind it travel.
            roles=tuple(results),
        )
    return CloudBindingView(
        round_dir=str(round_dir),
        evaluable=True,
        not_evaluated_reason="",
        bound=any(role.bound for role in results),
        refit_matches_banked=True,
        refit_vs_banked_db=worst_reconstruction,
        tolerance_db=REFIT_TOLERANCE_DB,
        severed_inputs=SEVERED_CLOUD_INPUTS,
        roles=tuple(results),
    )
