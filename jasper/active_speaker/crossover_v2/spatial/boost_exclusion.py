# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.interference_nulls import (
    CLASSIFICATION_POSITION_DEPENDENT,
    classify_dip_position_variance,
)


# --------------------------------------------------------------------------- #
# the blind span below the null registry's floor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoostExclusion:
    """:func:`boost_excluded_bands_hz`'s answer, plus the line it justifies.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_boost_evidence``, as data rather than a log
    call because this module is side-effect-free.
    """

    bands: tuple[tuple[float, float], ...]
    diagnostics: dict[str, Any]


def boost_excluded_bands_hz(
    combined: Any,
    result: Mapping[str, Any],
    *,
    echo_band_hz: Sequence[float],
) -> BoostExclusion:
    """Bands BELOW the null registry's own floor where this cloud's positions
    disagree about a dip — so boosting one corrects nothing any listener hears
    (#1967).

    The registry's analysis band is floored at 4 kHz, so below that edge it
    contributes no exclusions — not because it was uncertain but because it was
    never asked — while a round's largest prescribed boost can sit under that
    floor. This runs
    :func:`~jasper.audio_measurement.interference_nulls.classify_dip_position_variance`
    over the blind span and hands the dips the positions DISAGREE about to the
    fit vocabulary, which refuses a lift whose realized cascade would put
    significant gain in one.

    It cannot GRANT boost anywhere: the bound is monotone by construction, and a
    ``position_invariant`` dip is left exactly as the gate had it, because
    position-invariance says a dip is real and not that it is correctable. The
    residual is named: such a dip may still be a source-fixed interference null
    rather than a driver deficit, and separating those needs the post-apply arm
    (#1868).

    Each band costs a real correction — the fit drops any boost filter whose
    action region overlaps one, per filter, with a whole-lift refusal
    (``lift_suppressed_reason="boost_excluded_band"``) only when every boost was
    aimed — which is why this returns only the positively-contradicted class and
    never a "we were unsure here" list.

    Fails OPEN, disclosed: a span too narrow to analyse, a cloud with no
    per-position curves, or a numeric failure all yield no exclusions.
    ``variance_check_failed`` is reported in
    :attr:`BoostExclusion.diagnostics` so the flow can raise the journal line's
    level.
    """
    registry = result.get("null_registry") or {}
    n_dependent = 0
    floor_hz = float(echo_band_hz[0])
    grid = np.asarray(getattr(combined, "freqs_hz", ()), dtype=float)
    # The cloud's gated validity floor is the honest lower edge: below it every
    # position's curve is a truncated-window artifact.
    validity_floor_hz = result.get("validity_floor_hz")
    lo_hz = float(validity_floor_hz) if validity_floor_hz else 0.0
    if grid.size:
        lo_hz = max(lo_hz, float(grid[0]))
    span = (lo_hz, floor_hz)
    bands: tuple[tuple[float, float], ...] = ()
    reason = ""
    n_dips = 0
    variance_check_failed = False
    if not (0.0 < lo_hz < floor_hz) or int(
        np.count_nonzero((grid >= lo_hz) & (grid <= floor_hz))
    ) < 3:
        reason = "no_blind_span"
    else:
        try:
            report = classify_dip_position_variance(combined, band_hz=span)
        except Exception:  # noqa: BLE001 - see "Fails OPEN" above.
            reason = "variance_check_failed"
            variance_check_failed = True
        else:
            reason = report.reason
            n_dips = len(report.dips)
            n_dependent = sum(
                dip.classification == CLASSIFICATION_POSITION_DEPENDENT
                for dip in report.dips
            )
            bands = report.position_dependent_bands_hz
    return BoostExclusion(
        bands=bands,
        diagnostics={
            # The band the registry adjudicated, and the span below it where
            # it structurally could not.
            "registry_band_hz": [round(v, 3) for v in echo_band_hz],
            "registry_classification": str(registry.get("classification") or ""),
            "registry_reason": str(registry.get("reason") or ""),
            "unadjudicated_span_hz": [round(v, 3) for v in span],
            "variance_reason": reason,
            "n_dips": n_dips,
            # How many of those dips the cloud's positions DISAGREED about — the
            # only class this bound acts on. ``n_dips - n_position_dependent`` is
            # the invariant remainder, which keeps its boost and is exactly the
            # residual #1868 has to close.
            "n_position_dependent": n_dependent,
            "boost_excluded_bands_hz": [
                [round(lo, 3), round(hi, 3)] for lo, hi in bands
            ],
            "variance_check_failed": variance_check_failed,
        },
    )
