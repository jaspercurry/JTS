# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The promotion path: persisted carve-out records become findings.

Promotion attaches a ``mechanism``, a ``fix_class`` and a ``confidence`` tier
to numbers shipped instruments already produced. It is not a detector: no
signal is analysed and no threshold applied here. Every promoted finding
stays ``unsure`` (P2-only support), ``eq`` is never the routed class for an
interference null, and the household sentence is copied, never rewritten.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Mapping, Sequence

from jasper.log_event import log_event

from .findings import EvidenceRef, Finding, FindingError
from .closed_sets import (
    CONFIDENCE_UNSURE,
    PROBE_POSITION_VARIANCE,
    PROBE_ROTATION,
)
from .mechanisms import (
    MECHANISM_BOUNDARY_SBIR,
    MECHANISM_HF_REFLECTION,
)
from .session_identity import SessionIdentity

logger = logging.getLogger(__name__)

#: Producer id written into the finding set's provenance marker.
PRODUCED_BY = "jasper.attribution.promotion.promote_carve_outs"

#: The one carve-out source that carries attributable evidence.
SOURCE_IDENTIFIED_NULL = "identified_null"

#: Position-variance classification -> (mechanism, routed fix class).
#: ``insufficient_evidence`` is absent.
_CLASSIFICATION_ROUTES: Mapping[str, tuple[str, str]] = {
    # Source-fixed: same frequencies at every position (§4 M2).
    "position_invariant": (MECHANISM_HF_REFLECTION, "carve"),
    # Position-variant interference null -> `physical`, never `eq` (§4 M5).
    "position_dependent": (MECHANISM_BOUNDARY_SBIR, "physical"),
}

_EVIDENCE_KEYS = ("f_center_hz", "n", "tau_us", "r_time", "r_freq", "depth_db")


def _intervals(carve_outs: Any) -> list[Mapping[str, Any]]:
    """Flatten the persisted per-band carve-out structure, de-duplicated.

    The persisted structure lists a null under every spec band it overlaps, but
    a straddling null is one physical feature and must become one finding.
    """

    if not isinstance(carve_outs, Sequence) or isinstance(carve_outs, (str, bytes)):
        return []
    seen: set[tuple[Any, Any, Any]] = set()
    flat: list[Mapping[str, Any]] = []
    for band in carve_outs:
        if not isinstance(band, Mapping):
            continue
        rows = band.get("intervals")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = (row.get("f_lo_hz"), row.get("f_hi_hz"), row.get("source"))
            if key in seen:
                continue
            seen.add(key)
            flat.append(row)
    return flat


def _band_bounds(row: Mapping[str, Any]) -> tuple[float, float] | None:
    """This record's ``(f_lo_hz, f_hi_hz)`` as real floats, or ``None``.

    A narrowing, not a second validator: ordering and non-negativity stay
    :class:`Finding`'s. ``bool`` is excluded explicitly because
    ``isinstance(True, int)`` would otherwise make a band edge 1.0 Hz.
    """

    bounds: list[float] = []
    for key in ("f_lo_hz", "f_hi_hz"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        bounds.append(number)
    return bounds[0], bounds[1]


def promote_carve_outs(
    carve_outs: Any,
    *,
    session: SessionIdentity,
    cites: Iterable[EvidenceRef],
) -> tuple[Finding, ...]:
    """Promote every attributable carve-out record to a finding.

    ``carve_outs`` is the persisted block from the cloud pipeline's result, so
    the same call promotes a live close and a replayed archive. ``cites`` must
    include at least one commissioning-bundle citation (``Finding`` enforces
    it). Returns findings in ascending band order; empty is common.
    """

    pointers = tuple(cites)
    out: list[Finding] = []
    for row in _intervals(carve_outs):
        if row.get("source") != SOURCE_IDENTIFIED_NULL:
            continue
        route = _CLASSIFICATION_ROUTES.get(str(row.get("classification") or ""))
        if route is None:
            continue
        mechanism, fix_class = route
        evidence: dict[str, Any] = {
            key: row[key] for key in _EVIDENCE_KEYS if row.get(key) is not None
        }
        evidence["classification"] = str(row.get("classification"))
        band_hz = _band_bounds(row)
        if band_hz is None:
            # A record that IS attributable but whose band is unusable: a
            # refusal, not a skip, so it is never dropped silently.
            log_event(
                logger,
                "attribution.carve_out_promotion_refused",
                level=logging.WARNING,
                mechanism=mechanism,
                classification=str(row.get("classification")),
                error=(
                    "carve-out record has no usable band: "
                    f"f_lo_hz={row.get('f_lo_hz')!r} f_hi_hz={row.get('f_hi_hz')!r}"
                ),
            )
            continue
        try:
            out.append(
                Finding(
                    mechanism=mechanism,
                    band_hz=band_hz,
                    evidence=evidence,
                    # Rule 1 — P2-only support never rises above `unsure`.
                    confidence=CONFIDENCE_UNSURE,
                    fix_class=fix_class,
                    # Rule 3 — the shipped sentence, copied.
                    household_copy=str(row.get("reason") or ""),
                    probes_run=(PROBE_POSITION_VARIANCE,),
                    probes_recommended=(PROBE_ROTATION,),
                    cites=pointers,
                )
            )
        except FindingError as exc:
            # A malformed record must not take the whole findings set with it.
            # It stays in the null registry and the carve-out disclosure.
            log_event(
                logger,
                "attribution.carve_out_promotion_refused",
                level=logging.WARNING,
                mechanism=mechanism,
                classification=str(row.get("classification")),
                error=str(exc),
            )
            continue
    out.sort(key=lambda finding: finding.band_hz)
    return tuple(out)


__all__ = [
    "PRODUCED_BY",
    "SOURCE_IDENTIFIED_NULL",
    "promote_carve_outs",
]
