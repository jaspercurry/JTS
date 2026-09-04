# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the analyzer is told about each capture (#2291 Phase 5a-iii).

Sibling of :mod:`.programs`: that module answers what a phase plays, this one
what the analyzer is told about the capture that comes back. Every function is
a decision about what to WITHHOLD — each field a :class:`MeasurementPriors`
carries licenses a claim the analyzer will then make, so the withholdings the
docstrings below name are load-bearing. Inputs are stated, never reached for:
session evidence arrives as arguments.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ..branch_chain import crossover_response_complex, radiating_band_hz, sections_by_role
from ..camilla_yaml import role_polarity
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program_analysis import (
    AppliedAlignment,
    MeasurementPriors,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program import ExcitationProgram, ProgramSegment

__all__ = [
    "role_transfers",
    "configured_crossover_transfers",
    "candidate_required_band_hz",
    "measure_sweep_bounds",
    "measure_sweep_durations_s",
    "check_priors",
    "measure_priors",
    "lateral_priors",
    "verify_priors",
    "cloud_priors",
    "entry_baseline_priors",
]


def role_transfers(
    sections_by_role_map: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Per-role ``freqs -> complex response``, evaluated HOST-side.

    The kernel may not import this package, so it gets a callable, never the
    ``CrossoverSection`` behind it.
    """
    if sections_by_role_map is None:
        return None
    return {
        role: functools.partial(crossover_response_complex, sections=tuple(sections))
        for role, sections in sections_by_role_map.items()
    }


def configured_crossover_transfers(
    source_preset: Any,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """``(response_by_role, polarity_sign_by_role)`` for the committed crossover.

    ONE derivation, two phases: MEASURE consumes it as §4.2's ``C_c``, VERIFY as
    the candidate's design target for the summed response (R18, #1868).
    """
    return (
        role_transfers(sections_by_role(source_preset.crossover_regions)),
        {role: -1 if inverted else 1
         for role, inverted in role_polarity(source_preset).items()},
    )


def candidate_required_band_hz(
    sections_by_role_map: Mapping[str, Sequence[Any]], *, fc_hz: float,
) -> dict[str, tuple[float, float]]:
    """§4.2's candidate-required bins per role, at ONE corner.

    Each role's radiating span unioned with the trim/alignment overlap band.
    The overlap is deliberately UNCLAMPED: a superset is the safe side of a
    required mask. Single owner of this formula (#2291 Phase 5a-v, #2336 N2).
    """
    overlap = overlap_band_hz(float(fc_hz))
    return {
        role: (min(radiating_band_hz(sec)[0], overlap[0]),
               max(radiating_band_hz(sec)[1], overlap[1]))
        for role, sec in sections_by_role_map.items()
    }


def _sweep_branches(
    measure_program: "ExcitationProgram | None",
) -> tuple["ProgramSegment", ...]:
    """Every branch's first-occurrence sweep, lowest first.

    ``build_measure_program`` pins ``sweep_w`` for the lower driver and
    ``sweep_t`` for the upper, and a 1-way main's solo keeps the ``sweep_w``
    spelling, so a missing ``sweep_t`` is a one-branch program.
    """
    if measure_program is None:
        return ()
    branches: list["ProgramSegment"] = []
    for segment_id in ("sweep_w", "sweep_t"):
        try:
            branches.append(measure_program.segment(segment_id))
        except KeyError:
            break
    return tuple(branches)


def measure_sweep_bounds(
    measure_program: "ExcitationProgram | None",
) -> tuple[float, float] | None:
    """The band EVERY MEASURE branch was excited over, or ``None``.

    Read off the COMPOSED MEASURE program rather than derived from Fc (§5.6).
    On a pair that is the upper branch's sweep floor and the lower branch's
    sweep ceiling; on a 1-way main, the solo sweep's own band.
    """
    branches = _sweep_branches(measure_program)
    if not branches:
        return None
    lo, hi = branches[-1].f1_hz, branches[0].f2_hz
    if lo is None or hi is None:
        return None
    return float(lo), float(hi)


def measure_sweep_durations_s(
    measure_program: "ExcitationProgram | None",
) -> dict[str, float] | None:
    """MEASURE's ACTUAL per-role sweep length, realized — possibly fitted.

    #2921's duration fit is a continuous float no search grid can reach, so an
    offline rebuild can only reproduce a fitted program by reading this back.
    ``None`` when there is no MEASURE program yet, as for the band above.
    """
    branches = _sweep_branches(measure_program)
    if measure_program is None or not branches:
        return None
    rate = measure_program.sample_rate_hz
    return {str(seg.role): seg.n_samples / rate for seg in branches}


def check_priors(*, fc_hz: float | None) -> MeasurementPriors:
    """CHECK's priors — Fc only, for the MEASURE level solve (#1825).

    Fc scopes each band's SNR requirement by whether the band lies inside the
    crossover overlap window (``program_analysis._band_required_snr_db``).
    Withholding it applies the ALIGNMENT requirement everywhere — a louder
    solve — so this prior can only make MEASURE quieter, never louder.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz)


def measure_priors(
    *,
    fc_hz: float | None,
    source_preset: Any,
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None,
    ambient_report: Any,
    alignment_delay_bounds_us: tuple[float, float] | None,
    applied_alignment: AppliedAlignment | None,
    explicit_alignment_delay_us: float | None,
    explicit_alignment_polarity_sign: int | None,
) -> MeasurementPriors:
    """MEASURE's priors — the widest set, and the only §4.2 de-embedding.

    Every input is keyword-only and undefaulted, deliberately: giving
    ``applied_alignment`` a default would silently downgrade a held alignment to
    "commit no delay" (#2617), and defaulting ``explicit_alignment_delay_us``
    would run the AUTOMATIC alignment on a round its receipt calls prescribed.

    ``applied_alignment`` reaches MEASURE alone, because MEASURE is the only
    phase that commits an alignment; handing it to VERIFY or a cloud pose puts
    the speaker's current answer inside a comparison meant to be independent of
    it. ``ambient_report`` is CHECK's measured room floor (#1830), ``None`` only
    where CHECK produced none, leaving the SNR verdict honestly absent. The
    explicit alignment pins are REQUEST facts, validated at the boundary the
    request arrived on.

    The three configured-path fields are gated on ``protection_sections_by_role``
    together: ``_compose_configured_path_ir`` RAISES on a partial prior set.
    """
    configured_response, configured_polarity = configured_crossover_transfers(
        source_preset
    )
    return MeasurementPriors(
        crossover_fc_hz=fc_hz,
        alignment_delay_bounds_us=alignment_delay_bounds_us,
        applied_alignment=applied_alignment,
        explicit_alignment_delay_us=explicit_alignment_delay_us,
        explicit_alignment_polarity_sign=explicit_alignment_polarity_sign,
        ambient_report=ambient_report,
        measurement_protection_response_by_role=role_transfers(
            protection_sections_by_role
        ),
        configured_crossover_response_by_role=(
            configured_response if protection_sections_by_role is not None else None
        ),
        configured_polarity_sign_by_role=(
            configured_polarity if protection_sections_by_role is not None else None
        ),
        # §4.2's candidate-required bins, from their single owner above. Absent
        # with no corner: the union is half an overlap band, and a 1-way
        # declares neither.
        candidate_required_band_hz_by_role=(
            None if protection_sections_by_role is None or fc_hz is None
            else candidate_required_band_hz(
                sections_by_role(source_preset.crossover_regions), fc_hz=fc_hz,
            )
        ),
    )


def lateral_priors(*, fc_hz: float | None, ambient_report: Any) -> MeasurementPriors:
    """Priors for one lateral pose — MEASURE-shaped, deliberately NEUTRAL.

    Everything the anchor gets EXCEPT the configured-path composition maps:
    §4.2's ``S_c = sign_c * M * C_c / P`` is per candidate, so baking the
    configured ``C`` in here would make the retained evidence answer for one
    corner alone. ``_compose_configured_path_ir`` returns its input untouched
    iff ALL THREE maps are ``None`` and raises if only some are, so the omission
    is an exact, checked no-op. No ``predicted_sum`` and no alignment bounds:
    §4.4 holds the anchor solution FIXED at the sides, so nothing here may read
    as a per-pose trim/delay/polarity solve.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz, ambient_report=ambient_report)


def verify_priors(
    *,
    fc_hz: float | None,
    source_preset: Any,
    predicted_sum: Any,
    sweep_bounds: tuple[float, float] | None,
) -> MeasurementPriors:
    """VERIFY's priors — the tracking comparison, and the absolute claim.

    The candidate's own crossover rides along for the ABSOLUTE claim (R18,
    #1868), UNGUARDED by ``measurement_protection`` unlike :func:`measure_priors`
    — here it IS the design target. Safe only while the de-embedding
    (``_compose_configured_path_ir``) stays reachable from ``_analyze_measure``
    alone.
    """
    configured_response, configured_polarity = configured_crossover_transfers(
        source_preset
    )
    return MeasurementPriors(
        crossover_fc_hz=fc_hz,
        predicted_sum=predicted_sum,
        measure_excited_band_hz=sweep_bounds,
        configured_crossover_response_by_role=configured_response,
        configured_polarity_sign_by_role=configured_polarity,
    )


def cloud_priors(*, fc_hz: float | None) -> MeasurementPriors:
    """Priors for a position-group capture — deliberately WITHOUT ``predicted_sum``.

    The mic is OFF the design axis by construction, so measured-vs-predicted
    divergence there is the spatial variation the cloud exists to sample, not a
    tracking error; withholding the prior leaves ``analysis.verify_tracking``
    ``None``. The candidate's crossover transfers are withheld for the same
    reason (R18, #1868): an off-axis crossover-region ABSOLUTE claim would grade
    the crossover's own lobing as a realization defect. Do not thread either
    back in.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz)


def entry_baseline_priors(*, fc_hz: float | None) -> MeasurementPriors:
    """Priors for #2291's pre-apply capture — :func:`cloud_priors`' two
    withholdings, for a different reason.

    ``crossover_fc_hz`` is kept: it is the session's declared crossover, not a
    claim about this capture. Nothing is applied when this capture is taken, so
    ``predicted_sum`` (and with it ``measure_excited_band_hz``, which only
    clamps the tracking band) would grade the ENTRY graph against the
    CANDIDATE's model; withholding leaves ``analysis.verify_tracking`` ``None``,
    which ``verification.evaluate_realization`` reads as UNAVAILABLE, not as a
    pass. The configured-crossover maps are withheld for the same reason (R18,
    #1868). Do not thread the withheld priors back in.
    """
    return MeasurementPriors(crossover_fc_hz=fc_hz)
