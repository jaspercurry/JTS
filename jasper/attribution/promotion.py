# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The promotion paths: verdicts the flow already computed become findings.

Promotion attaches a ``mechanism``, a ``fix_class`` and a ``confidence`` tier
to numbers shipped instruments already produced. Neither path is a detector:
no signal is analysed and no threshold applied here. Every promoted finding
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
    PROBE_DESIGN_AXIS,
    PROBE_POSITION_VARIANCE,
    PROBE_REPEAT_VARIANCE,
    PROBE_ROTATION,
)
from .mechanisms import (
    MECHANISM_BOUNDARY_SBIR,
    MECHANISM_HF_REFLECTION,
    MECHANISM_LEVEL_FRAME,
)
from .session_identity import SessionIdentity

logger = logging.getLogger(__name__)

#: Producer id written into the finding set's provenance marker.
PRODUCED_BY = "jasper.attribution.promotion.promote_carve_outs"

#: The level-frame path's own provenance marker.
PRODUCED_BY_LEVEL_FRAME = (
    "jasper.attribution.promotion.promote_level_frame_disagreement"
)

#: The one carve-out source that carries attributable evidence. Mirrors
#: ``crossover_v2.spatial.CARVE_OUT_SOURCE_IDENTIFIED_NULL``.
SOURCE_IDENTIFIED_NULL = "identified_null"

#: Position-variance classification -> (mechanism, routed fix class). Mirrors
#: ``interference_nulls.CLASSIFICATION_*``; ``insufficient_evidence`` is absent.
_CLASSIFICATION_ROUTES: Mapping[str, tuple[str, str]] = {
    # Source-fixed: same frequencies at every position (§4 M2).
    "position_invariant": (MECHANISM_HF_REFLECTION, "carve"),
    # Position-variant interference null -> `physical`, never `eq` (§4 M5).
    "position_dependent": (MECHANISM_BOUNDARY_SBIR, "physical"),
}

_EVIDENCE_KEYS = ("f_center_hz", "n", "tau_us", "r_time", "r_freq", "depth_db")


def _intervals(carve_outs: Any) -> list[Mapping[str, Any]]:
    """Flatten the persisted per-band carve-out structure, de-duplicated.

    ``carve_outs_by_band`` lists a null under every spec band it overlaps, but
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


#: The one sentence a household may be shown when the ESTIMATORS disagree, and
#: the ONLY place it is written; :data:`REALIZED_LEVEL_HOUSEHOLD_COPY` is its
#: sibling and the record's ``reason`` picks between them. It reports an
#: outcome and asks for nothing (ruling S8: the two readings measure different
#: quantities, so re-measuring cannot close the gap). It names no part of the
#: speaker — :class:`~jasper.attribution.findings.Finding` enforces that.
LEVEL_FRAME_HOUSEHOLD_COPY = (
    "Two different ways of reading how this speaker's high and low ranges "
    "balance came out apart from each other. They measure different things, "
    "so that is expected here and neither one is wrong. The tuning was set "
    "from the measurement either way — nothing to do."
)

#: The sentence for the OTHER condition this record can carry: the committed
#: pair's two REALIZED levels sit further apart than the tolerance
#: (``intervention.REALIZED_LEVEL_SUSPECT_REASON``). It carries the
#: recommendation the realized-level demotion
#: (`docs/measurement-loop-doctrine.md` deviation (i)) would otherwise have
#: lost, and says "would not end up" because the levels are read off the
#: emission the fit MODELS, not off a capture of the applied tuning.
REALIZED_LEVEL_HOUSEHOLD_COPY = (
    "This speaker's high and low ranges would not end up level with each other "
    "on the tuning this pass produced. Re-check what you entered in speaker "
    "setup — each range's sensitivity, and any resistor pad — then measure "
    "again."
)

#: The band keys, which become ``band_hz`` rather than evidence. Every OTHER
#: key in the record is evidence (see :func:`promote_level_frame_disagreement`).
_LEVEL_FRAME_BAND_KEYS = ("f_lo_hz", "f_hi_hz")


def promote_level_frame_disagreement(
    record: Any,
    *,
    session: SessionIdentity,
    cites: Iterable[EvidenceRef],
) -> Finding | None:
    """Promote one banked level-frame disagreement to an M7 finding.

    Two conditions reach here and the record's own ``reason`` says which: the
    two level DEFINITIONS differ, or the committed pair's REALIZED levels do.
    This function reads that field and nothing else — re-deciding the gate's
    threshold would be §3.1's forbidden second verdict. Every non-band key is
    evidence, by rule rather than by list. Returns the finding, or ``None``
    when the record is malformed — logged, never raised: a findings failure
    must not cost a session the gate already allowed to proceed.
    """

    if not isinstance(record, Mapping):
        return None
    band_hz = _band_bounds(record)
    evidence = {
        str(key): value
        for key, value in record.items()
        if key not in _LEVEL_FRAME_BAND_KEYS
    }
    if band_hz is None:
        log_event(
            logger,
            "attribution.level_frame_promotion_refused",
            level=logging.WARNING,
            mechanism=MECHANISM_LEVEL_FRAME,
            error=(
                "banked level-frame record has no usable band: "
                f"f_lo_hz={record.get('f_lo_hz')!r} "
                f"f_hi_hz={record.get('f_hi_hz')!r}"
            ),
        )
        return None
    # Imported inside the function: `intervention` costs ~1.8 s and ~1000
    # modules against ~0.1 s for this leaf package. Safe because the only path
    # that reaches here already has it in `sys.modules`, so this cannot raise
    # an ImportError past the seam above.
    from jasper.active_speaker.crossover_v2.intervention import (
        REALIZED_LEVEL_SUSPECT_REASON,
    )

    # `.get`, so an absent or unrecognised `reason` falls to the estimator arm,
    # which reports an outcome and asks for nothing — never the realized arm's
    # request to go and change a setup value.
    realized_only = record.get("reason") == REALIZED_LEVEL_SUSPECT_REASON
    try:
        return Finding(
            mechanism=MECHANISM_LEVEL_FRAME,
            band_hz=band_hz,
            evidence=evidence,
            # `unsure` on both arms: the definition arm cannot separate a real
            # level error from two estimators reading different spans of a
            # non-flat curve, and the realized arm measures the pair's levels
            # but not the CAUSE.
            confidence=CONFIDENCE_UNSURE,
            # REALIZED -> `eq`: the frame is not in dispute, the committed pair
            # simply sits at levels that do not match (§4 M7). DEFINITION ->
            # `document_as_physics`: ruling S8 — there is nothing to re-solve.
            fix_class="eq" if realized_only else "document_as_physics",
            household_copy=(
                REALIZED_LEVEL_HOUSEHOLD_COPY
                if realized_only
                else LEVEL_FRAME_HOUSEHOLD_COPY
            ),
            # NO probe was run: none of the flow's estimators is a §5 primitive,
            # and claiming one ran would launder a model-derived number.
            probes_run=(),
            probes_recommended=(PROBE_DESIGN_AXIS, PROBE_REPEAT_VARIANCE),
            cites=tuple(cites),
        )
    except FindingError as exc:
        log_event(
            logger,
            "attribution.level_frame_promotion_refused",
            level=logging.WARNING,
            mechanism=MECHANISM_LEVEL_FRAME,
            error=str(exc),
        )
        return None


__all__ = [
    "LEVEL_FRAME_HOUSEHOLD_COPY",
    "PRODUCED_BY",
    "PRODUCED_BY_LEVEL_FRAME",
    "REALIZED_LEVEL_HOUSEHOLD_COPY",
    "SOURCE_IDENTIFIED_NULL",
    "promote_carve_outs",
    "promote_level_frame_disagreement",
]
