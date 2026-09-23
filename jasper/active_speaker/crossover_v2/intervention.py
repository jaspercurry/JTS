# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fit a crossover's branches and anchor their trims, as pure functions.

Assembles this repository's existing pure DSP primitives; no fitter, solver or
estimator is reimplemented here. Must never import anything under
:mod:`jasper.web`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from ..branch_chain import CrossoverSection, radiating_band_hz, sections_by_role
from ..branch_target import branch_target
from ..linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    _SIGMA_TOLERABLE_DB as SIGMA_TOLERABLE_DB,
    EnvelopeCurve,
    compose_envelope,
    compute_sigma_curve,
)
from ..linearization_fit import (
    FitVocabulary,
    LinearizationFit,
    complex_correction_response,
    core_level_band_hz,
    fit_driver_linearization,
    measurement_hole_bands_hz,
)
from ..profile import CrossoverRegion
from jasper.audio_measurement.program_analysis import solve_branch_trims

from .contracts import CrossoverV2ContractError

__all__ = [
    "BranchFits", "CloudFitTerms",
    "DriverEvidence",
    "LINEARIZATION_MIN_PAIRED_OCCURRENCES",
    "NonFiniteTrimError",
    "PlannerError",
    "PlannerInputError",
    "anchor_trims",
    "resolve_trims_after_fit",
    "compose_sigma_db",
    "fit_branches",
]


class PlannerError(CrossoverV2ContractError):
    """The planner cannot plan from these inputs — a refusal, not a crash.

    A subclass of :class:`~.contracts.CrossoverV2ContractError` so it inherits
    that base's :attr:`refusal_reason`.
    """


class PlannerInputError(PlannerError):
    """A required planner input is missing or malformed."""

    refusal_reason = "contract_invalid"


class NonFiniteTrimError(PlannerError):
    """A fitted trim term is NaN or infinite, so no trim is resolved."""

    refusal_reason = "trim_not_finite"


# Minimum paired in-capture occurrences (primary + repeats) per driver before
# the linearization path is trusted at all. A POLICY floor — what linearization
# requires — deliberately not imported from the MEASURE program's own default
# repeat count, which it happens to equal today.
LINEARIZATION_MIN_PAIRED_OCCURRENCES = 3


def compose_sigma_db(
    own: Any,
    sibling: Any,
    *,
    tier: str,
    valid_band_hz: tuple[float, float],
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray | None:
    """The σ-composition policy: the paired-N gate plus the per-tier floor.

    ``own``/``sibling`` are the two
    :class:`~jasper.audio_measurement.program_analysis.DriverResponse` of a
    crossover pair. Returns ``None`` — no evidence, no permission — when EITHER
    driver has fewer than :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES`
    occurrences (primary + repeats); an under-repeated sibling voids the pair's
    trust even if ``own`` alone has plenty. Raises :class:`PlannerInputError`
    for a tier outside the closed set.

    Otherwise computes ``own``'s live σ(f) and floors it at the tier's own
    tolerable value: ``sigma_eff = max(sigma_tolerable(tier), live)``.

    That floor is BEHAVIORALLY INERT at its current value.
    ``repeatability_limit``'s formula is
    ``D_cap * min(1, sigma_tolerable / max(sigma, eps))``, which for any
    ``live <= sigma_tolerable`` already saturates at ``D_cap``. It exists as a
    seam for a policy that sets the floor HIGHER than ``sigma_tolerable``; do
    not assume it currently does more than the paired-N gate above.
    """
    own_n = 1 + len(own.repeat_responses)
    sibling_n = 1 + len(sibling.repeat_responses)
    if (
        own_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
        or sibling_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
    ):
        return None
    live = compute_sigma_curve(own, valid_band_hz=valid_band_hz, grid_hz=grid_hz)
    if live is None:
        return None
    try:
        floor_db = SIGMA_TOLERABLE_DB[tier]
    except KeyError as exc:
        # A tier outside the closed set is a caller error, not a measurement
        # outcome; name it rather than let a bare ``KeyError`` escape.
        raise PlannerInputError(
            f"unknown mic tier {tier!r}; expected one of "
            f"{sorted(SIGMA_TOLERABLE_DB)}"
        ) from exc
    return np.maximum(floor_db, live)


@dataclass(frozen=True)
class CloudFitTerms:
    """What a closed spatial cloud contributes to the correction envelope.

    The three optional arguments of
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`,
    travelling together as one value so the fit cannot be handed a
    half-supplied pair, plus the boost-only bound the fit *vocabulary* takes.

    ``boost_excluded_bands_hz`` does NOT go to the envelope. Empty is the
    ordinary case and means "nothing contradicted a boost", never "no
    evidence".
    """

    excluded_bands_hz: tuple[tuple[float, float], ...] = ()
    band_spread: tuple[Any, ...] = ()
    n_positions: int = 0
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()
    boost_responses: tuple[Any, ...] = ()


@dataclass(frozen=True)
class DriverEvidence:
    """One driver's measured response and the two bands that bound its fit.

    ``excited_band_hz`` is the declared sweep span for this role — the band its
    stimulus actually radiated in, which bounds σ-composition and the
    envelope's validity. ``driver_class`` is the envelope's per-class depth
    table key.
    """

    role: str
    response: Any
    excited_band_hz: tuple[float, float]
    driver_class: str = "unknown"
    fit_budget: Mapping[str, Any] = field(default_factory=dict)


def anchor_trims(
    *,
    roles: tuple[str, ...],
    anchor_base_db: Mapping[str, float],
    giveback_db: Mapping[str, float],
) -> tuple[dict[str, float], float]:
    """Place the anchored trim pair and normalize it non-positive.

    Returns ``(anchored_db, normalize_shift_db)``. The anchor is
    ``base + giveback``: no third term, no branch, no threshold.
    ``anchor_base_db`` is the raw measured trim — the number the branch solve
    measured — so there is nothing to arbitrate here.

    **The non-positive normalize is the hearing-safety invariant here.** Every
    returned trim is ``<= 0``: a branch whose own cuts give back more than its
    raw attenuation would otherwise land POSITIVE (a boost), which the emitter
    refuses and the hardware must never see. The shift is subtracted from every
    role identically, so it preserves relative leveling exactly and is honest
    extra ledger rather than a tonal change.
    """

    unnormalized = {
        role: float(anchor_base_db.get(role, 0.0))
        + float(giveback_db.get(role, 0.0))
        for role in roles
    }
    shift = max(0.0, max(unnormalized.values()))
    return {r: v - shift for r, v in unnormalized.items()}, shift


def resolve_trims_after_fit(
    drivers: Sequence[DriverEvidence], fits: Mapping[str, LinearizationFit],
    regions: Sequence[CrossoverRegion],
) -> dict[str, float]:
    """Resolve fitted trims from raw measurements on one shared frequency grid."""
    by_role = {driver.role: driver for driver in drivers}
    region = next(region for region in regions
                  if {region.lower_driver, region.upper_driver} == by_role.keys())
    roles = (region.lower_driver, region.upper_driver)
    pair = [by_role[role] for role in roles]
    grid = pair[0].response.freqs_hz
    raw = [driver.response.complex_tf for driver in pair]
    corrected = [response * complex_correction_response(fits[driver.role].filters, grid)
                 for driver, response in zip(pair, raw)]
    spans = [(max(driver.excited_band_hz[0], driver.response.fit_floor_hz or 0.0),
              driver.excited_band_hz[1]) for driver in pair]
    before, after = [solve_branch_trims(
        grid, responses[0], responses[1], region.fc_hz,
        woofer_span_hz=spans[0], tweeter_span_hz=spans[1],
    ) for responses in (raw, corrected)]
    base = dict(zip(roles, before[:2]))
    giveback = dict(zip(roles, (before[2] - after[2], before[3] - after[3])))
    # The anchor's non-positive normalize is a max(): a NaN term compares False
    # and would reach the emitted trim unclamped.
    if not np.isfinite([*base.values(), *giveback.values()]).all():
        raise NonFiniteTrimError(f"trim base {base} or give-back {giveback} is not finite")
    trims, _ = anchor_trims(roles=roles, anchor_base_db=base, giveback_db=giveback)
    return trims


@dataclass(frozen=True)
class BranchFits:
    envelopes: Mapping[str, EnvelopeCurve]
    fits: Mapping[str, LinearizationFit]
    radiating_bands_hz: Mapping[str, tuple[float, float]]
    core_bands_hz: Mapping[str, tuple[float, float] | None]
    blind_bands_hz: tuple[tuple[float, float], ...]


def fit_branches(
    drivers: Sequence[DriverEvidence], *,
    mic_tiers: Mapping[str, str],
    vocabulary: FitVocabulary | Mapping[str, FitVocabulary],
    sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
    source_preset: Mapping[str, Any] | None = None,
    cloud: CloudFitTerms | Mapping[str, CloudFitTerms] | None = None,
    on_bands: Callable[[Mapping[str, tuple[float, float]]], None] | None = None,
) -> BranchFits:
    """Compose every envelope before fitting the shared measurement hole."""
    if sections is None:
        sections = sections_by_role(
            CrossoverRegion.from_mapping(region)
            for region in (source_preset or {}).get("crossover_regions") or ()
        )
    responses = {driver.role: driver.response for driver in drivers}
    radiating = {role: radiating_band_hz(sections.get(role, ())) for role in responses}
    if on_bands is not None:
        on_bands(radiating)
    envelopes = {}
    clouds = cloud if isinstance(cloud, Mapping) else {role: cloud for role in responses}
    for driver in drivers:
        role, response = driver.role, driver.response
        role_cloud = clouds.get(role)
        # The σ gate reads each branch's REPEAT count against its sibling's; a lone
        # branch is its OWN sibling, reducing the paired-N gate to its own count.
        sibling = next((other for name, other in responses.items() if name != role), response)
        envelopes[role] = compose_envelope(
            role, response, excited_band_hz=driver.excited_band_hz,
            mic_tier=mic_tiers[role], driver_class=driver.driver_class,
            sigma_db=compose_sigma_db(
                response, sibling, tier=mic_tiers[role], valid_band_hz=driver.excited_band_hz,
            ),
            excluded_bands_hz=role_cloud.excluded_bands_hz if role_cloud else None,
            band_spread=role_cloud.band_spread if role_cloud else None,
            n_positions=role_cloud.n_positions if role_cloud else None,
        )
    core = {role: core_level_band_hz(envelopes[role], radiating_band_hz=radiating[role]) for role in responses}
    blind = measurement_hole_bands_hz(list(core.values()))
    fits = {
        driver.role: fit_driver_linearization(
            driver.response, envelopes[driver.role],
            vocabulary=(vocabulary if isinstance(vocabulary, FitVocabulary) else vocabulary[driver.role])
            .with_budget(driver.fit_budget),
            radiating_band_hz=radiating[driver.role], blind_bands_hz=blind,
            target=branch_target(sections.get(driver.role, ()), envelopes[driver.role].freqs_hz),
        )
        for driver in drivers
    }
    return BranchFits(envelopes, fits, radiating, core, blind)


