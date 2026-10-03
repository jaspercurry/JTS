# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the analyzer is told about each capture (#2291).

Sibling of :mod:`.programs`: that module answers what a phase plays, this one
what the analyzer is told about the capture that comes back. Every function is
a decision about what to WITHHOLD — each field a :class:`MeasurementPriors`
carries licenses a claim the analyzer will then make, so the withholdings the
docstrings below name are load-bearing. Inputs are stated, never reached for:
session evidence arrives as arguments.
"""

from __future__ import annotations

import functools
from typing import Any, Mapping, Sequence

from ..branch_chain import crossover_response_complex, radiating_band_hz
from ..crossover_section import sections_by_role
from ..camilla_yaml import role_polarity
from jasper.audio_measurement.program_analysis.model import SummedAlignmentReference
from jasper.audio_measurement.comparison_bands import overlap_band_hz
from jasper.audio_measurement.program_analysis import (
    AppliedAlignment,
    MeasurementPriors,
)

__all__ = [
    "role_transfers",
    "configured_crossover_transfers",
    "candidate_required_band_hz",
    "check_priors",
    "measure_priors",
    "lateral_priors",
]


def role_transfers(sections_by_role_map: Mapping[str, Any]) -> dict[str, Any]:
    """Per-role ``freqs -> complex response``, evaluated HOST-side.

    The kernel may not import this package, so it gets a callable, never the
    ``CrossoverSection`` behind it.
    """
    return {
        role: functools.partial(crossover_response_complex, sections=tuple(sections))
        for role, sections in sections_by_role_map.items()
    }


def configured_crossover_transfers(
    source_preset: Any,
) -> tuple[dict[str, Any], dict[str, int]]:
    """``(response_by_role, polarity_sign_by_role)`` for the committed crossover.

    ONE derivation, two readers: MEASURE consumes it as §4.2's ``C_c``, the
    summed-alignment reference as the graph's design target (#1868).
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
    required mask. Single owner of this formula (#2291, #2336).
    """
    overlap = overlap_band_hz(float(fc_hz))
    return {
        role: (min(radiating_band_hz(sec)[0], overlap[0]),
               max(radiating_band_hz(sec)[1], overlap[1]))
        for role, sec in sections_by_role_map.items()
    }


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
    protection_sections_by_role: Mapping[str, Sequence[Any]],
    ambient_report: Any,
    alignment_delay_bounds_us: tuple[float, float] | None,
    applied_alignment: AppliedAlignment | None,
    summed_alignment: SummedAlignmentReference | None = None,
) -> MeasurementPriors:
    """MEASURE's priors — the widest set, and the only §4.2 de-embedding.

    Every input is keyword-only and undefaulted, deliberately: giving
    ``applied_alignment`` a default would silently downgrade a held alignment to
    "commit no delay" (#2617).

    ``applied_alignment`` reaches MEASURE alone, because MEASURE is the only
    phase that commits an alignment; handing it to VERIFY or a cloud pose puts
    the speaker's current answer inside a comparison meant to be independent of
    it. ``ambient_report`` is CHECK's measured room floor (#1830), ``None`` only
    where CHECK produced none, leaving the SNR verdict honestly absent.

    The three configured-path maps travel together: ``_compose_configured_path_ir``
    RAISES on a partial prior set.
    """
    configured_response, configured_polarity = configured_crossover_transfers(
        source_preset
    )
    return MeasurementPriors(
        crossover_fc_hz=fc_hz,
        alignment_delay_bounds_us=alignment_delay_bounds_us,
        applied_alignment=applied_alignment,
        ambient_report=ambient_report, summed_alignment=summed_alignment,
        measurement_protection_response_by_role=role_transfers(
            protection_sections_by_role
        ),
        configured_crossover_response_by_role=configured_response,
        configured_polarity_sign_by_role=configured_polarity,
        # §4.2's candidate-required bins, from their single owner above. Absent
        # with no corner: the union is half an overlap band, and a 1-way
        # declares neither.
        candidate_required_band_hz_by_role=(
            None if fc_hz is None
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
